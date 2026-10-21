import SwiftUI

// Her stage, the right of the iPad's home. By default it shows 「现在」, the stretch he is in now as its opened row says it (what it was spent on, what is inside it),
// and under it 「等你」, each thing that waits on him with its own buttons, answered on the spot. A tap on a row of the list on the left puts that row here instead
// (the bar outlines its span, so the list, the bar and the stage show the same stretch); 今天 brings back what is now. It changes in place: no sheet, no push.

private let ink = SetColor.ink
private let muted = SetColor.muted
private let faint = SetColor.faint
private let amber = Color(red: 1, green: 201 / 255, blue: 143 / 255)

struct PadStage: View {
    let engine: StarEngine
    let model: DayTimelineModel?
    let width: Double
    @Environment(\.accessibilityReduceMotion) private var reduce

    private var tl: TimelineState { engine.ui.tl }
    private let side = 30.0

    var body: some View {
        let opened = tl.openRow.flatMap { id in model?.entries.first { $0.id == id } }
        ScrollView(showsIndicators: false) {
            VStack(alignment: .leading, spacing: 0) {
                if let e = opened { page(e) } else if let m = model { now(m); waiting(m) } else { waitingCards }
            }
            .padding(.horizontal, side).padding(.top, 18).padding(.bottom, 60)
            .frame(maxWidth: .infinity, alignment: .leading)
            .id(opened?.id ?? "now")
            .transition(reduce ? .opacity : .opacity.combined(with: .offset(x: 12)))
        }
        .scrollBounceBehavior(.basedOnSize)
        .animation(padMotion, value: opened?.id)
    }

    // MARK: a row of the list

    private func page(_ e: TLEntry) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Button { tl.openRow = nil } label: {
                HStack(spacing: 5) {
                    Image(systemName: "chevron.left").font(.system(size: 13, weight: .semibold))
                    Text(Words.Pad.backToNow).font(.system(size: 14, weight: .medium))
                }
                .foregroundStyle(ink.opacity(0.8)).frame(minHeight: 32).contentShape(Rectangle())
            }
            .buttonStyle(PressScale())
            head(e).padding(.top, 10)
            TLOpenView(e: e, tl: tl, engine: engine, mapWidth: width - 2 * side).padding(.top, 18)
        }
    }

    /// the row's icon in its circle, its title large, and its line: the hours, how long, where
    private func head(_ e: TLEntry) -> some View {
        HStack(alignment: .center, spacing: 12) {
            if !e.symbol.isEmpty {
                Image(systemName: e.symbol).font(.system(size: 16, weight: .medium)).foregroundStyle(e.color.map { $0.color } ?? ink.opacity(0.82))
                    .frame(width: 36, height: 36).background(Circle().fill(Color.white.opacity(0.06)))
            }
            VStack(alignment: .leading, spacing: 3) {
                Text(e.title).font(.system(size: 22, weight: .semibold)).foregroundStyle(e.unknown ? muted : ink).fixedSize(horizontal: false, vertical: true)
                Text(e.pageLine).font(.system(size: 13)).foregroundStyle(muted).fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    // MARK: now

    /// the stretch now is in, as its opened row says it; nothing when now is in none (a day before today, a gap)
    @ViewBuilder private func now(_ m: DayTimelineModel) -> some View {
        if let e = m.nowRow(), e.kind != .now {
            label(Words.Pad.now, color: muted)
            head(e).padding(.top, 8)
            TLOpenView(e: e, tl: tl, engine: engine, mapWidth: width - 2 * side).padding(.top, 16)
                .padding(.bottom, 28)
        }
    }

    // MARK: what waits

    /// the day's rows that wait on him (a reminder that rang, an event his mail made) with what their opened row offers, and the brain's cards with their own buttons
    @ViewBuilder private func waiting(_ m: DayTimelineModel) -> some View {
        let rows = m.waiting.compactMap { id in m.entries.first { $0.id == id } }, cards = Waiting.shared.cards
        if !rows.isEmpty || !cards.isEmpty {
            label(Words.Pad.waiting(rows.count + cards.count), color: amber)
            VStack(alignment: .leading, spacing: 12) {
                ForEach(rows) { e in
                    VStack(alignment: .leading, spacing: 10) {
                        HStack(spacing: 10) {
                            MarkShape(kd: .wait).frame(width: 14, height: 14)
                            Text(e.title).font(.system(size: 16, weight: .medium)).foregroundStyle(amber)
                            if let d = e.detail { Text(d).font(.system(size: 13)).foregroundStyle(muted).lineLimit(1) }
                            Spacer(minLength: 0)
                            Text(e.time).font(AT.mono(12)).foregroundStyle(faint)
                        }
                        TLOpenView(e: e, tl: tl, engine: engine, mapWidth: width - 2 * side - 32)
                    }
                    .padding(16)
                    .background(RoundedRectangle(cornerRadius: 16).fill(Color.white.opacity(0.04)))
                    .overlay(RoundedRectangle(cornerRadius: 16).stroke(amber.opacity(0.18), lineWidth: 0.5))
                }
                ForEach(cards) { c in WaitingCardView(card: c) }
            }
            .padding(.top, 10)
        }
    }

    /// with no day yet, the brain's cards still wait on him
    @ViewBuilder private var waitingCards: some View {
        let cards = Waiting.shared.cards
        if !cards.isEmpty {
            label(Words.Pad.waiting(cards.count), color: amber)
            VStack(alignment: .leading, spacing: 12) { ForEach(cards) { c in WaitingCardView(card: c) } }.padding(.top, 10)
        }
    }

    private func label(_ s: String, color: Color) -> some View {
        Text(s).font(.system(size: 12.5, weight: .semibold)).foregroundStyle(color).tracking(0.5)
    }
}
