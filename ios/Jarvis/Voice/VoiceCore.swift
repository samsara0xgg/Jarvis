import Foundation

// The recognizer's decisions, with no UIKit so a Mac harness can compile and check them (ios/Tools/voice_harness).

enum VoiceLang: String, CaseIterable, Sendable {
    case zh = "zh-CN", en = "en-US"
    var locale: Locale { Locale(identifier: rawValue) }
    /// what the host gets in `say.language`
    var code: String { self == .zh ? "zh" : "en" }
    var modelMissingText: String { self == .zh ? "中文语音模型没装好" : "英文语音模型没装好" }
}

/// What one locale's transcriber finalized for an utterance.
struct LaneResult: Equatable, Sendable {
    var lang: VoiceLang
    var text = ""
    var confidence = 0.0
}

/// Mean transcriptionConfidence over a lane's runs; no runs (empty text) counts as 0.
func meanConfidence(_ runs: [Double]) -> Double { runs.isEmpty ? 0 : runs.reduce(0, +) / Double(runs.count) }

protocol LaneText { var lang: VoiceLang { get }; var text: String { get }; var confidence: Double { get } }
extension LaneResult: LaneText {}

/// zh: the text has CJK (scalar >= U+2E80). en: it has Latin letters and no CJK.
func inOwnScript(_ l: some LaneText) -> Bool {
    let u = l.text.unicodeScalars
    let cjk = u.contains { $0.value >= 0x2E80 }
    return l.lang == .zh ? cjk : !cjk && u.contains { $0.isASCII && $0.properties.isAlphabetic }
}

/// The locale whose text is more confident wins. Measured on 40 real recordings; it ties a SenseVoice detector, so no other language detector.
/// On equal confidence (volatile text carries none) the lane whose text is in its own script wins; if neither or both are, the first (zh).
func pickLane<T: LaneText>(_ a: T, _ b: T) -> T {
    if a.confidence != b.confidence { return b.confidence > a.confidence ? b : a }
    return inOwnScript(b) && !inOwnScript(a) ? b : a
}

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

// MARK: words over her voice

private let backchannels: Set<String> = ["嗯", "嗯嗯", "嗯嗯嗯", "对", "对对", "对的", "是", "是的", "好", "好的", "哦", "噢", "喔", "啊", "行",
                                         "ok", "okay", "yeah", "yes", "mhm", "mmhmm", "uhhuh", "right", "sure"]

/// 2 x longest common subsequence / total length: 1 for equal strings
private func similarity(_ a: [Character], _ b: [Character]) -> Double {
    guard !a.isEmpty, !b.isEmpty else { return 0 }
    var prev = [Int](repeating: 0, count: b.count + 1)
    for x in a {
        var cur = [Int](repeating: 0, count: b.count + 1)
        for (j, y) in b.enumerated() { cur[j + 1] = x == y ? prev[j] + 1 : max(prev[j + 1], cur[j]) }
        prev = cur
    }
    return 2 * Double(prev[b.count]) / Double(a.count + b.count)
}

/// Her own voice leaking into the microphone: the words are in what she said lately, or nearly (0.8, the Mac's rule).
func isOwnEcho(_ heard: String, _ hers: String) -> Bool {
    let h = Array(squash(heard)), r = Array(squash(hers))
    guard !h.isEmpty, !r.isEmpty else { return false }
    if String(r).contains(String(h)) { return true }
    guard h.count < r.count else { return similarity(h, r) >= 0.8 }
    // windows as long as the words, or a little longer for a word the recognizer added
    return (0...(r.count - h.count)).contains { i in [0, 2].contains { similarity(h, Array(r[i..<min(r.count, i + h.count + $0)])) >= 0.8 } }
}

/// The part of `text` after what was already judged (a growing partial starts with the earlier one, give or take a revision).
func afterJudged(_ text: String, _ judged: String) -> String {
    let n = zip(text, judged).prefix { $0 == $1 }.count
    return String(text.dropFirst(n))
}

enum OverHer: Equatable { case echo, backchannel, tooShort, barge }

/// What his words over her voice are: her echo, a nod, too little to stop her for yet, or an interruption.
/// `hers`: what she said lately. A stop or wait word interrupts at once; otherwise three CJK characters or two Latin words.
func judgeOverHer(_ text: String, hers: String, judged: String) -> OverHer {
    let rest = afterJudged(text, judged)
    let t = squash(rest)
    if t.isEmpty { return .echo }
    if let c = shortCommand(rest), c == .stop || c == .wait { return .barge }
    if isOwnEcho(rest, hers) { return .echo }
    if backchannels.contains(t) { return .backchannel }
    let cjk = t.unicodeScalars.filter { $0.value >= 0x2E80 }.count
    let words = rest.split { !$0.isLetter }.filter { $0.unicodeScalars.allSatisfy(\.isASCII) }.count
    return cjk >= 3 || words >= 2 ? .barge : .tooShort
}

#if DEBUG || VOICE_HARNESS
func demoVoiceCore() {
    // words over her voice: the 10-10 leak ("我看一下日程" heard while she said it), nods, a stop word, a real interruption
    let her = "我看一下日程。今天下午三点你有一个面试"
    assert(judgeOverHer("我看一下日程", hers: her, judged: "") == .echo)
    assert(judgeOverHer("我看一下日成", hers: her, judged: "") == .echo)
    assert(judgeOverHer("嗯", hers: her, judged: "") == .backchannel && judgeOverHer("Okay.", hers: her, judged: "") == .backchannel)
    assert(judgeOverHer("停", hers: her, judged: "") == .barge && judgeOverHer("等一下", hers: her, judged: "") == .barge)
    assert(judgeOverHer("不是", hers: her, judged: "") == .tooShort)
    assert(judgeOverHer("不是这个意思", hers: her, judged: "") == .barge)
    assert(judgeOverHer("我看一下日程不是这个意思", hers: her, judged: "我看一下日程") == .barge)
    assert(judgeOverHer("嗯我看一下日程", hers: her, judged: "嗯") == .echo)
    assert(judgeOverHer("wait a second", hers: "The weather is fine", judged: "") == .barge)
    assert(afterJudged("我看一下日程不是", "我看一下日程") == "不是")
    assert(meanConfidence([]) == 0 && meanConfidence([0.5, 1]) == 0.75)
    let zh = LaneResult(lang: .zh, text: "退下吧", confidence: 0.998)
    let en = LaneResult(lang: .en, text: "Toy Saba.", confidence: 0.529)
    assert(pickLane(zh, en).lang == .zh && pickLane(en, zh).lang == .zh)
    assert(pickLane(LaneResult(lang: .zh), LaneResult(lang: .en)).lang == .zh)       // tie
    assert(pickLane(LaneResult(lang: .zh), LaneResult(lang: .en, text: "I would.", confidence: 0.57)).lang == .en)
    // zero confidence on both (volatile text): the lane in its own script wins, else zh
    let (zhText, enGarbage) = (LaneResult(lang: .zh, text: "今天天气怎么样"), LaneResult(lang: .en, text: "Jin tian tian chi"))
    assert(pickLane(zhText, enGarbage).lang == .zh)
    assert(pickLane(LaneResult(lang: .zh), LaneResult(lang: .en, text: "What time is it")).lang == .en)
    assert(pickLane(LaneResult(lang: .zh, text: "ok"), LaneResult(lang: .en, text: "ok")).lang == .en)
    assert(pickLane(LaneResult(lang: .zh), LaneResult(lang: .en)).lang == .zh && pickLane(LaneResult(lang: .zh, text: "你好"), LaneResult(lang: .en)).lang == .zh)
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
