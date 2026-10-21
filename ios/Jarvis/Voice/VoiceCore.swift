// words: legacy
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

/// Enough words to tell anything from: three CJK characters or two English words. Fewer match something she said by chance (我, 今天, 好),
/// and taking them for her echo cut his first words off when he spoke over her (yilun 2026-10-11).
func enoughToJudge(_ s: String) -> Bool {
    let cjk = squash(s).unicodeScalars.filter { $0.value >= 0x2E80 }.count
    let words = s.split { !$0.isLetter }.filter { $0.unicodeScalars.allSatisfy(\.isASCII) }.count
    return cjk >= 3 || words >= 2
}

/// Her own voice leaking into the microphone: the words are in what she said lately, or nearly (0.8, the Mac's rule). Too few words are never her echo.
func isOwnEcho(_ heard: String, _ hers: String) -> Bool {
    let h = Array(squash(heard)), r = Array(squash(hers))
    guard !h.isEmpty, !r.isEmpty, enoughToJudge(heard) else { return false }
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

/// Roughly how long a text takes to say: a CJK character 1, a Latin letter or digit 0.3, anything else 0.15.
func speechUnits(_ s: String) -> Double { s.unicodeScalars.reduce(0) { $0 + unitCost($1) } }

/// The start of `s` that takes `units` to say.
func speechPrefix(_ s: String, _ units: Double) -> String {
    var used = 0.0, end = s.startIndex
    for i in s.indices {
        used += s[i].unicodeScalars.reduce(0) { $0 + unitCost($1) }
        if used > units { break }
        end = s.index(after: i)
    }
    return String(s[..<end])
}

private func unitCost(_ c: Unicode.Scalar) -> Double {
    if c.value >= 0x2E80 && !CharacterSet.punctuationCharacters.contains(c) { return 1 }
    return CharacterSet.alphanumerics.contains(c) ? 0.3 : 0.15
}

enum OverHer: Equatable { case echo, backchannel, tooShort, barge }

/// His last spoken say, kept so that words which start soon after it and go on from it can name it. Only the id is kept, never the words: the host puts
/// the segments together (ADR 0271), so each say carries just what was newly said and a thought of three says is never sent as three growing copies.
struct SayChain: Equatable {
    private(set) var id: String?
    private(set) var endedAt = 0.0

    /// His utterance ended at `t`; the say that follows names it.
    mutating func ended(at t: Double) { endedAt = t }
    /// The spoken say with this id was sent.
    mutating func said(_ id: String) { self.id = id }
    /// Nothing before this is a sentence the next words could go on from (typed words, or listening began afresh).
    mutating func clear() { id = nil }
    /// The id the words that start at `t` go on from: his last say, if they start within `window` seconds of its end.
    func continues(at t: Double, within window: Double) -> String? {
        guard let id, t - endedAt < window else { return nil }
        return id
    }
}

/// Only nods: one from the list, or nod characters over and over (嗯嗯嗯嗯, 好的好的).
private let nodChars: Set<Character> = ["嗯", "对", "是", "的", "好", "哦", "噢", "喔", "啊", "行"]
func isNod(_ squashed: String) -> Bool {
    backchannels.contains(squashed) || (!squashed.isEmpty && squashed.count <= 6 && squashed.first != "的" && squashed.allSatisfy(nodChars.contains))
}

/// What his words over her voice are: her echo, a nod, too little to tell yet, or an interruption.
/// `hers`: what she said lately; `judged`: the start already taken for her echo. A stop or wait word interrupts at once; otherwise three CJK characters or two Latin words.
func judgeOverHer(_ text: String, hers: String, judged: String) -> OverHer {
    let rest = afterJudged(text, judged)
    let t = squash(rest)
    if t.isEmpty { return .echo }
    if let c = shortCommand(rest), c == .stop || c == .wait { return .barge }
    if isNod(t) { return .backchannel }
    if !enoughToJudge(rest) { return .tooShort }
    return isOwnEcho(rest, hers) ? .echo : .barge
}

#if DEBUG || VOICE_HARNESS
func demoVoiceCore() {
    // a thought in three says: each names the one before and none carries more than its own words
    var chain = SayChain()
    assert(chain.continues(at: 0, within: 4) == nil)
    chain.ended(at: 1); chain.said("a")
    assert(chain.continues(at: 4.9, within: 4) == "a" && chain.continues(at: 5.1, within: 4) == nil)
    chain.ended(at: 5); chain.said("b")
    assert(chain.continues(at: 6, within: 4) == "b")
    chain.clear()
    assert(chain.continues(at: 6, within: 4) == nil)
    assert(speechUnits("你好") == 2 && abs(speechUnits("Hi，") - 0.75) < 1e-9)
    assert(speechPrefix("你好 Allen。", 2) == "你好" && speechPrefix("你好 Allen。", 3.7) == "你好 Allen" && speechPrefix("你好", 0.5) == "" && speechPrefix("你好", 9) == "你好")
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
    // his first words over her are his even when she said them too (2026-10-11: 「我」 and 「今天」 were taken for her echo and cut off)
    assert(judgeOverHer("我", hers: her, judged: "") == .tooShort && judgeOverHer("今天", hers: her, judged: "") == .tooShort && !isOwnEcho("今天", her))
    assert(judgeOverHer("今天下午能不能改到明天", hers: her, judged: "") == .barge)
    assert(judgeOverHer("嗯嗯嗯嗯", hers: her, judged: "") == .backchannel && judgeOverHer("好的好的", hers: her, judged: "") == .backchannel)
    assert(judgeOverHer("嗯不对我是说明天", hers: her, judged: "") == .barge && judgeOverHer("的", hers: her, judged: "") == .tooShort)
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
