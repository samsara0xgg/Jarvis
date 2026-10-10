import Foundation

// The recognizer's decisions, with no UIKit so a Mac harness can compile and check them (ios/Tools/voice_harness).

enum VoiceLang: String, CaseIterable, Sendable {
    case zh = "zh-CN", en = "en-US"
    var locale: Locale { Locale(identifier: rawValue) }
    /// what the host gets in `say.language`
    var code: String { self == .zh ? "zh" : "en" }
}

/// What one locale's transcriber finalized for an utterance.
struct LaneResult: Equatable, Sendable {
    var lang: VoiceLang
    var text = ""
    var confidence = 0.0
}

/// Mean transcriptionConfidence over a lane's runs; no runs (empty text) counts as 0.
func meanConfidence(_ runs: [Double]) -> Double { runs.isEmpty ? 0 : runs.reduce(0, +) / Double(runs.count) }

/// The locale whose text is more confident wins. Measured on 40 real recordings; it ties a SenseVoice detector, so no other language detector.
/// A tie goes to the first (zh).
func pickLane(_ a: LaneResult, _ b: LaneResult) -> LaneResult { b.confidence > a.confidence ? b : a }

// MARK: short commands

enum VoiceCommand: String, Sendable { case stop, wait, resume, goodnight }

private let commandTable: [VoiceCommand: [String]] = [
    .stop: ["退下", "退下吧", "停", "停下", "别说了", "闭嘴", "stop", "shutup"],
    .wait: ["等一下", "等等", "等我一下", "wait", "holdon"],
    .resume: ["继续", "接着说", "goon", "continue"],
    .goodnight: ["晚安", "goodnight"],
]

/// lowercased, without punctuation, symbols or white space
func squash(_ s: String) -> String {
    String(s.lowercased().unicodeScalars.filter {
        !CharacterSet.punctuationCharacters.contains($0) && !CharacterSet.symbols.contains($0) && !CharacterSet.whitespacesAndNewlines.contains($0)
    })
}

/// A command only when the whole utterance is that command.
func shortCommand(_ text: String) -> VoiceCommand? {
    let t = squash(text)
    return commandTable.first { $0.value.contains(t) }?.key
}

// MARK: wake word

private let wakeWords = ["jarvis", "贾维斯"]

func heardWake(_ text: String) -> Bool {
    let t = text.lowercased()
    return wakeWords.contains { t.contains($0) }
}

/// The words after the first wake word, without the punctuation that follows it. Text with no wake word comes back as it was.
func stripWake(_ text: String) -> String {
    guard let hit = wakeWords.compactMap({ text.range(of: $0, options: .caseInsensitive) }).min(by: { $0.lowerBound < $1.lowerBound }) else { return text }
    return String(text[hit.upperBound...].drop { $0.isPunctuation || $0.isWhitespace })
}

#if DEBUG || VOICE_HARNESS
func demoVoiceCore() {
    assert(meanConfidence([]) == 0 && meanConfidence([0.5, 1]) == 0.75)
    let zh = LaneResult(lang: .zh, text: "退下吧", confidence: 0.998)
    let en = LaneResult(lang: .en, text: "Toy Saba.", confidence: 0.529)
    assert(pickLane(zh, en).lang == .zh && pickLane(en, zh).lang == .zh)
    assert(pickLane(LaneResult(lang: .zh), LaneResult(lang: .en)).lang == .zh)       // tie
    assert(pickLane(LaneResult(lang: .zh), LaneResult(lang: .en, text: "I would.", confidence: 0.57)).lang == .en)
    for (t, c) in [("退下", VoiceCommand.stop), ("退下吧。", .stop), (" Shut up! ", .stop), ("shut  up", .stop), ("Stop.", .stop), ("停", .stop),
                   ("等一下", .wait), ("Hold on", .wait), ("hold on,", .wait), ("继续", .resume), ("Go on.", .resume), ("接着说", .resume),
                   ("晚安", .goodnight), ("Good night!", .goodnight)] {
        assert(shortCommand(t) == c, t)
    }
    for t in ["退下 吧 好", "stop the music", "不要停", "wait for me", "", "。"] { assert(shortCommand(t) == nil, t) }
    assert(heardWake("Hey Jarvis, what time is it") && heardWake("贾维斯你好") && !heardWake("hello"))
    assert(stripWake("Jarvis, what time is it") == "what time is it")
    assert(stripWake("嘿，贾维斯，几点了") == "几点了")
    assert(stripWake("Jarvis.") == "" && stripWake("no wake") == "no wake")
}
#endif
