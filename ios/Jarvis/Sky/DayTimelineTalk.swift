import SwiftUI

// A talk on the timeline home happens in her place, over the day: while it is on show her sentence, the bar and the chips give way to the latest round of it (his words, when he spoke
// them and he may want to see whether she understood; hers, as she says them), and the day's list stays where it is. The older rounds, the map of the places her answer names, the steps
// she took, and the whole of an answer too long for the place are the history's, which opens from the bottom (Sky/StarPanel.swift). This file says what the place holds (`TLTalk`, which
// needs no screen to be checked, and how long it stays after her last word) and draws it (`TLTalkArea`). The engine keeps the time (`StarEngine.tlUntil`, Sky/StarTalk.swift).

private let ink = SetColor.ink
private let muted = SetColor.muted
private let amber = Color(red: 1, green: 201 / 255, blue: 143 / 255)                // what waits on him, as in the list
private let retryAmber = Color(red: 1, green: 217 / 255, blue: 176 / 255)           // and the talk rows' own, for a message that did not go

extension TLLayout {
    /// Her place for a talk is her sentence's place grown to the room above the list: from the sentence's line down to just over the row of the pills (the way back, the count of what waits).
    static let talkBottom = 244.0
    static var talkH: Double { talkBottom - lineY }
}

/// What her place holds of a talk: its latest round, from his last words on; the rounds before it are in the history.
struct TLTalk: Equatable {
    /// what the phone heard him say, for a round he spoke; a typed round, or one a button sent (总结, 问她), shows none of his words
    var heard: String?
    /// her rows of the round that are drawn, in the order they come: her words (a line before the answer, then the answer), the status standing in her place until her words come, a turn that failed
    var hers: [LiveTurn] = []
    /// the cards she put up in the round: none is drawn here, only how many wait
    var cards = 0
    /// his message of the round did not go out
    var unsent: LiveTurn?

    var isEmpty: Bool { heard == nil && hers.isEmpty && cards == 0 && unsent == nil }
    /// he spoke the round
    var spoken: Bool { heard != nil }
    /// how many characters of her words the round has, as they show
    var chars: Int { hers.filter { $0.status == nil && !$0.failed }.reduce(0) { $0 + String(AnswerText.linked($1.text).characters).count } }

    /// How long the place stays after her last word, in ms. A round he spoke, and she said whole (what is written is her voice again), stays 4 s after her voice ends: he has heard it. Any other is
    /// read: 8 s and 0.12 s a character, at most 45 s.
    static func stay(chars: Int, said: Bool) -> Double { said ? 4000 : min(45_000, 8000 + 120 * Double(chars)) }

    /// the round of the live rows of a talk
    static func of(_ turns: [LiveTurn]) -> TLTalk {
        var t = TLTalk()
        let from = turns.lastIndex(where: \.his).map { $0 + 1 } ?? 0
        if from > 0 {
            let his = turns[from - 1], words = his.text.trimmingCharacters(in: .whitespacesAndNewlines)
            if his.heard, !words.isEmpty { t.heard = words }
            if his.send == .failed { t.unsent = his }
        }
        for r in turns[from...] {
            if r.card != nil { t.cards += 1 } else if r.status != nil || r.failed || !r.text.isEmpty { t.hers.append(r) }
        }
        return t
    }

    /// the row of her answer, which the history opens at: the answer, else the line before it; nil while she has said nothing
    var answer: Int? {
        let words = hers.filter { $0.status == nil && !$0.failed && !$0.text.isEmpty }
        return (words.last { !$0.leadIn } ?? words.last)?.id
    }
}

/// Her place for a talk, over the sentence, the bar and the chips (which `TLTopView` fades out for it): his heard words in one muted line, hers under them as she says them, and under those
/// what waits. A tap on her words, or 全文 › when they are longer than the place, opens the history at them.
struct TLTalkArea: View {
    let engine: StarEngine
    let ui: StarUI
    let model: DayTimelineModel?
    @Environment(\.accessibilityReduceMotion) private var reduce
    @State private var tall = 0.0           // how high her words are laid out: taller than the room they have, and the last line fades out with 全文 › in its corner
    private let heardH = 22.0, waitingH = 28.0          // his line with the gap under it, and the line of what waits

    var body: some View {
        let on = ui.tlTalk && ui.tlHome, talk = TLTalk.of(ui.liveTurns)
        VStack(alignment: .leading, spacing: 0) {
            if let heard = talk.heard {
                Text(heard).font(.system(size: 13)).foregroundStyle(muted).lineLimit(1).truncationMode(.middle).contentTransition(.identity)
                    .frame(maxWidth: .infinity, minHeight: 16, alignment: .leading).padding(.bottom, 6)
            }
            words(talk)
            if talk.cards > 0 { waiting(talk.cards) }
        }
        .frame(width: TLLayout.barW, height: TLLayout.talkH, alignment: .topLeading)
        .simultaneousGesture(DragGesture(minimumDistance: 0).onChanged { _ in engine.holdLinger(true) }.onEnded { _ in engine.holdLinger(false) })        // a finger on it holds it, as on the rows under the dial
        .fadeThrough(on: on, reduce: reduce)
        .allowsHitTesting(on).accessibilityHidden(!on)
        .offset(x: 30, y: TLLayout.lineY)
    }

    // MARK: her words

    /// Her rows, in the room the place has left of his line and the line of what waits: when they are taller than it the last line fades out and 全文 › stands at the foot on the right
    @ViewBuilder private func words(_ talk: TLTalk) -> some View {
        if !talk.hers.isEmpty || talk.unsent != nil {
            let room = TLLayout.talkH - (talk.heard == nil ? 0 : heardH) - (talk.cards > 0 ? waitingH : 0), over = tall > room + 1
            VStack(alignment: .leading, spacing: 6) {
                ForEach(talk.hers) { r in row(r) }
                if let u = talk.unsent { notSent(u) }
            }
            .frame(maxWidth: .infinity, alignment: .topLeading)
            .fixedSize(horizontal: false, vertical: true)
            .onGeometryChange(for: Double.self) { Double($0.size.height) } action: { tall = $0 }
            .frame(maxWidth: .infinity, alignment: .topLeading)
            .frame(height: tall > 0 ? min(tall, room) : room, alignment: .topLeading)          // as high as they are, and no higher than the room: the line of what waits comes right under them
            .clipped()
            .mask(alignment: .top) {
                VStack(spacing: 0) {
                    Color.black
                    LinearGradient(colors: [.black, .clear], startPoint: .top, endPoint: .bottom).frame(height: over ? 28 : 0)
                }
            }
            .overlay(alignment: .bottomTrailing) {
                Text("全文 ›").font(.system(size: 13)).foregroundStyle(ink.opacity(0.72)).opacity(over ? 1 : 0).accessibilityHidden(!over)
            }
            .animation(.easeOut(duration: 0.2), value: over)
            .contentShape(Rectangle())
            .onTapGesture { engine.showAnswer(talk.answer) }
            .accessibilityAddTraits(.isButton).accessibilityHint("打开对话记录")
        }
    }

    /// her typing line or the step she is on, her words, or the failure of her turn, in the one place: the line fades into her words
    private func row(_ r: LiveTurn) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            if r.failed { failed(r) } else if let s = r.status { StatusRow(line: s, size: 13).transition(.opacity) } else { written(r).transition(.opacity) }
        }
        .animation(.easeOut(duration: 0.2), value: r.status == nil)
    }

    /// Her words as the panel lays them out (a list, a table, code and a quote as blocks, shown at once; else a word at a time as she says them), at the size of her sentence
    @ViewBuilder private func written(_ r: LiveTurn) -> some View {
        let blocks = AnswerBlocks.parse(r.text)
        if blocks.contains(where: { if case .paragraph = $0 { false } else { true } }) {
            AnswerView(blocks: blocks, size: 15, lineSpacing: 3)
        } else {
            RevealText(text: styledWords(r.text, base: ink.opacity(0.9)), reveal: r.reveal)
                .font(.system(size: 15)).lineSpacing(3).tint(retryAmber).frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    /// her turn failed: it says so in her place, with the button that sends the same words again
    private func failed(_ r: LiveTurn) -> some View {
        HStack(spacing: 12) {
            Text(r.text).font(.system(size: 15)).foregroundStyle(muted)
            Button { engine.sendAgain(row: "l\(r.id)") } label: {
                Text("重试").font(.system(size: 14, weight: .semibold)).foregroundStyle(retryAmber)
                    .padding(.horizontal, 14).frame(minHeight: 32).background(Color.white.opacity(0.08), in: Capsule()).contentShape(Capsule())
            }.buttonStyle(.plain)
            Spacer(minLength: 0)
        }
    }

    /// his message did not go out: it says so, and the button sends that same message again (under the id it first had)
    private func notSent(_ his: LiveTurn) -> some View {
        Button { engine.sendAgain(row: "l\(his.id)") } label: {
            HStack(spacing: 5) {
                Image(systemName: "exclamationmark.circle.fill")
                Text("没发出去，点重试")
            }
            .font(.system(size: 13)).foregroundStyle(retryAmber).frame(minHeight: 32).contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("没发出去，点重试")
    }

    // MARK: what waits

    /// the cards she put up are in the list's own place for what waits: this goes to them
    private func waiting(_ n: Int) -> some View {
        Button { if let model { ui.tl.revealWaiting(in: model) } } label: {
            HStack(spacing: 4) {
                Text("等你 \(n) 件")
                Image(systemName: "chevron.right").font(.system(size: 10, weight: .semibold))
            }
            .font(.system(size: 13, weight: .semibold)).foregroundStyle(amber).frame(maxWidth: .infinity, minHeight: 28, alignment: .leading).contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(n) 件等你")
    }
}
