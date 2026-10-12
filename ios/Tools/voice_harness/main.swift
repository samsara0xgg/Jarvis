import AVFoundation
import Foundation

// Mac check of the recognizer core: the asserts in VoiceCore / VoiceWire / PlayerCore, then both locales over every recorded clip
// through the same DualRecognizer the app uses. Run with run.sh.

demoVoiceCore()
demoVoiceWire()
demoPlayerCore()
print("asserts ok (command table, pick rule, wake word, frame codecs, PCM16 layout, player render)")

// Cross-language check (host_codec_check.py): `voice_harness xlang DIR` decodes the frames the host's own code wrote into DIR and writes ours back for it.
func xlang(_ dir: URL) {
    func bin(_ n: String) -> Data { try! Data(contentsOf: dir.appendingPathComponent(n + ".bin")) }
    func text(_ n: String) -> HostFrame { HostFrame(try! Data(contentsOf: dir.appendingPathComponent(n + ".json")))! }
    assert(PlayerIn(bin("active_7")) == .active(7) && PlayerIn(bin("active_none")) == .active(-1) && PlayerIn(bin("discard")) == .discard(1))
    assert(PlayerIn(bin("gain")) == .gain(target: 0.25, ramp: 1920) && PlayerIn(bin("hold")) == .hold(-1))
    assert(PlayerIn(bin("pcm")) == .pcm(generation: 5, start: 96_000, samples: [0, 1, -1, 16384, -32768, 1234]), "\(String(describing: PlayerIn(bin("pcm"))))")
    assert(text("ready_voice") == .ready(device: "phone", voice: true, sampleRate: 32_000, judges: false) && text("ready_text") == .ready(device: "phone", voice: false, sampleRate: 32_000, judges: false))
    assert(text("said") == .said(utteranceId: "u1", turnId: "Tu1", verdict: "turn"))
    guard case .row(let row) = text("row") else { fatalError("row") }
    assert(row.eventType == "surface.response_chunk" && row.eventUid == String(repeating: "a", count: 32) && row.tsEpochMs == 1_700_000_000_000)
    assert(row.turnId == "T1" && row.text == "好的" && row.phase == "final" && row.payload["sequence"] as? Int == 0, "\(row)")
    guard case .pong(let t, let host) = text("pong") else { fatalError("pong") }
    assert(t == 1234 && host > 0)
    guard case .error("bad_say", _) = text("error_bad_say"), case .error("bad_ping", _) = text("error_bad_ping"), case .error("retry", _) = text("error_retry") else { fatalError("error") }
    func put(_ n: String, _ d: Data) { try! d.write(to: dir.appendingPathComponent(n)) }
    put("swift_hello.json", PhoneFrame.hello.json)
    put("swift_say_spoken.json", PhoneFrame.say(utteranceId: "u1", text: "你好 Jarvis", language: "zh", confidence: 0.93, spoken: true).json)
    put("swift_say_typed.json", PhoneFrame.say(utteranceId: "u2", text: "hi", language: nil, confidence: nil, spoken: false).json)
    put("swift_ping.json", PhoneFrame.ping(tNs: 1_234_567_890_123).json)
    put("swift_barge.json", PhoneFrame.barge.json)
    put("swift_cancel.json", PhoneFrame.cancel.json)
    // READY through the player's own builder: the source rate, not the hardware's
    let core = PlayerCore(sampleRate: 48_000)
    core.deviceLatencyNs = 20_000_000
    put("swift_ready.bin", core.readyFrame(bufferFrames: 512).bytes)
    put("swift_report.bin", PlayerOut.report(PlayerReport(generation: 9, start: 100, end: 740, audibility: 1, callbackNs: 55, presentationDelayNs: 66, first: true)).bytes)
    put("swift_report_none.bin", PlayerOut.report(PlayerReport(generation: -1, start: 0, end: 0, audibility: 3, callbackNs: 0, presentationDelayNs: 0, first: false)).bytes)
    put("swift_status.bin", PlayerOut.status(PlayerStatus(readIdx: 77, gain: 0.5, callbacks: 3, overloads: 2, starvationGaps: 1, tailRampSamples: 128, playedSamples: 99)).bytes)
    put("swift_discard_ack.bin", PlayerOut.discardAck((1 << 40) + 3).bytes)
    print("host frames decoded by Swift: ACTIVE x2, DISCARD, GAIN, HOLD, PCM16, ready x2, said, row, pong, error x3")
}
if CommandLine.arguments.count == 3, CommandLine.arguments[1] == "xlang" { xlang(URL(fileURLWithPath: CommandLine.arguments[2])); exit(0) }

// The stub host end to end: say -> row + synthesized audio frames -> the player renders them and reports every sample played.
@MainActor func stubLoop() async throws {
    let link = LocalStubLink()
    link.connect()
    let core = PlayerCore(sampleRate: 48_000)
    var block = [Float](repeating: 0, count: 480)
    let render = Task.detached {
        while !Task.isCancelled {
            block.withUnsafeMutableBufferPointer { core.render($0.baseAddress!, frames: 480, delayNs: 10_000_000) }
            try? await Task.sleep(nanoseconds: 10_000_000)
        }
    }
    var rowTypes: [String] = [], rowText = "", rowTurns = Set<String>(), total = 0, frames = 0, loud = false
    let (turn, verdict, _) = try await link.say(utteranceId: "u1", text: "你好 Jarvis", language: .zh, confidence: 0.9, spoken: true, overHer: false, conversation: false, about: nil, timeout: 10)
    let reader = Task { @MainActor in
        for await e in link.events {
            switch e {
            case .row(let r):
                rowTypes.append(r.eventType); rowTurns.insert(r.turnId ?? "")
                if r.eventType == "surface.response_chunk" { rowText += r.text ?? "" }
            case .audio(let f):
                core.ingest(f)
                if case .pcm(_, _, let x) = f { total += x.count; frames += 1 }
            default: break
            }
        }
    }
    var reports: [PlayerReport] = []
    let start = Date()
    var lastNew = Date()
    while Date().timeIntervalSince(lastNew) < 1.5, Date().timeIntervalSince(start) < 30 {
        try await Task.sleep(nanoseconds: 20_000_000)
        for case .report(let r) in core.drain() { reports.append(r); lastNew = Date(); loud = loud || core.level > 0.001 }
    }
    reader.cancel(); render.cancel()
    assert(turn == "stub-1" && verdict == "turn" && rowText == "还没连上大脑。你刚才说的是：你好 Jarvis" && rowTurns == [turn], "\(rowText) \(rowTurns)")
    assert(rowTypes == ["surface.response_open", "surface.response_chunk", "surface.response_chunk", "surface.response_emitted"], "\(rowTypes)")
    assert(total > 32_000, "synthesized speech is longer than a second: \(total) samples")
    assert(reports.last?.end == Int64(total), "every source sample is reported played: \(String(describing: reports.last?.end)) of \(total)")
    assert(loud && reports.first?.first == true)
    print(String(format: "stub loop ok: %.1f s of speech in %d frames, %d reports, last end cursor %d", Double(total) / 32_000, frames, reports.count, Int(reports.last!.end)))
}
try await stubLoop()

let root = URL(fileURLWithPath: "/Users/alllllenshi/.jarvis/research/phone-2026-10-09")
let wavs = try FileManager.default.contentsOfDirectory(at: root.appendingPathComponent("wav"), includingPropertiesForKeys: nil)
    .filter { $0.pathExtension == "wav" }.sorted { $0.lastPathComponent < $1.lastPathComponent }

guard #available(macOS 26, *) else { fatalError("needs macOS 26") }
try await DualRecognizer.ensureAssets { _ in }

func recognize(_ url: URL, faults: [VoiceLang: VoiceLane.Fault] = [:], dead: DeadLog? = nil) async throws -> (picked: LaneResult, all: [LaneResult], seconds: Double) {
    let file = try AVAudioFile(forReading: url)
    let start = Date()
    let rec = DualRecognizer(faults: faults, onDead: { dead?.add($0, $1) }, onPartial: { _ in })
    while file.framePosition < file.length {
        guard let buf = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: 4096) else { break }
        try file.read(into: buf)
        if let mono = monoFloat(buf) { rec.feed(mono) }
    }
    let (picked, all) = await rec.finish()
    return (picked, all, Date().timeIntervalSince(start))
}

_ = try await recognize(wavs[0])        // warm both models; not counted

// A lane that cannot start, or never delivers, must not hang finish(): the live lane's text is still picked.
final class DeadLog: @unchecked Sendable {
    private let lock = NSLock()
    private var items: [(VoiceLang, Bool)] = []
    func add(_ l: VoiceLang, _ both: Bool) { lock.lock(); items.append((l, both)); lock.unlock() }
    var all: [(VoiceLang, Bool)] { lock.lock(); defer { lock.unlock() }; return items }
}
do {
    var clip = wavs[0], base = try await recognize(clip)
    for w in wavs where base.picked.text.isEmpty { clip = w; base = try await recognize(w) }
    let live = base.picked.lang, other: VoiceLang = live == .zh ? .en : .zh
    assert(!base.picked.text.isEmpty)
    let d1 = DeadLog()
    let failed = try await recognize(clip, faults: [other: .failStart], dead: d1)
    assert(failed.seconds < 2 && failed.picked == base.picked && failed.all.first { $0.lang == other }!.text.isEmpty, "\(failed)")
    assert(d1.all.count == 1 && d1.all[0].0 == other && !d1.all[0].1, "\(d1.all)")
    print(String(format: "dead lane %@ (failed to start): finish %.2fs, picked %@ \"%@\" like the unfaulted run, reported dead once, not both", other.rawValue, failed.seconds, failed.picked.lang.rawValue, failed.picked.text))
    let hung = try await recognize(clip, faults: [other: .hangFinish])
    assert(hung.seconds >= kLaneDeadline && hung.seconds < kLaneDeadline + 1 && hung.picked.lang == base.picked.lang && !hung.picked.text.isEmpty, "\(hung)")
    print(String(format: "hung lane %@ (never finishes): cut off, finish %.2fs (deadline %.1fs), picked %@ \"%@\"", other.rawValue, hung.seconds, kLaneDeadline, hung.picked.lang.rawValue, hung.picked.text))
    // A lane that never delivers a final result still gives its last volatile text; both-empty stays empty.
    let nf = try await recognize(clip, faults: [live: .noFinal, other: .failStart])
    assert(nf.seconds < kLaneDeadline && nf.picked.lang == live && !nf.picked.text.isEmpty, "\(nf)")
    print(String(format: "no-final lane %@ (isFinal dropped): finish %.2fs, fell back to volatile text, %d chars (the normal run had %d)", live.rawValue, nf.seconds, nf.picked.text.count, base.picked.text.count))
    let nfBoth = try await recognize(clip, faults: [.zh: .noFinal, .en: .noFinal])
    assert(!nfBoth.picked.text.isEmpty, "\(nfBoth)")
    print(String(format: "no-final on both lanes: finish %.2fs, picked %@ with %d chars of volatile text", nfBoth.seconds, nfBoth.picked.lang.rawValue, nfBoth.picked.text.count))
    // The tie-break on zero confidence: a lane in its own script beats one that is not; neither or both -> zh.
    let pairs: [(String, LaneResult, LaneResult, VoiceLang)] = [
        ("zh text vs en garbage, conf 0", LaneResult(lang: .zh, text: "今天天气怎么样"), LaneResult(lang: .en, text: "Jin tian tian chi yang"), .zh),
        ("en garbage longer than zh text, conf 0", LaneResult(lang: .zh, text: "你好"), LaneResult(lang: .en, text: "Knee how a very long run"), .zh),
        ("en text vs empty zh, conf 0", LaneResult(lang: .zh), LaneResult(lang: .en, text: "What time is it"), .en),
        ("zh latin-only vs en text, conf 0", LaneResult(lang: .zh, text: "ok"), LaneResult(lang: .en, text: "ok"), .en),
        ("both empty", LaneResult(lang: .zh), LaneResult(lang: .en), .zh),
    ]
    for (name, a, b, want) in pairs {
        let got = pickLane(a, b)
        assert(got.lang == want, name)
        print("tie-break \(name): picked \(got.lang.rawValue)")
    }
    let d2 = DeadLog()
    let none = try await recognize(clip, faults: [.zh: .failStart, .en: .failStart], dead: d2)
    assert(none.seconds < 2 && none.picked.text.isEmpty && d2.all.count == 2 && d2.all.contains { $0.1 }, "\(none) \(d2.all)")
    print(String(format: "both lanes dead: finish %.2fs, empty text, reported both dead", none.seconds))
}

var rows: [[String: Any]] = []
var times: [Double] = []
for url in wavs {
    let id = url.deletingPathExtension().lastPathComponent
    let (picked, all, secs) = try await recognize(url)
    let zh = all[0], en = all[1]
    let cmd = shortCommand(picked.text)
    times.append(secs)
    print(String(format: "%@ %@ zh %.3f en %.3f %.2fs  %@%@", id, picked.lang.rawValue, zh.confidence, en.confidence, secs,
                 picked.text, cmd.map { "  [cmd \($0.rawValue)]" } ?? ""))
    rows.append(["id": id, "picked": picked.lang.rawValue, "text": picked.text, "zh_text": zh.text, "zh_conf": zh.confidence,
                 "en_text": en.text, "en_conf": en.confidence, "command": cmd?.rawValue as Any, "seconds": secs])
}
let median = times.sorted()[times.count / 2]
print(String(format: "clips %d, median %.3f s, max %.3f s", times.count, median, times.max() ?? 0))

// The same rule computed earlier (apple_dual.json, plain arm), for comparison.
let old = try JSONSerialization.jsonObject(with: Data(contentsOf: root.appendingPathComponent("apple_dual.json"))) as! [[String: Any]]
var same = 0, sameText = 0, sameNames = 0, diffs: [String] = []
for r in rows {
    guard let o = old.first(where: { $0["id"] as? String == r["id"] as? String }) else { continue }
    let zh = (o["zh-CN|plain"] as! [String: Any]), en = (o["en-US|plain"] as! [String: Any])
    let oldPick = (en["conf"] as! Double) > (zh["conf"] as! Double) ? "en-US" : "zh-CN"
    if oldPick == r["picked"] as! String { same += 1 } else { diffs.append("\(r["id"]!) now \(r["picked"]!) (zh \(r["zh_conf"]!) en \(r["en_conf"]!)) was \(oldPick) (zh \(zh["conf"]!) en \(en["conf"]!))") }
    let nz = o["zh-CN|names"] as! [String: Any], ne = o["en-US|names"] as! [String: Any]
    if ((ne["conf"] as! Double) > (nz["conf"] as! Double) ? "en-US" : "zh-CN") == r["picked"] as! String { sameNames += 1 }
    if (oldPick == "zh-CN" ? zh["text"] as! String : en["text"] as! String) == r["text"] as! String { sameText += 1 }
}
print("same locale as apple_dual.json: \(same) of \(rows.count); same text: \(sameText); same locale as the names arm: \(sameNames)")
for d in diffs { print("  differs:", d) }

try JSONSerialization.data(withJSONObject: ["median_seconds": median, "same_locale": same, "clips": rows], options: [.prettyPrinted, .sortedKeys])
    .write(to: root.appendingPathComponent("harness_out.json"))
