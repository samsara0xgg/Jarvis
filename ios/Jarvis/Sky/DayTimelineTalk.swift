// words: legacy
import SwiftUI

// Her card (跨端UI标准 §2): one small glass card under her on 今天, 地图 and 设置, over the page and under her. The page under it never moves; what it covers of the day's pinned top slides
// out (`StarUI.herCardFoot`), and the line under her on each page gives way to it (`StarUI.herCardOn`). It holds a talk (his heard words in grey, when he spoke them and he may want to see
// whether she understood; hers, as she says them, in ink, two lines at most; the earlier rounds of the same talk above them, and past 180 pt it scrolls), else her voice's line or what she says
// of tidying today. On 聊天 the conversation is the page: there it holds only her voice's line. This file says what it holds (`TLTalk`, which needs no screen to be checked, and how long it stays
// after her last word) and draws it (`HerWordsCard`). The engine keeps the time (`StarEngine.tlUntil`, Sky/StarTalk.swift).

private let ink = SetColor.ink
private let muted = SetColor.muted
private let amber = Look.waits                                                      // what waits on him, as in the list
private let retryAmber = Color(red: 1, green: 217 / 255, blue: 176 / 255)           // and the talk rows' own, for a message that did not go

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

    /// How long the place stays after her last word, in ms, spoken or written: until he does something else (scrolls the day, opens a row, goes to another day or a page, leaves the app:
    /// `StarEngine.releaseTalk`), and if he does nothing, ten minutes. Her sentence of the day comes back in its place.
    static let stay = 600_000.0

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

    /// The rounds of the talk before its latest, oldest first: what he said and the last of her words in each (the card's dialog above the latest round)
    static func earlier(_ turns: [LiveTurn]) -> [TLPastRound] {
        let his = turns.indices.filter { turns[$0].his }
        return zip(his, his.dropFirst()).compactMap { from, to in
            let words = turns[(from + 1)..<to].filter { !$0.his && $0.card == nil && $0.status == nil && !$0.failed && !$0.text.isEmpty }
            guard let hers = words.last(where: { !$0.leadIn }) ?? words.last else { return nil }
            return TLPastRound(id: hers.id, his: turns[from].text.trimmingCharacters(in: .whitespacesAndNewlines), hers: hers.text)
        }
    }

    /// the row of her answer, which the history opens at: the answer, else the line before it; nil while she has said nothing
    var answer: Int? {
        let words = hers.filter { $0.status == nil && !$0.failed && !$0.text.isEmpty }
        return (words.last { !$0.leadIn } ?? words.last)?.id
    }
}

/// A round of the talk before its latest, as her card shows it: her row's id (which 聊天 opens at), his words, hers
struct TLPastRound: Identifiable, Equatable {
    let id: Int
    let his: String
    let hers: String
}

/// Her card, under her: a talk's rounds, else her line (`StarUI.herLine`), on glass. A tap on her words, or 去聊天接着说 when they are longer than the two lines the card gives them, opens 聊天
/// at them. With more than one round on it, a tap anywhere outside it folds it back into her. It tells how far down it reaches (`StarUI.herCardFoot`).
struct HerWordsCard: View {
    let engine: StarEngine
    let ui: StarUI
    @Environment(\.accessibilityReduceMotion) private var reduce
    @State private var tall = 0.0           // how high her words are laid out: taller than two lines, and the last line fades out with 去聊天接着说 under it
    @State private var height = 0.0         // how tall the whole card is
    @State private var dialogH = 0.0        // how tall its rounds are, laid out whole: past `dialogRoom` they scroll
    @State private var kept = ""            // her line, kept while the card fades out
    private let wordsRoom = 44.0, width = 376.0, dialogRoom = 180.0
    static let top = 110.0                  // just under her (her foot is at 109)

    var body: some View {
        let on = ui.herCardOn, talk = !ui.panelOpen && ui.tlTalk
        let now = TLTalk.of(ui.liveTurns), earlier = talk ? TLTalk.earlier(ui.liveTurns) : []
        ZStack(alignment: .top) {
            if on && !earlier.isEmpty {
                Color.clear.contentShape(Rectangle()).frame(width: W, height: H).onTapGesture { engine.releaseTalk() }          // a tap outside the dialog folds it
            }
            VStack(spacing: 0) {
                if talk {
                    if earlier.isEmpty { round(now) } else { dialog(earlier, now) }
                } else {
                    Text(kept).font(.system(size: Look.note)).foregroundStyle(Look.ink2).lineLimit(2).multilineTextAlignment(.center)
                }
            }
            .padding(.horizontal, 16).padding(.vertical, 10)
            .frame(maxWidth: width)
            .background(Look.card.opacity(0.55), in: RoundedRectangle(cornerRadius: 20, style: .continuous))
            .jarvisGlass(RoundedRectangle(cornerRadius: 20, style: .continuous))
            .onGeometryChange(for: Double.self) { Double($0.size.height) } action: { height = $0 }
            .simultaneousGesture(DragGesture(minimumDistance: 0).onChanged { _ in engine.holdLinger(true) }.onEnded { _ in engine.holdLinger(false) })        // a finger on it holds it, as on the rows under the dial
            .offset(y: Self.top)
            .fadeThrough(on: on, reduce: reduce)
            .allowsHitTesting(on).accessibilityHidden(!on)
        }
        .frame(width: W, height: H, alignment: .top)
        .onChange(of: ui.herLine, initial: true) { _, l in if let l { kept = l } }
        .onChange(of: on ? Self.top + height : 0, initial: true) { _, f in ui.herCardFoot = f }
    }

    /// the latest round: his heard words, hers, what waits
    private func round(_ talk: TLTalk) -> some View {
        VStack(spacing: 0) {
            if let heard = talk.heard {
                Text(heard).font(.system(size: Look.note)).foregroundStyle(muted).lineLimit(1).truncationMode(.middle).contentTransition(.identity)
                    .frame(maxWidth: .infinity, minHeight: 16).padding(.bottom, 3)
            }
            words(talk)
            if talk.cards > 0 { waiting(talk.cards) }
        }
    }

    /// A back-and-forth on one page: the earlier rounds above the latest, in a small dialog that scrolls past 180 pt and stands at its foot
    private func dialog(_ earlier: [TLPastRound], _ now: TLTalk) -> some View {
        ScrollView {
            VStack(spacing: 10) {
                ForEach(earlier) { r in
                    VStack(spacing: 2) {
                        if !r.his.isEmpty { Text(r.his).font(.system(size: Look.note)).foregroundStyle(muted).lineLimit(1).truncationMode(.middle) }
                        Text(r.hers).font(.system(size: Look.body)).foregroundStyle(Look.ink2).lineLimit(3).multilineTextAlignment(.center)
                    }
                    .frame(maxWidth: .infinity)
                    .contentShape(Rectangle())
                    .onTapGesture { engine.showAnswer(r.id) }
                }
                round(now)
            }
            .onGeometryChange(for: Double.self) { Double($0.size.height) } action: { dialogH = $0 }
        }
        .defaultScrollAnchor(.bottom)
        .scrollBounceBehavior(.basedOnSize)
        .scrollIndicators(.hidden)
        .frame(height: min(max(dialogH, 1), dialogRoom))
    }

    // MARK: her words

    /// Her rows, in two lines: when they are taller the last line fades out and 去聊天接着说 stands under them
    @ViewBuilder private func words(_ talk: TLTalk) -> some View {
        if !talk.hers.isEmpty || talk.unsent != nil {
            let over = tall > wordsRoom + 1
            VStack(spacing: 0) {
            VStack(spacing: 6) {
                ForEach(talk.hers) { r in row(r) }
                if let u = talk.unsent { notSent(u) }
            }
            .frame(maxWidth: .infinity)
            .fixedSize(horizontal: false, vertical: true)
            .onGeometryChange(for: Double.self) { Double($0.size.height) } action: { tall = $0 }
            .frame(maxWidth: .infinity)
            .animation(reduce ? nil : .smooth(duration: 0.2)) { $0.frame(height: tall > 0 ? min(tall, wordsRoom) : wordsRoom, alignment: .top) }          // as high as they are, and no higher than two lines
            .clipped()
            .mask(alignment: .top) {
                VStack(spacing: 0) {
                    Color.black
                    LinearGradient(colors: [.black, .clear], startPoint: .top, endPoint: .bottom).frame(height: over ? 22 : 0)
                }
            }
            if over {
                Text(Words.PhoneCard.goOn).font(.system(size: Look.note, weight: .medium)).foregroundStyle(ink.opacity(0.72)).frame(maxWidth: .infinity, minHeight: 22).transition(.opacity)
            }
            }
            .animation(.easeOut(duration: 0.2), value: over)
            .contentShape(Rectangle())
            .onTapGesture { engine.showAnswer(talk.answer) }
            .accessibilityAddTraits(.isButton).accessibilityHint("打开对话记录")
        }
    }

    /// her typing line or the step she is on, her words, or the failure of her turn, in the one place: the line fades into her words
    private func row(_ r: LiveTurn) -> some View {
        VStack(alignment: .center, spacing: 0) {
            if r.failed { failed(r) } else if let s = r.status { StatusRow(line: s, size: 13, hug: true).frame(maxWidth: .infinity).transition(.opacity) } else { written(r).transition(.opacity) }
        }
        .animation(.easeOut(duration: 0.2), value: r.status == nil)
    }

    /// Her words as the panel lays them out (a list, a table, code and a quote as blocks, shown at once; else a word at a time as she says them), at the size of her answer
    @ViewBuilder private func written(_ r: LiveTurn) -> some View {
        let blocks = AnswerBlocks.parse(r.text)
        if blocks.contains(where: { if case .paragraph = $0 { false } else { true } }) {
            AnswerView(blocks: blocks, size: Look.body, lineSpacing: 3)
        } else {
            RevealText(text: styledWords(r.text, base: ink), reveal: r.reveal)
                .font(.system(size: Look.body, weight: .medium)).lineSpacing(3).multilineTextAlignment(.center).tint(retryAmber).frame(maxWidth: .infinity)
        }
    }

    /// her turn failed: it says so in her place, with the button that sends the same words again
    private func failed(_ r: LiveTurn) -> some View {
        HStack(spacing: 12) {
            Text(r.text).font(.system(size: Look.body)).foregroundStyle(muted).lineLimit(2)
            Button { engine.sendAgain(row: "l\(r.id)") } label: {
                Text("重试").font(.system(size: 14, weight: .semibold)).foregroundStyle(retryAmber)
                    .padding(.horizontal, 14).frame(minHeight: 32).background(Color.white.opacity(0.08), in: Capsule()).contentShape(Capsule())
            }.buttonStyle(.plain)
        }
    }

    /// his message did not go out: it says so, and the button sends that same message again (under the id it first had)
    private func notSent(_ his: LiveTurn) -> some View {
        Button { engine.sendAgain(row: "l\(his.id)") } label: {
            HStack(spacing: 5) {
                Image(systemName: "exclamationmark.circle.fill")
                Text("没发出去，点重试")
            }
            .font(.system(size: Look.note)).foregroundStyle(retryAmber).frame(minHeight: 32).contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("没发出去，点重试")
    }

    // MARK: what waits

    /// the cards she put up are in the list's own place for what waits: this goes to them, on 今天
    private func waiting(_ n: Int) -> some View {
        Button { engine.showPage(.day); ui.tl.waitingTap += 1 } label: {
            HStack(spacing: 4) {
                Text("等你 \(n) 件")
                Image(systemName: "chevron.right").font(.system(size: 10, weight: .semibold))
            }
            .font(.system(size: Look.note, weight: .semibold)).foregroundStyle(amber).frame(maxWidth: .infinity, minHeight: 28).contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(n) 件等你")
    }
}

@MainActor extension StarUI {
    /// what her card says when no talk is on it: her voice's line (the model download, what went wrong), else what she says of tidying today
    var herLine: String? {
        if let voiceNote { return voiceNote }
        return tl.tidy != .idle && tl.day == tl.today ? tl.tidy.words : nil
    }
    /// her card is up: she is in her place over the timeline and no finger is on the day's bar (its minute stands under her then); on 聊天 only for her line
    var herCardOn: Bool {
        guard HomeModule.shared.isTimeline, tlHome, tl.scrub == nil else { return false }
        return panelOpen ? voiceNote != nil : tlTalk || herLine != nil
    }
}
