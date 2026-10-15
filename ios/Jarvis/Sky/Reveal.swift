import SwiftUI

// How her words appear in the talk: the whole answer is laid out at once, its line breaks final, and each word fades in (opacity only) when it is said. A spoken answer
// follows her voice (the voice session's pace, Voice/VoiceSession `revealAnswer`); a typed one has come whole, so it all fades in at once. Nothing is laid out again
// as it goes: the text view stays as it is and a text renderer draws each glyph at the opacity of its word.

/// When each word of a row is said, in seconds on `Date.timeIntervalSinceReferenceDate`. A word past the end of `at` is not said yet.
struct Reveal: Equatable {
    var at: [Double] = []
    var voiced = false          // her voice sets the pace; a typed answer is shown at once under Reduce Motion

    static let fade = 0.2

    /// `n` words said one `every` after the other from `start`
    static func timed(_ n: Int, from start: Double, every: Double) -> Reveal { Reveal(at: (0..<n).map { start + Double($0) * every }) }

    /// words up to the `n`th are said from `t` on (said words are never taken back)
    mutating func say(_ n: Int, at t: Double) { while at.count < n { at.append(t) } }

    /// 0 (not said) to 1 (lit) for the word `w`; with no fade a word is either
    func alpha(_ w: Int, at t: Double, fade: Double = Reveal.fade) -> Double {
        guard w < at.count else { return 0 }
        guard fade > 0 else { return t >= at[w] ? 1 : 0 }
        let k = clamp((t - at[w]) / fade, 0, 1)
        return 1 - (1 - k) * (1 - k)
    }

    /// when the last word said so far is lit
    func end(fade: Double = Reveal.fade) -> Double { (at.last ?? 0) + fade }

    // MARK: words

    /// The word each character of `s` belongs to: a Chinese character (or an emoji) is a word of its own, a run of Latin letters and digits is one (`don't`, `well-known` too),
    /// and spaces and punctuation go with the word before them, so they come in with it.
    static func words(_ s: String) -> [Int] {
        var out: [Int] = [], w = -1, inLatin = false
        for c in s {
            if let u = c.unicodeScalars.first, u.value >= 0x2E80, !CharacterSet.punctuationCharacters.contains(u) { w += 1; inLatin = false }
            else if c.isLetter || c.isNumber { if !inLatin { w += 1 }; inLatin = true }
            else if !"'’-".contains(c) { inLatin = false }
            out.append(max(w, 0))
        }
        return out
    }

    static func count(_ s: String) -> Int { (words(s).last ?? -1) + 1 }

    /// how many words are said when her voice has got `chars` characters into the text whose words are `wordOf`: those it has started
    static func said(_ chars: Int, in wordOf: [Int]) -> Int { chars <= 0 || wordOf.isEmpty ? 0 : wordOf[min(chars, wordOf.count) - 1] + 1 }
}

/// The word a piece of the text belongs to, carried on the text itself: the layout's character indices start again at each paragraph (and at some runs within a line),
/// so a glyph's word cannot be counted from them.
private struct WordTag: TextAttribute { let word: Int }

/// Draws each glyph at the opacity of its word. The text under it keeps its layout; only this redraws.
private struct RevealRenderer: TextRenderer {
    let reveal: Reveal
    let t: Double
    let fade: Double

    func draw(layout: Text.Layout, in ctx: inout GraphicsContext) {
        for line in layout {
            for run in line {            // a run is one word: the tags split them
                let a = reveal.alpha(run[WordTag.self]?.word ?? 0, at: t, fade: fade)
                if a > 0 { ctx.opacity = a; ctx.draw(run) }
            }
        }
    }
}

/// 60 frames a second from now until `until`, then no more: the last frame is the final state.
private struct RevealClock: TimelineSchedule {
    let until: Date
    func entries(from start: Date, mode: TimelineScheduleMode) -> UnfoldFirstSequence<Date> {
        let dt = mode == .lowFrequency ? 1.0 : 1.0 / 60
        return sequence(first: start) { let n = $0.addingTimeInterval(dt); return n <= until ? n : nil }
    }
}

/// Her words (already styled: links amber and underlined) as they are said. With no `reveal`, a finished or earlier row, they are plain text.
struct RevealText: View {
    let text: AttributedString
    var reveal: Reveal?
    @Environment(\.accessibilityReduceMotion) private var reduce

    var body: some View {
        if let r = reveal, !(reduce && !r.voiced) {
            let tagged = Self.tagged(text), fade = reduce ? 0 : Reveal.fade
            TimelineView(RevealClock(until: Date(timeIntervalSinceReferenceDate: r.end(fade: fade)))) { ctx in
                tagged.textRenderer(RevealRenderer(reveal: r, t: ctx.date.timeIntervalSinceReferenceDate, fade: fade))
            }
        } else { Text(text) }
    }

    /// `text` with each word's characters tagged with the word (`Reveal.words`), its styles and links kept
    static func tagged(_ text: AttributedString) -> Text {
        pieces(text).reduce(Text("")) { $0 + Text(AttributedString(text[$1.range])).customAttribute(WordTag(word: $1.word)) }
    }

    /// the stretches of `text` that are one word each, in order
    static func pieces(_ text: AttributedString) -> [(range: Range<AttributedString.Index>, word: Int)] {
        let wordOf = Reveal.words(String(text.characters))
        var out: [(range: Range<AttributedString.Index>, word: Int)] = []
        var lo = text.startIndex, i = text.startIndex, k = 0
        while i < text.endIndex {
            let next = text.characters.index(after: i)
            if next == text.endIndex || wordOf[k + 1] != wordOf[k] { out.append((lo..<next, wordOf[k])); lo = next }
            i = next; k += 1
        }
        return out
    }
}

#if DEBUG
extension StarEngine {
    /// `-scene reveal`: an answer in Chinese and English with a link, laid out whole and lit word by word at a typed answer's old pace. The frame gaps while it runs go to Library/Caches/reveal.log.
    func runRevealScene() {
        setLevel(0)
        beginTalk(); lingerHold = true
        addTurn(his: true, "给我讲讲明天的安排，顺便查查那家小蛋糕店")
        startSay("明天下午三点你有一个和 Lumen Labs 的二面，在线上进行，大概四十五分钟。之前我会把你上次面试的笔记整理出来，放在日程里，你可以随时翻开看看。"
                 + "晚上八点半给 Mia 回电话，我到时候提醒你。Remember that the 32 bus leaves at 9:20, so you should be at the stop by nine. "
                 + "蛋糕店的地址在[这里](https://example.com/cake)，也可以直接看 https://example.org/map，它周末也开门。路上要不要我先查一下天气和出门的时间？", cps: 28)
        // the gaps between frames while it runs
        final class Gaps: NSObject {
            var last = 0.0, first = 0.0, worst = 0.0, slow = 0, n = 0, at = ""
            @objc func tick(_ l: CADisplayLink) {
                if last > 0 { let dt = l.timestamp - last; worst = max(worst, dt); n += 1; if dt > 0.03 { slow += 1; at += " \(Int((l.timestamp - first) * 1000))@\(Int(dt * 1000))" } } else { first = l.timestamp }
                last = l.timestamp
            }
        }
        let g = Gaps(), link = CADisplayLink(target: g, selector: #selector(Gaps.tick))
        link.add(to: .main, forMode: .common)
        DispatchQueue.main.asyncAfter(deadline: .now() + 9) {
            link.invalidate()
            StallLog.append("reveal scene: \(g.n) frames, worst gap \(Int(g.worst * 1000)) ms, \(g.slow) over 30 ms (ms in@gap):\(g.at)", to: "reveal.log")
        }
    }
}
#endif
