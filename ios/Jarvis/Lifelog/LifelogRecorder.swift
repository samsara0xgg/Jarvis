import AVFoundation
import CallKit
import Foundation
import Observation
import UIKit
import WidgetKit
import os

// His recorded day (ADR 0238), the recording side: the microphone as it is, raw (no noise suppression, no gain control, no silence
// trimming), written in pieces of 10 minutes of wall clock to the outbox (LifelogStore). Three parts:
//   LifelogRecorder  the switch, interruptions, termination; what the UI reads (@MainActor)
//   LifelogMic       the recorder's own engine, used while the voice is not running; the UI cues play through it
//   LifelogWriter    one serial queue: converts to stereo 48 kHz, writes the file (LifelogOgg.swift), cuts pieces, closes them into the outbox
// During a conversation the voice's engine owns the microphone (voice processing, mono) and feeds the same writer from its tap, so
// a piece spans conversations. Each hand-over costs a gap in the audio (the old engine stops before the new one is built): short for
// the recorder's own engine, up to a second or so while the voice settles its input.
// At home, while the Mac records him, the writer keeps nothing (LifelogGate, LifelogCoverage.swift, ADR 0292); the engine and the session run as ever.
// A hand-over, or a rebuild after the hardware changed, keeps the app awake until the recorder's engine runs: iOS freezes a background app
// with no audio running within seconds, and a frozen app may not start the microphone when it wakes. If the audio stops anyway, he is told
// (LifelogAlarm, ADR 0375).

enum LifelogClock {
    /// The first 10-minute mark of the local clock (:00, :10, :20 ...) after `ms`, in UTC milliseconds.
    static func nextBoundary(after ms: Int64, offsetSec: Int = TimeZone.current.secondsFromGMT()) -> Int64 {
        let step: Int64 = 600_000, off = Int64(offsetSec) * 1000
        return ((ms + off) / step + 1) * step - off
    }
    static func nowMs() -> Int64 { Int64((Date().timeIntervalSince1970 * 1000).rounded()) }
}

/// Anything the microphone gives (mono at any rate, stereo 48 kHz) to the file's processing format: stereo, 48 kHz, Float32, non-interleaved.
/// Mono is copied to both channels.
final class LifelogResampler {
    private var conv: AVAudioConverter?
    func convert(_ b: AVAudioPCMBuffer) -> AVAudioPCMBuffer? {
        let out = LifelogWriter.format
        if b.format == out { return b }
        if conv?.inputFormat != b.format { conv = AVAudioConverter(from: b.format, to: out) }
        guard let conv else { return nil }
        return Jarvis.convert(b, using: conv)
    }
}

/// HE-AAC in m4a through AVAudioFile: the fallback, finished only when closed.
final class AACSink: LifelogSink {
    private var file: AVAudioFile?
    init(url: URL, format: AudioFormatID) throws {
        file = try AVAudioFile(forWriting: url, settings: [AVFormatIDKey: format, AVSampleRateKey: 48_000, AVNumberOfChannelsKey: 2, AVEncoderBitRateKey: 48_000],
                               commonFormat: .pcmFormatFloat32, interleaved: false)
    }
    func write(_ b: AVAudioPCMBuffer) throws { try file?.write(from: b) }
    func finish() { file = nil }                            // releasing it finishes the file
}

final class LifelogWriter: @unchecked Sendable {
    static let shared = LifelogWriter()
    static let rate = 48_000.0
    static let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: rate, channels: 2, interleaved: false)!

    /// a piece opened (its first sample's time) or closed (nil); called off the main thread
    var onPiece: (@Sendable (Date?) -> Void)?
    /// nothing is written while this is false
    private let accepting = OSAllocatedUnfairLock(initialState: false)
    func setAccepting(_ on: Bool) { accepting.withLock { $0 = on } }

    /// Nothing is kept until this moment (UTC ms), 0 for no such moment (ADR 0292: the Mac records where he is). A decision not renewed
    /// lapses by itself, so a gate that stopped asking never leaves the day unrecorded.
    private let dropUntilMs = OSAllocatedUnfairLock<Int64>(initialState: 0)
    var dropping: Bool { dropUntilMs.withLock { $0 } > LifelogClock.nowMs() }
    /// Stops keeping audio until `until` (nil: keeps it). The piece being written is closed at once when the audio stops being kept, so a piece never
    /// mixes kept and dropped time.
    func setDropping(until: Int64?) {
        let was = dropping
        dropUntilMs.withLock { $0 = until ?? 0 }
        if !was && dropping { close() }
    }

    private let queue = DispatchQueue(label: "jarvis.lifelog.writer", qos: .userInitiated)
    // everything below on `queue` only
    private var sink: LifelogSink?
    private var piece = LifelogPiece(id: "", startedMs: 0, durationMs: 0, ext: "ogg")
    private var frames: Int64 = 0
    private var boundaryMs: Int64 = 0
    private var holdoffMs: Int64 = 0
    private var formatIndex = 0
    private var resampler = LifelogResampler()

    /// Called on the audio thread: copies the buffer and returns; the rest happens on the writer's queue.
    func feed(_ b: AVAudioPCMBuffer) {
        guard accepting.withLock({ $0 }) else { return }
        LifelogAlarm.heard()                                // the microphone is alive, whether this audio is kept or not
        guard !dropping, let src = b.floatChannelData,
              let c = AVAudioPCMBuffer(pcmFormat: b.format, frameCapacity: b.frameLength) else { return }
        c.frameLength = b.frameLength
        for ch in 0..<Int(b.format.channelCount) { c.floatChannelData![ch].update(from: src[ch], count: Int(b.frameLength)) }
        queue.async { self.write(c) }
    }

    /// Closes the piece now; the next buffer opens another.
    func close() { queue.async { self.closeNow() } }
    /// For the app's termination: returns when the piece is in the outbox.
    func closeSync() { queue.sync { closeNow() } }

    private func write(_ b: AVAudioPCMBuffer) {
        guard accepting.withLock({ $0 }), !dropping else { return }
        let now = LifelogClock.nowMs()
        if sink != nil, now >= boundaryMs { closeNow() }
        if sink == nil {
            guard now >= holdoffMs else { return }
            // the buffer arrives after its samples were taken
            guard open(startedMs: now - Int64(Double(b.frameLength) / b.format.sampleRate * 1000)) else { holdoffMs = now + 5_000; return }
        }
        guard let out = resampler.convert(b) else { return }
        do {
            try sink?.write(out)
            frames += Int64(out.frameLength)
        } catch {
            lifelogNote("write failed: \(error.localizedDescription)")
            closeNow()
            holdoffMs = now + 5_000
        }
    }

    /// Ogg Opus first (written by us, page by page, so a killed app leaves a playable file), then HE-AAC v2 and HE-AAC in m4a through
    /// AVAudioFile if the Opus encoder cannot be made (the simulator has no v2 encoder): stereo at about 48 kbps.
    static let formats: [AudioFormatID] = [kAudioFormatOpus, kAudioFormatMPEG4AAC_HE_V2, kAudioFormatMPEG4AAC_HE]

    static func create(in dir: URL, id: String, format: AudioFormatID) throws -> (LifelogSink, ext: String) {
        if format == kAudioFormatOpus { return (try OggOpusSink(url: dir.appendingPathComponent("\(id).ogg")), "ogg") }
        return (try AACSink(url: dir.appendingPathComponent("\(id).m4a"), format: format), "m4a")
    }

    private func open(startedMs: Int64) -> Bool {
        let dir = LifelogStore.ensure(LifelogStore.openDir)
        let id = UUID().uuidString
        var made: (LifelogSink, ext: String)?
        while made == nil, formatIndex < Self.formats.count {
            do { made = try Self.create(in: dir, id: id, format: Self.formats[formatIndex]) } catch {
                lifelogNote("file format \(formatIndex) could not be created: \(error.localizedDescription)")
                try? FileManager.default.removeItem(at: dir.appendingPathComponent("\(id).ogg"))
                formatIndex += 1
            }
        }
        guard let (f, ext) = made else { lifelogNote("no file could be created"); formatIndex = 0; return false }
        piece = LifelogPiece(id: id, startedMs: startedMs, durationMs: 0, ext: ext)
        // what a launch after a kill needs to close this file as a piece
        try? JSONEncoder().encode(piece).write(to: dir.appendingPathComponent("\(id).json"), options: .atomic)
        sink = f
        frames = 0
        boundaryMs = LifelogClock.nextBoundary(after: LifelogClock.nowMs())   // not after startedMs: that can precede the mark that just cut the last piece
        resampler = LifelogResampler()
        onPiece?(Date(timeIntervalSince1970: Double(startedMs) / 1000))
        return true
    }

    private func closeNow() {
        guard let f = sink else { return }
        sink = nil
        f.finish()
        piece.durationMs = frames * 1000 / Int64(Self.rate)
        Self.finish(piece, dir: LifelogStore.openDir)
        lifelogNote("piece \(piece.id.prefix(8)) closed, \(piece.durationMs / 1000) s \(piece.ext)")
        onPiece?(nil)
    }

    /// Moves a piece from `dir` into the outbox and counts it; a piece with no audio in it goes away.
    private static func finish(_ p: LifelogPiece, dir: URL) {
        let fm = FileManager.default
        let from = dir.appendingPathComponent("\(p.id).\(p.ext)"), meta = dir.appendingPathComponent("\(p.id).json")
        defer { try? fm.removeItem(at: meta) }
        guard p.durationMs > 0 else { try? fm.removeItem(at: from); return }
        LifelogStore.ensure()
        guard (try? fm.moveItem(at: from, to: p.file)) != nil, (try? p.save()) != nil else { return }
        LifelogStore.addClosed(ms: p.durationMs)
    }

    /// The length of what a file left open holds, nil if it holds none. An Ogg is cut back to its last complete page; an m4a must read as it is.
    private static func readable(_ url: URL) -> Int64? {
        if url.pathExtension == "ogg" {
            guard let d = try? Data(contentsOf: url) else { return nil }
            let s = OggOpus.scan([UInt8](d))
            guard s.durationMs > 0, let h = try? FileHandle(forWritingTo: url) else { return nil }
            try? h.truncate(atOffset: UInt64(s.validLength))
            try? h.close()
            return s.durationMs
        }
        guard let f = try? AVAudioFile(forReading: url), f.length > 0 else { return nil }
        return f.length * 1000 / Int64(f.processingFormat.sampleRate)
    }

    /// At launch: what a killed app left in open/ is closed if it reads, else held in held/broken (never deleted).
    func recover() {
        queue.async {
            let fm = FileManager.default, dir = LifelogStore.openDir
            var any = false
            for name in (try? fm.contentsOfDirectory(atPath: dir.path)) ?? [] where !name.hasSuffix(".json") {
                let audio = dir.appendingPathComponent(name)
                let id = audio.deletingPathExtension().lastPathComponent
                let meta = dir.appendingPathComponent("\(id).json")
                if var p = (try? Data(contentsOf: meta)).flatMap({ try? JSONDecoder().decode(LifelogPiece.self, from: $0) }),
                   p.ext == audio.pathExtension, let ms = Self.readable(audio) {
                    p.durationMs = ms
                    Self.finish(p, dir: dir)
                    lifelogNote("recovered piece \(id.prefix(8)), \(p.durationMs / 1000) s")
                    any = true
                } else {
                    let held = LifelogStore.ensure(LifelogStore.heldDir("broken"))
                    try? fm.moveItem(at: audio, to: held.appendingPathComponent(name))
                    try? fm.moveItem(at: meta, to: held.appendingPathComponent("\(id).json"))
                    lifelogNote("piece \(id.prefix(8)) left open could not be read, held in broken/")
                }
            }
            if any { self.onPiece?(nil) }
        }
    }
}

/// The recorder's own engine: no voice processing, its own mode on the shared session. It runs while the switch is on, the voice is
/// not running and no interruption is on. Everything on `queue`; `flags` are what the rest of the app sets.
final class LifelogMic: @unchecked Sendable {
    static let shared = LifelogMic()
    private struct Flags { var on = false, interrupted = false, voice = false, foreground = true }
    private let flags = OSAllocatedUnfairLock(initialState: Flags())
    private let queue = DispatchQueue(label: "jarvis.lifelog.mic")
    private var engine: AVAudioEngine?
    private var observer: NSObjectProtocol?
    private var retrying = false
    /// starts that failed in a row while the app was in the background; past `backgroundTries` it waits for the app to open or the interruption to end
    private var backgroundFailures = 0
    private static let backgroundTries = 3
    /// another app's call has the audio (Discord, WhatsApp ... not all of them go through CallKit): the recorder stays off and leaves the
    /// session alone until no other app's audio is on. Recording beside such a call took his microphone from it (phone, 2026-10-12).
    private var yielded = false
    /// the running engine and its cue player, for `play`
    private static let cue = OSAllocatedUnfairLock<(engine: AVAudioEngine, player: AVAudioPlayerNode)?>(initialState: nil)
    private static let cueFormat = AVAudioFormat(standardFormatWithSampleRate: SoundSynth.sampleRate, channels: 2)!

    /// True while the switch is on: the session is the recorder's, so the UI cues must not touch its category.
    var holdsSession: Bool { flags.withLock { $0.on } }

    func set(on: Bool, interrupted: Bool) {
        flags.withLock { $0.on = on; $0.interrupted = interrupted }
        kick()
    }

    /// The app came to the foreground (true) or went to the background. In the foreground a failed start is tried again as long as it fails.
    func setForeground(_ fg: Bool) {
        flags.withLock { $0.foreground = fg }
        if fg { queue.async { self.backgroundFailures = 0 } }
    }

    /// The voice is about to build its engine: the recorder's is stopped first (the piece stays open, the session stays active).
    func voiceStarting() async {
        flags.withLock { $0.voice = true }
        if holdsSession { lifelogNote("the voice takes the microphone") }
        await withCheckedContinuation { c in queue.async { self.reconcile(); c.resume() } }
    }

    /// The voice stopped its engine. True if the recorder keeps the session (it takes the microphone back); false if the caller lets it go.
    /// `otherWaiting`: the voice had taken the session from another app's audio. The recorder leaves the session to that app and waits
    /// for it to end, as if it had yielded to it, so the caller lets it go.
    @discardableResult func voiceStopped(otherWaiting: Bool = false) -> Bool {
        flags.withLock { $0.voice = false }
        if holdsSession { lifelogNote("the voice gave the microphone back\(otherWaiting ? " (it had taken it from another app)" : "")") }
        if otherWaiting, holdsSession {
            queue.async { self.yield("the voice took the session from another app: it goes back to that app") }
            return false
        }
        kick()
        return holdsSession
    }

    /// Brings the engine in line with the flags; `restart` builds it anew (the media services were reset).
    func kick(restart: Bool = false) {
        let awake = Awake("lifelog: the microphone")
        queue.async {
            if restart { self.stopOwn() }
            if self.yielded { self.checkYield() } else { self.reconcile() }
            awake.end()
        }
    }

    /// A cue through the recorder's engine. False if the recorder does not hold the session (the caller plays it itself); true otherwise,
    /// played or, with the engine down for a moment, dropped.
    static func play(_ b: AVAudioPCMBuffer, gain: Float) -> Bool {
        guard shared.holdsSession else { return false }
        cue.withLock { c in
            guard let c, c.engine.isRunning else { return }
            c.player.volume = min(max(gain, 0), 1)
            c.player.scheduleBuffer(b, at: nil, options: .interrupts)
            if !c.player.isPlaying { c.player.play() }
        }
        return true
    }

    private func reconcile() {
        let f = flags.withLock { $0 }
        if f.on && !f.interrupted && !f.voice {
            if engine == nil, !yielded { startOwn() }
        } else {
            let had = engine != nil
            stopOwn()
            if had && !f.on && !f.voice { try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation) }
        }
    }

    private var wanted: Bool { flags.withLock { $0.on && !$0.interrupted && !$0.voice } }

    private func startOwn() {
        let s = AVAudioSession.sharedInstance()
        // the earpiece or a Bluetooth headset in call mode is never the recorder's route (it asks for neither): someone else's call is on
        let r0 = s.currentRoute
        if r0.outputs.contains(where: { $0.portType == .builtInReceiver || $0.portType == .bluetoothHFP })
            || r0.inputs.contains(where: { $0.portType == .bluetoothHFP }) { return yield("call route \(r0.outputs.map(\.portType.rawValue))") }
        // mixable so his music goes on; no HFP so AirPods stay in high quality and the input stays the built-in microphone; .default mode has no
        // automatic gain control
        do {
            try s.setCategory(.playAndRecord, mode: .default, options: [.mixWithOthers, .allowBluetoothA2DP, .defaultToSpeaker])
            try s.setActive(true)
        } catch { return retry("session: \(error.localizedDescription)") }
        Self.preferStereoBuiltIn(s)
        // installTap with a 0 Hz or 0-channel format raises an exception Swift cannot catch (see VoiceAudio.start), so wait for a real one
        var built: (AVAudioEngine, AVAudioFormat)?
        for _ in 0..<30 {
            guard wanted else { return }
            let e = AVAudioEngine()
            let f = e.inputNode.outputFormat(forBus: 0)
            if s.isInputAvailable, f.sampleRate > 0, f.channelCount > 0 { built = (e, f); break }
            Thread.sleep(forTimeInterval: 0.1)
        }
        guard let (e, fmt) = built else { return retry("no microphone format") }
        let player = AVAudioPlayerNode()
        e.attach(player)
        e.connect(player, to: e.mainMixerNode, format: Self.cueFormat)
        e.inputNode.installTap(onBus: 0, bufferSize: 4096, format: fmt) { buf, _ in LifelogWriter.shared.feed(buf) }
        do {
            e.prepare()
            try e.start()
        } catch {
            e.inputNode.removeTap(onBus: 0)
            return retry("engine: \(error.localizedDescription)")
        }
        engine = e
        backgroundFailures = 0
        Self.cue.withLock { $0 = (e, player) }
        // the hardware changed and the engine stopped itself: built again (a changed input format is the converter's business)
        observer = NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange, object: e, queue: nil) { [weak self, weak e] _ in
            let awake = Awake("lifelog: rebuilding the engine")
            self?.queue.async {
                defer { awake.end() }
                guard let self, let e, self.engine === e else { return }
                self.stopOwn()
                // another app setting up its audio (a call starting) stops this engine; music starting does not
                if let why = OtherAudio.why { return self.yield("engine configuration changed: \(why)") }
                lifelogNote("engine configuration changed, building again")
                self.reconcile()
            }
        }
        let r = s.currentRoute
        lifelogNote("own engine, input \(Int(fmt.sampleRate)) Hz \(fmt.channelCount) ch, "
            + "in \(r.inputs.map { "\($0.portType.rawValue) \($0.selectedDataSource?.dataSourceName ?? "-") \($0.selectedDataSource?.selectedPolarPattern?.rawValue ?? "-")" }), "
            + "out \(r.outputs.map(\.portType.rawValue)), \(s.category.rawValue)/\(s.mode.rawValue)")
    }

    private func stopOwn() {
        guard let e = engine else { return }
        engine = nil
        if let observer { NotificationCenter.default.removeObserver(observer) }
        observer = nil
        Self.cue.withLock { $0 = nil }
        e.inputNode.removeTap(onBus: 0)
        e.stop()
    }

    /// Not now (the app was launched in the background, another app holds the microphone): again in 10 s, and when the app comes to the foreground.
    /// In the background iOS refuses a new start for as long as the app stays there ('!int', phone 2026-10-20 and 21: 47 tries in a row), so after
    /// a few tries it waits for the app to open or the interruption to end, and he is told.
    private func retry(_ why: String) {
        if let other = OtherAudio.why { return yield("could not start (\(why)): \(other)") }
        if !flags.withLock({ $0.foreground }) {
            backgroundFailures += 1
            if backgroundFailures >= Self.backgroundTries {
                lifelogNote("could not start the microphone in the background (\(why)), \(backgroundFailures) times: waiting for the app to open or the interruption to end")
                Task { @MainActor in LifelogAlarm.shared.stopped(.background) }
                return
            }
        }
        lifelogNote("could not start the microphone (\(why)), trying again in 10 s")
        guard !retrying else { return }
        retrying = true
        queue.asyncAfter(deadline: .now() + 10) { [self] in retrying = false; reconcile() }
    }

    /// Stops, lets the session go (telling an app that was interrupted it may go on), and waits until no other app's audio is on, looking every 10 s (reading a
    /// property, never touching the session) and whenever `kick` runs.
    private func yield(_ why: String) {
        stopOwn()
        if !yielded {
            lifelogNote("yielding to another app's audio: \(why)")
            try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
            LifelogWriter.shared.close()
            Task { @MainActor in LifelogAlarm.shared.yield(true) }
        }
        yielded = true
        guard !retrying else { return }
        retrying = true
        queue.asyncAfter(deadline: .now() + 10) { [self] in retrying = false; checkYield() }
    }

    private func checkYield() {
        guard yielded, wanted else { endYield(); return reconcile() }
        let s = AVAudioSession.sharedInstance()
        let callRoute = s.currentRoute.outputs.contains { $0.portType == .builtInReceiver || $0.portType == .bluetoothHFP }
        if OtherAudio.why != nil || callRoute { return yield("still on") }
        lifelogNote("the other app's audio ended: recording again")
        endYield()
        reconcile()
    }

    private func endYield() {
        if yielded { Task { @MainActor in LifelogAlarm.shared.yield(false) } }
        yielded = false
    }

    /// The built-in microphone, its data source that does stereo, held as in a portrait phone. Only when the built-in microphone is
    /// already the input and no other app's audio is on: a headset, or another app's call, keeps the route it has.
    private static func preferStereoBuiltIn(_ s: AVAudioSession) {
        guard s.currentRoute.inputs.first?.portType == .builtInMic, !s.isOtherAudioPlaying else {
            lifelogNote("stereo not asked for: input \(s.currentRoute.inputs.map(\.portType.rawValue)), other audio \(s.isOtherAudioPlaying)")
            return
        }
        guard let mic = s.availableInputs?.first(where: { $0.portType == .builtInMic }) else { return lifelogNote("stereo: no built-in microphone listed") }
        let sources = mic.dataSources ?? []
        lifelogNote("stereo: sources \(sources.map { "\($0.dataSourceName) \($0.supportedPolarPatterns?.map(\.rawValue) ?? [])" })")
        guard let ds = sources.first(where: { $0.supportedPolarPatterns?.contains(.stereo) == true }) else { return }
        do {
            try s.setPreferredInput(mic)
            try ds.setPreferredPolarPattern(.stereo)
            try mic.setPreferredDataSource(ds)
            try s.setPreferredInputOrientation(.portrait)
            try s.setPreferredInputNumberOfChannels(min(2, s.maximumInputNumberOfChannels))
        } catch { lifelogNote("stereo: \(error.localizedDescription)") }
        lifelogNote("stereo: \(s.inputNumberOfChannels) ch of \(s.maximumInputNumberOfChannels), \(ds.dataSourceName) \(ds.selectedPolarPattern?.rawValue ?? "-")")
    }
}

@MainActor @Observable
final class LifelogRecorder {
    static let shared = LifelogRecorder()
    /// the switch is on and no interruption is in the way
    private(set) var recording = false
    /// when the piece being written began (its first sample)
    private(set) var pieceStartedAt: Date?
    @ObservationIgnored private var interrupted = false
    @ObservationIgnored private let calls = CallWatch()

    private init() {
        LifelogWriter.shared.onPiece = { d in
            Task { @MainActor in
                LifelogRecorder.shared.pieceStartedAt = d
                if d == nil { LifelogUploader.shared.kick(); LifelogGate.shared.refresh() }
            }
        }
        let nc = NotificationCenter.default
        nc.addObserver(forName: AVAudioSession.interruptionNotification, object: nil, queue: .main) { n in
            let began = (n.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt) == AVAudioSession.InterruptionType.began.rawValue
            lifelogNote("interruption \(began ? "began" : "ended") \(n.userInfo ?? [:])")
            // resumes whether or not the system says it should: he wants the day recorded
            MainActor.assumeIsolated {
                let r = LifelogRecorder.shared
                r.interrupted = began
                r.apply()
                // an interruption whose end never comes (10:13 on 2026-10-21) leaves it off until he opens the app; a call is a pause he knows of
                if began, !r.calls.inCall { LifelogAlarm.shared.stopped(.interrupted, after: 60) }
            }
        }
        // A call (the phone, or an app's through CallKit, as Discord's voice) pauses the recording, its microphone and session left alone;
        // its end resumes it even if no interruption-ended notice comes.
        calls.onChange = { inCall in
            lifelogNote("call \(inCall ? "started" : "ended")")
            LifelogRecorder.shared.apply()
        }
        nc.addObserver(forName: AVAudioSession.mediaServicesWereResetNotification, object: nil, queue: .main) { _ in
            lifelogNote("media services were reset: building the engine again")
            LifelogWriter.shared.close()
            LifelogMic.shared.kick(restart: true)
        }
        // the control run in the widget extension set the switch: follow it
        CFNotificationCenterAddObserver(CFNotificationCenterGetDarwinNotifyCenter(), nil, { _, _, _, _, _ in
            DispatchQueue.main.async { MainActor.assumeIsolated { LifelogRecorder.shared.followSwitch() } }
        }, LifelogSwitch.changedNote, nil, .deliverImmediately)
        // an interruption whose end never came (another app let the microphone go without saying so): tried again when he opens the app
        nc.addObserver(forName: UIApplication.didBecomeActiveNotification, object: nil, queue: .main) { _ in
            MainActor.assumeIsolated {
                let r = LifelogRecorder.shared
                // not while another app's audio is on: that is most likely the call that interrupted it
                if r.interrupted, OtherAudio.why == nil {
                    lifelogNote("app opened during an interruption, no other audio on: trying again"); r.interrupted = false; r.apply()
                }
                // an engine that runs but gives nothing is built anew (what the stop notice promises when he taps it)
                else if r.recording, LifelogAlarm.silentFor > 5 {
                    lifelogNote("app opened, no audio for \(Int(min(LifelogAlarm.silentFor, 1e6))) s: building the engine again"); LifelogMic.shared.kick(restart: true)
                }
                else { r.followSwitch() }
            }
        }
        nc.addObserver(forName: UIApplication.willEnterForegroundNotification, object: nil, queue: .main) { _ in LifelogMic.shared.setForeground(true) }
        nc.addObserver(forName: UIApplication.didEnterBackgroundNotification, object: nil, queue: .main) { _ in LifelogMic.shared.setForeground(false) }
        nc.addObserver(forName: UIApplication.willTerminateNotification, object: nil, queue: nil) { _ in LifelogWriter.shared.closeSync() }
    }

    /// The switch. Turning on asks for the microphone the first time; refused, it stays off.
    func setOn(_ on: Bool) { Task { await turn(on) } }

    /// `setOn` that returns once the switch, the microphone and the Live Activity are settled: an intent awaits it, because iOS lets
    /// the Live Activity start only while the intent runs.
    func turn(_ on: Bool) async {
        lifelogNote("switch \(on ? "on" : "off")")
        guard on else { LifelogStore.isOn = false; apply(); return }
        var granted = AVAudioApplication.shared.recordPermission == .granted
        if !granted { granted = await AVAudioApplication.requestRecordPermission() }
        if !granted { lifelogNote("microphone permission refused") }
        LifelogStore.isOn = granted
        apply()
    }

    /// The switch as the App Group has it now (the extension may have set it).
    func followSwitch() {
        if LifelogStore.isOn, AVAudioApplication.shared.recordPermission == .denied { LifelogStore.isOn = false }
        lifelogNote("switch is \(LifelogStore.isOn ? "on" : "off") (followed)")
        apply()
        LifelogMic.shared.kick()
    }

    /// At launch (also a relaunch after the app was killed): pieces left open are closed, and recording goes on if the switch is on.
    func resume() {
        LifelogWriter.shared.recover()
        #if DEBUG
        if CommandLine.arguments.contains("-lifelogTest") { LifelogStore.isOn = true }
        #endif
        if LifelogStore.isOn, AVAudioApplication.shared.recordPermission == .denied { LifelogStore.isOn = false }
        LifelogMic.shared.setForeground(UIApplication.shared.applicationState != .background)
        apply()
    }

    private func apply() {
        let on = LifelogStore.isOn && DeviceRole.senses          // an iPad records nothing (ADR 0338)
        let paused = interrupted || calls.inCall
        let live = on && !paused
        LifelogWriter.shared.setAccepting(live)
        if !live { LifelogWriter.shared.close() }
        LifelogMic.shared.set(on: on, interrupted: paused)
        LifelogAlarm.shared.expect(on && !calls.inCall)
        LifelogGate.shared.run(on)
        let changed = live != recording
        recording = live
        if changed { ControlCenter.shared.reloadControls(ofKind: LifelogSwitch.controlKind) }   // the control shows the switch as it is now
    }
}

/// Library/Caches/lifelog.log, with the uploader's lines
func lifelogNote(_ s: String) { StallLog.append("lifelog: \(s)", to: "lifelog.log") }

/// Keeps the app from being frozen in the background until `end` (at most 30 s), from any thread. A hand-over of the microphone leaves a
/// moment with no audio running, and iOS froze the app in that moment: the wake word let the microphone go as he locked the phone, and the
/// recorder's start ran only when the app woke minutes later, in the background, where iOS refused it (phone, 2026-10-20 and 21).
final class Awake: @unchecked Sendable {
    private let done = DispatchSemaphore(value: 0)

    init(_ reason: String) {
        ProcessInfo.processInfo.performExpiringActivity(withReason: reason) { [done] expired in
            if expired { done.signal() } else { _ = done.wait(timeout: .now() + 30) }
        }
    }

    func end() { done.signal() }
}

/// Whether a call is up (the phone, FaceTime, or an app's call through CallKit); `onChange` on the main thread.
private final class CallWatch: NSObject, CXCallObserverDelegate {
    private let observer = CXCallObserver()
    var onChange: (@MainActor (Bool) -> Void)?
    var inCall: Bool { observer.calls.contains { !$0.hasEnded } }

    override init() {
        super.init()
        observer.setDelegate(self, queue: .main)
    }

    func callObserver(_ callObserver: CXCallObserver, callChanged call: CXCall) {
        let now = inCall
        MainActor.assumeIsolated { onChange?(now) }
    }
}
