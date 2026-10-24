import AppKit
import Network
import ScreenCaptureKit
import VideoToolbox
import IOKit.pwr_mgt

// The wire, both ways: [type: UInt8][length: UInt32 big-endian][payload]. The iPad's RemoteWire.swift mirrors it.
enum Wire {
    // iPad → Mac
    static let hello: UInt8 = 0x01      // JSON {"token", "curtain": Bool, "acks": Bool, "width": Int, "prefer": String}, the last two as in `settings`
    static let input: UInt8 = 0x02      // JSON, see Input.handle
    static let keyframe: UInt8 = 0x03   // empty: the decoder lost its place
    static let got: UInt8 = 0x04        // empty: one frame arrived (sent only by an iPad whose hello says "acks")
    static let settings: UInt8 = 0x05   // JSON {"width": 0 = auto | pixels, "prefer": "fps" | "quality"}: the resolution, live
    // Mac → iPad
    static let info: UInt8 = 0x10       // JSON {"w", "h", "locked"}: the stream's pixel size
    static let params: UInt8 = 0x11     // HEVC parameter sets (VPS, SPS, PPS), each [UInt16 big-endian length][bytes]
    static let frame: UInt8 = 0x12      // [1 = keyframe][AVCC NAL units]
    static let refused: UInt8 = 0x13    // JSON {"error"}
    static let stats: UInt8 = 0x14      // JSON {"fps", "w", "mbps", "ms", "est": {"<width>": fps}}: once a second, see adapt

    static func pack(_ type: UInt8, _ payload: Data) -> Data {
        var d = Data([type])
        withUnsafeBytes(of: UInt32(payload.count).bigEndian) { d.append(contentsOf: $0) }
        d.append(payload)
        return d
    }
}

/// One connection from the iPad. Until it says the token it is only a knock (the iPad's reachability probe is one);
/// once it does, it becomes the one live session, and the previous one ends. Its state is touched only on `q` (the curtain and the power assertion on main).
final class Session: NSObject, SCStreamOutput, SCStreamDelegate, @unchecked Sendable {
    private static var live: Session?

    private let conn: NWConnection
    private let secret: String
    private let q = DispatchQueue(label: "jarvis.remote.session", qos: .userInteractive)
    // The low-latency encoder's call returns only when the frame is done (~10 ms here): on `q` it held up the iPad's acks behind it,
    // which read as a late link. It runs on its own queue, one frame at a time; a screen that came meanwhile goes next, the newest.
    private let encodeQ = DispatchQueue(label: "jarvis.remote.encode", qos: .userInteractive)
    private var encoding = false, missed = false
    private var inbox = Data()
    private var authed = false
    private var ended = false

    private var stream: SCStream?
    private var encoder: VTCompressionSession?
    private var lastPixels: CVPixelBuffer?
    private var needKey = true
    private var inFlight = 0            // bytes handed to the connection and not yet taken by TCP
    private var behind = false          // a frame came while the link was full; the newest goes once it drains
    // An iPad without acks: past this many bytes waiting to enter TCP the link is behind. TCP's own buffer (up to 4 MB) sits under it,
    // which on a slow link is seconds of lag.
    private static let fullLink = 1_500_000
    // An iPad that acks: the frames on their way to it, oldest first, with when each went. Once the oldest has been out
    // longer than `lag` the link is behind, so what he sees is never much older than that, whatever the buffers in between hold.
    // (A byte budget scaled by the bitrate starved itself: a low rate gave a small budget, which stalled, which cut the rate.)
    private var acks = false
    // The resolution, from the iPad's panel: a pinned width, or 0 = auto. `prefer` says what gives when the link is short: "fps" lets
    // sharpness go (the bitrate falls, and in auto the stream narrows), "quality" lets frames go (a bitrate floor keeps text sharp,
    // and the hold below drops what the link can't carry).
    private var pinned = 0
    private var prefer = "fps"
    private static let widths = [1920, 2160, 2420]   // the panel's choices; 2420 is his iPad Pro 11"'s own width
    // Fewer pixels encode faster, but not in proportion: what one frame costs the hardware encoder at each width, measured while he
    // streams at it, gives the panel's fps estimates. Seeded from 10-24's measurements.
    private static var encodeNs: [Int: Double] = [1920: 5_000_000, 2160: 7_000_000, 2420: 9_000_000]
    private var full = (0, 0)           // the display's own pixels
    private var cfg: SCStreamConfiguration?
    private var unacked: [(bytes: Int, sent: UInt64)] = []
    private var unackedBytes = 0
    private var delivered = 0           // bytes the iPad acked this second
    private var ackCount = 0, ackSum: UInt64 = 0, ackMax: UInt64 = 0   // how long frames took to be acked, this second
    private static let lag: UInt64 = 100_000_000
    private var held: Bool {
        guard acks else { return inFlight >= Session.fullLink }
        guard let oldest = unacked.first else { return false }
        return DispatchTime.now().uptimeNanoseconds - oldest.sent > Session.lag
    }
    private var size = (0, 0)
    private var locked: Bool?
    private var lockWatch: DispatchSourceTimer?
    // the bitrate follows the link: down by a third in a second the link fell behind, up a little after three clear ones
    private static let topRate = 150_000_000, floorRate = 8_000_000, sharpRate = 60_000_000
    private var rate = 20_000_000       // under what his tailnet carries (~25-30 Mbit/s measured); it climbs from here
    private var encodeBusy: UInt64 = 0  // time spent inside the encode call this second: the encoder, not the link, capped him at ~74 fps
    private var stalls = 0
    private var sentFrames = 0, sentBytes = 0, captured = 0
    private var clear = 0

    private var curtained = false
    private var awake: IOPMAssertionID = 0
    // A windowless background agent is napped and its work run at low priority: the same encoder took ~11 ms a frame here
    // against <1 ms in a foreground benchmark at 120 fps. While he is here it is latency-critical.
    private var activity: NSObjectProtocol?
    private let input = Input()

    init(_ conn: NWConnection, secret: String) {
        self.conn = conn
        self.secret = secret
    }

    func begin() {
        conn.stateUpdateHandler = { [self] s in
            switch s {
            case .failed, .cancelled: q.async { self.end() }
            default: break
            }
        }
        conn.start(queue: q)
        receive()
        q.asyncAfter(deadline: .now() + 5) { [self] in if !authed { end() } }
    }

    private func receive() {
        conn.receive(minimumIncompleteLength: 1, maximumLength: 1 << 16) { [self] data, _, done, error in
            if let data { inbox.append(data); drain() }
            if done || error != nil { end() } else if !ended { receive() }
        }
    }

    private func drain() {
        while inbox.count >= 5 {
            let s = inbox.startIndex
            let len = Int(inbox[s + 1]) << 24 | Int(inbox[s + 2]) << 16 | Int(inbox[s + 3]) << 8 | Int(inbox[s + 4])
            guard len < 1 << 20 else { end(); return }
            guard inbox.count >= 5 + len else { return }
            let type = inbox[s]
            let body = inbox.subdata(in: s + 5 ..< s + 5 + len)
            inbox.removeSubrange(s ..< s + 5 + len)
            handle(type, body)
        }
    }

    private func handle(_ type: UInt8, _ body: Data) {
        let json = (try? JSONSerialization.jsonObject(with: body)) as? [String: Any] ?? [:]
        if !authed {
            guard type == Wire.hello, let t = json["token"] as? String, same(t, secret) else {
                ended = true
                conn.send(content: Wire.pack(Wire.refused, try! JSONSerialization.data(withJSONObject: ["error": "token"])), completion: .contentProcessed { [conn] _ in conn.cancel() })
                return
            }
            authed = true
            let wantCurtain = json["curtain"] as? Bool ?? true
            curtained = wantCurtain
            acks = json["acks"] as? Bool ?? false
            take(json)
            DispatchQueue.main.async { [self] in
                if let old = Session.live { old.q.async { old.end() } }
                Session.live = self
                start(curtain: wantCurtain)
            }
            return
        }
        switch type {
        case Wire.input: input.handle(json)
        case Wire.keyframe: needKey = true; reencodeLast()
        case Wire.settings:
            take(json)
            if rate < lowest { rate = lowest; if let encoder { apply(rate, to: encoder) } }
            resize(pick())
        case Wire.got:
            guard !unacked.isEmpty else { return }
            let f = unacked.removeFirst()
            let age = DispatchTime.now().uptimeNanoseconds - f.sent
            ackCount += 1; ackSum += age; ackMax = max(ackMax, age)
            unackedBytes -= f.bytes
            delivered += f.bytes
            if behind && !held { behind = false; reencodeLast() }
        default: break
        }
    }

    private func take(_ json: [String: Any]) {
        pinned = json["width"] as? Int ?? 0
        prefer = json["prefer"] as? String ?? "fps"
    }

    private var lowest: Int { prefer == "quality" ? 20_000_000 : Session.floorRate }

    /// The pinned width, or in auto the widest the encoder keeps near 120 fps ("fps") or simply the widest ("quality").
    private func pick() -> Int {
        let fit = Session.widths.filter { $0 <= full.0 }
        if pinned > 0 || fit.isEmpty { return min(pinned > 0 ? pinned : full.0, full.0) / 2 * 2 }
        return prefer == "quality" ? fit.last! : fit.last { estimate($0) >= 115 } ?? fit[0]
    }

    private func height(_ w: Int) -> Int { full.1 * w / full.0 / 2 * 2 }
    private func estimate(_ w: Int) -> Int { min(120, Int(1e9 / (Session.encodeNs[w] ?? 9_000_000))) }

    /// On `q`, live: the capture and the encoder change width together, the iPad hears the new size, and the keyframe after carries
    /// the new parameter sets. Frames of the old size still on their way are dropped in encode.
    private func resize(_ w: Int) {
        guard let stream, let cfg, w != size.0 else { return }
        let h = height(w)
        cfg.width = w; cfg.height = h
        stream.updateConfiguration(cfg) { if let e = $0 { print("resize failed: \(e)") } }
        if let old = encoder { encodeQ.async { VTCompressionSessionInvalidate(old) } }
        encoder = makeEncoder(w, h)
        size = (w, h)
        needKey = true
        clear = 0
        send(Wire.info, try! JSONSerialization.data(withJSONObject: ["w": w, "h": h, "locked": locked ?? false]))
        print("resize \(w)x\(h)")
    }

    private func same(_ a: String, _ b: String) -> Bool {
        let x = Array(a.utf8), y = Array(b.utf8)
        guard x.count == y.count else { return false }
        return zip(x, y).reduce(0) { $0 | ($1.0 ^ $1.1) } == 0
    }

    private func send(_ type: UInt8, _ payload: Data) {
        inFlight += payload.count
        conn.send(content: Wire.pack(type, payload), completion: .contentProcessed { [self] _ in
            q.async {
                self.inFlight -= payload.count
                if self.behind && !self.held { self.behind = false; self.reencodeLast() }
            }
        })
    }

    // MARK: the screen

    /// Main thread: wake the display and keep it awake while he is here (a sleeping display locks this Mac), switch every other display
    /// off so all windows are where the iPad can see them, lay the curtain, then capture.
    private func start(curtain wantCurtain: Bool) {
        var poke: IOPMAssertionID = 0
        IOPMAssertionDeclareUserActivity("Jarvis Remote" as CFString, kIOPMUserActiveLocal, &poke)
        IOPMAssertionCreateWithName(kIOPMAssertionTypePreventUserIdleDisplaySleep as CFString, IOPMAssertionLevel(kIOPMAssertionLevelOn), "Jarvis Remote" as CFString, &awake)
        activity = ProcessInfo.processInfo.beginActivity(options: [.userInitiated, .latencyCritical, .idleDisplaySleepDisabled], reason: "Jarvis Remote streams the screen")
        let folded = Displays.fold()
        // the windows take a moment to move and the display list to settle before the curtain and the capture look at it
        DispatchQueue.main.asyncAfter(deadline: .now() + (folded ? 2 : 0)) { [self] in
            if wantCurtain { Curtain.lay() }
            Task { await capture() }
        }
    }

    private func capture() async {
        print("session from \(conn.endpoint)")
        do {
            // The whole display, nothing left out: a filter that leaves windows out makes the window server compose a second picture
            // every frame, and the encoder waited on it (~10 ms a frame). The curtain is the panel's gamma, after composition.
            let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
            guard let display = content.displays.first(where: { $0.displayID == CGMainDisplayID() }) ?? content.displays.first else { return }
            let filter = SCContentFilter(display: display, excludingWindows: [])
            let mode = CGDisplayCopyDisplayMode(display.displayID)
            let (w, h) = q.sync {
                full = (mode?.pixelWidth ?? display.width * 2, mode?.pixelHeight ?? display.height * 2)
                rate = max(rate, lowest)
                let w = pick()
                return (w, height(w))
            }

            let cfg = SCStreamConfiguration()
            cfg.width = w; cfg.height = h
            cfg.minimumFrameInterval = CMTime(value: 1, timescale: 120)   // his iPad and this display are both ProMotion
            cfg.pixelFormat = kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange
            cfg.queueDepth = 4
            cfg.showsCursor = true
            cfg.colorSpaceName = CGColorSpace.sRGB

            q.sync {
                encoder = makeEncoder(w, h)
                size = (w, h)
                self.cfg = cfg
                watchLock()
            }
            let s = SCStream(filter: filter, configuration: cfg, delegate: self)
            try s.addStreamOutput(self, type: .screen, sampleHandlerQueue: q)
            try await s.startCapture()
            q.sync { if ended { s.stopCapture() } else { stream = s } }
        } catch {
            print("capture failed: \(error)")
            q.async { self.end() }
        }
    }

    /// The iPad offers 解锁 only while this Mac is locked, so it hears each change.
    private func watchLock() {
        let t = DispatchSource.makeTimerSource(queue: q)
        t.schedule(deadline: .now(), repeating: 1)
        t.setEventHandler { [self] in
            adapt()
            if curtained { Curtain.lay() }   // display changes and ColorSync (Night Shift, True Tone) can put the curve back
            let now = (CGSessionCopyCurrentDictionary() as? [String: Any])?["CGSSessionScreenIsLocked"] as? Bool ?? false
            guard now != locked else { return }
            locked = now
            send(Wire.info, try! JSONSerialization.data(withJSONObject: ["w": size.0, "h": size.1, "locked": now]))
        }
        t.resume()
        lockWatch = t
    }

    /// Once a second: a second with stalls cuts the bitrate, three clear seconds raise it. The quantiser cap that keeps text sharp
    /// would override a low bitrate, so it holds only while the link can afford it.
    private func adapt() {
        let was = rate
        // one line a second while the screen moves: what the capture gave, what went, how much, and how often the link was full
        if captured > 0 {
            print("1s: captured \(captured) sent \(sentFrames) \(sentBytes * 8 / 1_000_000) Mbit delivered \(delivered * 8 / 1_000_000) Mbit stalls \(stalls) unacked \(unackedBytes / 1000) kB rate \(rate / 1_000_000) ack avg \(ackCount > 0 ? ackSum / UInt64(ackCount) / 1_000_000 : 0) max \(ackMax / 1_000_000) ms encode \(encodeBusy / 1_000_000) ms")
        }
        if sentFrames >= 30, let was = Session.encodeNs[size.0] { Session.encodeNs[size.0] = was * 0.8 + Double(encodeBusy) / Double(sentFrames) * 0.2 }
        // the iPad's panel: frames that went, the width, the bitrate, how long a frame took to be acked, and each width's fps estimate
        if size.0 > 0 {
            let est = Dictionary(uniqueKeysWithValues: Session.widths.filter { $0 <= full.0 }.map { (String($0), estimate($0)) })
            send(Wire.stats, try! JSONSerialization.data(withJSONObject: ["fps": sentFrames, "w": size.0, "mbps": sentBytes * 8 / 1_000_000,
                                                                          "ms": ackCount > 0 ? Int(ackSum / UInt64(ackCount) / 1_000_000) : 0, "est": est]))
        }
        // stalls while the link carried well under the bitrate are the link stuttering, not too little room: a lower bitrate cures nothing
        let roomy = acks && delivered * 8 < rate * 6 / 10
        // auto, "fps": a link that still stalls at a low bitrate gets a narrower stream; ten clear seconds with room try the next wider
        if pinned == 0 && prefer == "fps", let i = Session.widths.firstIndex(of: size.0) {
            if stalls > 0 && !roomy && rate <= 12_000_000 && i > 0 { resize(Session.widths[i - 1]) }
            else if clear >= 10 && rate >= 20_000_000 && i + 1 < Session.widths.count && Session.widths[i + 1] <= pick() { resize(Session.widths[i + 1]) }
        }
        captured = 0; sentFrames = 0; sentBytes = 0; delivered = 0; ackCount = 0; ackSum = 0; ackMax = 0; encodeBusy = 0
        if stalls > 0 {
            if !roomy { rate = max(rate * 2 / 3, lowest) }
            clear = 0
        } else {
            clear += 1
            if clear >= 3 { rate = min(rate * 115 / 100, Session.topRate) }
        }
        stalls = 0
        guard rate != was, let encoder else { return }
        apply(rate, to: encoder)
        print("bitrate \(rate / 1_000_000) Mbit/s")
    }

    private func apply(_ rate: Int, to s: VTCompressionSession) {
        VTSessionSetProperty(s, key: kVTCompressionPropertyKey_AverageBitRate, value: rate as CFNumber)
        VTSessionSetProperty(s, key: kVTCompressionPropertyKey_MaxAllowedFrameQP, value: (rate >= Session.sharpRate ? 20 : 51) as CFNumber)
    }

    private func makeEncoder(_ w: Int, _ h: Int) -> VTCompressionSession? {
        var s: VTCompressionSession?
        let spec = [kVTVideoEncoderSpecification_EnableLowLatencyRateControl: true] as CFDictionary
        VTCompressionSessionCreate(allocator: nil, width: Int32(w), height: Int32(h), codecType: kCMVideoCodecType_HEVC,
                                   encoderSpecification: spec, imageBufferAttributes: nil, compressedDataAllocator: nil,
                                   outputCallback: nil, refcon: nil, compressionSessionOut: &s)
        guard let s else { return nil }
        VTSessionSetProperty(s, key: kVTCompressionPropertyKey_RealTime, value: kCFBooleanTrue)
        VTSessionSetProperty(s, key: kVTCompressionPropertyKey_PrioritizeEncodingSpeedOverQuality, value: kCFBooleanTrue)
        VTSessionSetProperty(s, key: kVTCompressionPropertyKey_ProfileLevel, value: kVTProfileLevel_HEVC_Main_AutoLevel)
        VTSessionSetProperty(s, key: kVTCompressionPropertyKey_AllowFrameReordering, value: kCFBooleanFalse)
        // the average is only a ceiling: on text the encoder spends far less and lets detail go soft; a cap on the quantiser keeps it sharp
        apply(rate, to: s)
        VTSessionSetProperty(s, key: kVTCompressionPropertyKey_ExpectedFrameRate, value: 120 as CFNumber)
        // the capture is sRGB-converted BT.709 video range: say so, or the iPad guesses and the colours wash out
        VTSessionSetProperty(s, key: kVTCompressionPropertyKey_ColorPrimaries, value: kCVImageBufferColorPrimaries_ITU_R_709_2)
        VTSessionSetProperty(s, key: kVTCompressionPropertyKey_TransferFunction, value: kCVImageBufferTransferFunction_ITU_R_709_2)
        VTSessionSetProperty(s, key: kVTCompressionPropertyKey_YCbCrMatrix, value: kCVImageBufferYCbCrMatrix_ITU_R_709_2)
        VTSessionSetProperty(s, key: kVTCompressionPropertyKey_MaxKeyFrameInterval, value: 600 as CFNumber)
        VTCompressionSessionPrepareToEncodeFrames(s)
        return s
    }

    func stream(_ s: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .screen, sb.isValid,
              let info = (CMSampleBufferGetSampleAttachmentsArray(sb, createIfNecessary: false) as? [[SCStreamFrameInfo: Any]])?.first,
              let raw = info[.status] as? Int, SCFrameStatus(rawValue: raw) == .complete,
              let pixels = sb.imageBuffer else { return }
        lastPixels = pixels
        captured += 1
        if held { behind = true; stalls += 1; return }
        encode(pixels, at: sb.presentationTimeStamp)
    }

    /// The screen only sends frames when it changes, so a keyframe asked for on a still screen comes from the last one.
    private func reencodeLast() {
        if let p = lastPixels { encode(p, at: CMClockGetTime(CMClockGetHostTimeClock())) }
    }

    private func encode(_ pixels: CVPixelBuffer, at t: CMTime) {
        guard let encoder, !ended, CVPixelBufferGetWidth(pixels) == size.0 else { return }
        let props = needKey ? [kVTEncodeFrameOptionKey_ForceKeyFrame: true] as CFDictionary : nil
        if encoding { missed = true; return }
        encoding = true
        needKey = false
        encodeQ.async { [self] in
            let t0 = DispatchTime.now().uptimeNanoseconds
            VTCompressionSessionEncodeFrame(encoder, imageBuffer: pixels, presentationTimeStamp: t, duration: .invalid,
                                            frameProperties: props, infoFlagsOut: nil) { [self] status, _, out in
                guard status == noErr, let out else { return }
                q.async { self.ship(out) }
            }
            let dt = DispatchTime.now().uptimeNanoseconds - t0
            q.async { [self] in
                encodeBusy += dt
                encoding = false
                if missed { missed = false; reencodeLast() }
            }
        }
    }

    /// Every encoded frame goes: a link that falls behind holds back the capture instead (above), so no frame is missing from the chain.
    private func ship(_ sb: CMSampleBuffer) {
        guard !ended, let block = sb.dataBuffer else { return }
        let att = (CMSampleBufferGetSampleAttachmentsArray(sb, createIfNecessary: false) as? [[CFString: Any]])?.first
        let key = !(att?[kCMSampleAttachmentKey_NotSync] as? Bool ?? false)
        if key, let fmt = sb.formatDescription {
            var params = Data(), count = 0
            CMVideoFormatDescriptionGetHEVCParameterSetAtIndex(fmt, parameterSetIndex: 0, parameterSetPointerOut: nil, parameterSetSizeOut: nil, parameterSetCountOut: &count, nalUnitHeaderLengthOut: nil)
            for i in 0 ..< count {
                var p: UnsafePointer<UInt8>?, n = 0
                CMVideoFormatDescriptionGetHEVCParameterSetAtIndex(fmt, parameterSetIndex: i, parameterSetPointerOut: &p, parameterSetSizeOut: &n, parameterSetCountOut: nil, nalUnitHeaderLengthOut: nil)
                guard let p else { continue }
                withUnsafeBytes(of: UInt16(n).bigEndian) { params.append(contentsOf: $0) }
                params.append(p, count: n)
            }
            send(Wire.params, params)
        }
        var payload = Data([key ? 1 : 0])
        let n = CMBlockBufferGetDataLength(block)
        payload.append(Data(count: n))
        payload.withUnsafeMutableBytes { _ = CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: n, destination: $0.baseAddress! + 1) }
        sentFrames += 1
        sentBytes += payload.count
        if acks { unacked.append((payload.count, DispatchTime.now().uptimeNanoseconds)); unackedBytes += payload.count }
        send(Wire.frame, payload)
    }

    func stream(_ s: SCStream, didStopWithError error: Error) {
        print("stream stopped: \(error)")
        q.async { self.end() }
    }

    // MARK: the end

    /// Lift the curtain and let the display sleep now, which locks this Mac again (his lock is "immediately").
    func end() {
        guard !ended else { return }
        ended = true
        stream?.stopCapture()
        lockWatch?.cancel()
        if let e = encoder { encodeQ.sync { VTCompressionSessionInvalidate(e) } }
        conn.cancel()
        guard authed else { return }
        let awake = awake
        DispatchQueue.main.async { [self] in
            if awake != 0 { IOPMAssertionRelease(awake) }
            if let activity { ProcessInfo.processInfo.endActivity(activity) }
            guard Session.live === self else { return }
            Session.live = nil
            Curtain.lift()
            Displays.unfold()
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/usr/bin/pmset")
            p.arguments = ["displaysleepnow"]
            try? p.run()
        }
    }
}

/// The other displays mirror the main one while he is here, so the windows on them come over to the one the iPad sees.
/// For this app only: if it dies, macOS puts the displays back by itself.
enum Displays {
    private static var folded: [CGDirectDisplayID] = []

    private static var mirrored = false

    /// Switched off, as if unplugged, so their windows come to the main display (Jump Desktop does the same); mirroring them instead
    /// cost a scaled 4K composite every frame, so it is only the fallback. Both are for this app only and undo themselves if it dies.
    static func fold() -> Bool {
        var ids = [CGDirectDisplayID](repeating: 0, count: 16), n: UInt32 = 0
        CGGetOnlineDisplayList(16, &ids, &n)
        let main = CGMainDisplayID()
        let others = ids.prefix(Int(n)).filter { $0 != main && CGDisplayMirrorsDisplay($0) == kCGNullDirectDisplay }
        guard !others.isEmpty else { return false }
        var cfg: CGDisplayConfigRef?
        guard CGBeginDisplayConfiguration(&cfg) == .success else { return false }
        others.forEach { _ = CGSConfigureDisplayEnabled(cfg, $0, false) }
        var done = CGCompleteDisplayConfiguration(cfg, .forAppOnly)
        mirrored = done != .success
        if mirrored, CGBeginDisplayConfiguration(&cfg) == .success {
            others.forEach { CGConfigureDisplayMirrorOfDisplay(cfg, $0, main) }
            done = CGCompleteDisplayConfiguration(cfg, .forAppOnly)
        }
        print("\(mirrored ? "mirror" : "switch off") \(others.count) other display(s): \(done.rawValue)")
        guard done == .success else { return false }
        folded = Array(others)
        return true
    }

    static func unfold() {
        guard !folded.isEmpty else { return }
        var cfg: CGDisplayConfigRef?
        guard CGBeginDisplayConfiguration(&cfg) == .success else { return }
        for d in folded {
            if mirrored { CGConfigureDisplayMirrorOfDisplay(cfg, d, kCGNullDirectDisplay) } else { _ = CGSConfigureDisplayEnabled(cfg, d, true) }
        }
        CGCompleteDisplayConfiguration(cfg, .forAppOnly)
        folded = []
    }
}

// SkyLight's display switch, private (CoreGraphics has none); Jump Desktop Connect calls it to switch physical displays off.
@_silgen_name("CGSConfigureDisplayEnabled")
func CGSConfigureDisplayEnabled(_ config: CGDisplayConfigRef?, _ display: CGDirectDisplayID, _ enabled: Bool) -> CGError

enum Curtain {
    /// Every panel goes black through its gamma curve (Jump Desktop Connect's values). Gamma applies after the screen is composed,
    /// so the capture still sees the desktop; Quartz restores the curves itself if this process dies.
    static func lay() {
        var ids = [CGDirectDisplayID](repeating: 0, count: 16), n: UInt32 = 0
        CGGetOnlineDisplayList(16, &ids, &n)
        ids.prefix(Int(n)).forEach { CGSetDisplayTransferByFormula($0, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01) }
    }

    static func lift() { CGDisplayRestoreColorSyncSettings() }
}
