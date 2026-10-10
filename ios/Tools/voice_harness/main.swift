import AVFoundation
import Foundation

// Mac check of the recognizer core: the asserts in VoiceCore / VoiceWire / PlayerCore, then both locales over every recorded clip
// through the same DualRecognizer the app uses. Run with run.sh.

demoVoiceCore()
demoVoiceWire()
demoPlayerCore()
print("asserts ok (command table, pick rule, wake word, frame codecs, PCM16 layout, player render)")

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
    var row: String?, total = 0, frames = 0, loud = false
    let turn = try await link.say(utteranceId: "u1", text: "你好 Jarvis", language: .zh, spoken: true)
    let reader = Task { @MainActor in
        for await e in link.events {
            switch e {
            case .row(let r): row = r.text
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
    assert(turn == "stub-1" && row == "还没连上大脑。你刚才说的是：你好 Jarvis", "\(String(describing: row))")
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

func recognize(_ url: URL) async throws -> (picked: LaneResult, all: [LaneResult], seconds: Double) {
    let file = try AVAudioFile(forReading: url)
    let start = Date()
    let rec = DualRecognizer { _ in }
    while file.framePosition < file.length {
        guard let buf = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: 4096) else { break }
        try file.read(into: buf)
        if let mono = monoFloat(buf) { rec.feed(mono) }
    }
    let (picked, all) = await rec.finish()
    return (picked, all, Date().timeIntervalSince(start))
}

_ = try await recognize(wavs[0])        // warm both models; not counted

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
