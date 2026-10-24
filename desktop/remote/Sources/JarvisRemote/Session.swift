import AppKit
import Network
import ScreenCaptureKit
import VideoToolbox
import IOKit.pwr_mgt

// The wire, both ways: [type: UInt8][length: UInt32 big-endian][payload]. The iPad's RemoteWire.swift mirrors it.
enum Wire {
    // iPad → Mac
    static let hello: UInt8 = 0x01      // JSON {"token", "curtain": Bool}
    static let input: UInt8 = 0x02      // JSON, see Input.handle
    static let keyframe: UInt8 = 0x03   // empty: the decoder lost its place
    // Mac → iPad
    static let info: UInt8 = 0x10       // JSON {"w", "h", "locked"}: the stream's pixel size
    static let params: UInt8 = 0x11     // HEVC parameter sets (VPS, SPS, PPS), each [UInt16 big-endian length][bytes]
    static let frame: UInt8 = 0x12      // [1 = keyframe][AVCC NAL units]
    static let refused: UInt8 = 0x13    // JSON {"error"}

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
    private let q = DispatchQueue(label: "jarvis.remote.session")
    private var inbox = Data()
    private var authed = false
    private var ended = false

    private var stream: SCStream?
    private var encoder: VTCompressionSession?
    private var lastPixels: CVPixelBuffer?
    private var needKey = true
    private var inFlight = 0            // bytes handed to the connection and not yet taken by TCP
    private var behind = false          // a frame came while the link was full; the newest goes once it drains
    // ~0.15 s of video at the bitrate: past this the link is behind, and new screens wait instead of queueing
    private static let fullLink = 1_500_000
    private var size = (0, 0)
    private var locked: Bool?
    private var lockWatch: DispatchSourceTimer?
    // the bitrate follows the link: down by a third in a second the link fell behind, up a little after three clear ones
    private static let topRate = 150_000_000, floorRate = 4_000_000, sharpRate = 60_000_000
    private var rate = Session.topRate
    private var stalls = 0
    private var clear = 0

    private var curtain: [NSWindow] = []
    private var awake: IOPMAssertionID = 0
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
        default: break
        }
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
                if self.behind && self.inFlight < Session.fullLink { self.behind = false; self.reencodeLast() }
            }
        })
    }

    // MARK: the screen

    /// Main thread: wake the display and keep it awake while he is here (a sleeping display locks this Mac), fold every other display
    /// into the main one so all windows are where the iPad can see them, lay the curtain, then capture.
    private func start(curtain wantCurtain: Bool) {
        var poke: IOPMAssertionID = 0
        IOPMAssertionDeclareUserActivity("Jarvis Remote" as CFString, kIOPMUserActiveLocal, &poke)
        IOPMAssertionCreateWithName(kIOPMAssertionTypePreventUserIdleDisplaySleep as CFString, IOPMAssertionLevel(kIOPMAssertionLevelOn), "Jarvis Remote" as CFString, &awake)
        let folded = Displays.fold()
        // the windows take a moment to move and the display list to settle before the curtain and the capture look at it
        DispatchQueue.main.asyncAfter(deadline: .now() + (folded ? 2 : 0)) { [self] in
            if wantCurtain { curtain = Curtain.lay() }
            Task { await capture() }
        }
    }

    private func capture() async {
        print("session from \(conn.endpoint)")
        do {
            // The curtain is this app's only windows: the stream leaves out the whole app, so the Mac shows black and the iPad the desktop.
            // A just-ordered window can take a moment to reach the shareable list.
            var content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
            for _ in 0 ..< 10 where !curtain.isEmpty && !content.applications.contains(where: { $0.processID == getpid() }) {
                try await Task.sleep(nanoseconds: 100_000_000)
                content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
            }
            guard let display = content.displays.first(where: { $0.displayID == CGMainDisplayID() }) ?? content.displays.first else { return }
            let me = content.applications.filter { $0.processID == getpid() }
            let filter = SCContentFilter(display: display, excludingApplications: me, exceptingWindows: [])
            let mode = CGDisplayCopyDisplayMode(display.displayID)
            let w = mode?.pixelWidth ?? display.width * 2, h = mode?.pixelHeight ?? display.height * 2

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
        if stalls > 0 {
            rate = max(rate * 2 / 3, Session.floorRate)
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
        if inFlight >= Session.fullLink { behind = true; stalls += 1; return }
        encode(pixels, at: sb.presentationTimeStamp)
    }

    /// The screen only sends frames when it changes, so a keyframe asked for on a still screen comes from the last one.
    private func reencodeLast() {
        if let p = lastPixels { encode(p, at: CMClockGetTime(CMClockGetHostTimeClock())) }
    }

    private func encode(_ pixels: CVPixelBuffer, at t: CMTime) {
        guard let encoder, !ended else { return }
        let props = needKey ? [kVTEncodeFrameOptionKey_ForceKeyFrame: true] as CFDictionary : nil
        needKey = false
        VTCompressionSessionEncodeFrame(encoder, imageBuffer: pixels, presentationTimeStamp: t, duration: .invalid,
                                        frameProperties: props, infoFlagsOut: nil) { [self] status, _, out in
            guard status == noErr, let out else { return }
            q.async { self.ship(out) }
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
        if let e = encoder { VTCompressionSessionInvalidate(e) }
        conn.cancel()
        guard authed else { return }
        let awake = awake
        DispatchQueue.main.async { [self] in
            curtain.forEach { $0.close() }
            if awake != 0 { IOPMAssertionRelease(awake) }
            guard Session.live === self else { return }
            Session.live = nil
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

    static func fold() -> Bool {
        var ids = [CGDirectDisplayID](repeating: 0, count: 16), n: UInt32 = 0
        CGGetOnlineDisplayList(16, &ids, &n)
        let main = CGMainDisplayID()
        let others = ids.prefix(Int(n)).filter { $0 != main && CGDisplayMirrorsDisplay($0) == kCGNullDirectDisplay }
        guard !others.isEmpty else { return false }
        var cfg: CGDisplayConfigRef?
        guard CGBeginDisplayConfiguration(&cfg) == .success else { return false }
        others.forEach { CGConfigureDisplayMirrorOfDisplay(cfg, $0, main) }
        let done = CGCompleteDisplayConfiguration(cfg, .forAppOnly)
        print("fold \(others.count) display(s) into the main one: \(done.rawValue)")
        guard done == .success else { return false }
        folded = Array(others)
        return true
    }

    static func unfold() {
        guard !folded.isEmpty else { return }
        var cfg: CGDisplayConfigRef?
        guard CGBeginDisplayConfiguration(&cfg) == .success else { return }
        folded.forEach { CGConfigureDisplayMirrorOfDisplay(cfg, $0, kCGNullDirectDisplay) }
        CGCompleteDisplayConfiguration(cfg, .forAppOnly)
        folded = []
    }
}

enum Curtain {
    /// A black window above everything on every display, letting every click through.
    static func lay() -> [NSWindow] {
        NSScreen.screens.map { screen in
            let w = NSWindow(contentRect: screen.frame, styleMask: .borderless, backing: .buffered, defer: false)
            w.backgroundColor = .black
            w.level = NSWindow.Level(rawValue: Int(CGShieldingWindowLevel()))
            w.ignoresMouseEvents = true
            w.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary]
            w.isReleasedWhenClosed = false
            w.setFrame(screen.frame, display: true)
            w.orderFrontRegardless()
            return w
        }
    }
}
