import SwiftUI

// Her stage, the right of the iPad's home. By default it shows 「现在」, the stretch he is in now as its opened row says it (what it was spent on, what is inside it),
// and under it 「等你」, each thing that waits on him with its own buttons, answered on the spot. A tap on a row of the list on the left puts that row here instead
// (the bar outlines its span, so the list, the bar and the stage show the same stretch), with what waits still under it; a tap on the same row again, or on now on the bar,
// brings back what is now. It changes in place: no sheet, no push, no way back to press.

/// A row's icon flying from its place in the list to the stage's title when the row is tapped (the one motion `padMotion`; under Reduce Motion the title just cross-fades in).
/// The rows say where their icons are (in the home's space `padHome`, which no view of the phone has), the title says where it stands, and the home draws the flight over both.
@Observable final class PadFlight {
    struct Item { let id: String; let symbol: String; let color: Color; let from: CGRect; var to: CGRect? }
    @ObservationIgnored var rects: [String: CGRect] = [:]          // row icons, rewritten while the list scrolls: kept out of observation
    @ObservationIgnored var titles: [String: CGRect] = [:]         // the stage's title icons, which may be laid out before the flight begins
    var item: Item?

    func begin(_ e: TLEntry) {
        guard !e.symbol.isEmpty, let from = rects[e.id] else { item = nil; return }
        item = Item(id: e.id, symbol: e.symbol, color: e.color.map { $0.color } ?? SetColor.ink.opacity(0.82), from: from, to: titles[e.id])
    }
    /// the title's icon is in place: the flight lands on it
    func land(_ id: String, at r: CGRect) {
        titles[id] = r
        if item?.id == id, item?.to != r { item?.to = r }
    }
}

private struct PadFlightKey: EnvironmentKey { static let defaultValue: PadFlight? = nil }
extension EnvironmentValues { var padFlight: PadFlight? { get { self[PadFlightKey.self] } set { self[PadFlightKey.self] = newValue } } }

/// the flight itself, over the home: the icon moves from the row's circle to the title's, and the title's own icon shows when it has landed
struct PadFlightView: View {
    let flight: PadFlight
    var body: some View { if let it = flight.item { Flying(flight: flight, it: it).id(it.id) } }

    private struct Flying: View {
        let flight: PadFlight
        let it: PadFlight.Item
        @State private var go = false

        var body: some View {
            let r = go ? (it.to ?? it.from) : it.from
            Image(systemName: it.symbol).font(.system(size: 16, weight: .medium)).foregroundStyle(it.color)
                .frame(width: r.width, height: r.height).background(Circle().fill(Color.white.opacity(0.06)))
                .position(x: r.midX, y: r.midY)
                .allowsHitTesting(false)
                .onChange(of: it.to != nil) { _, has in if has { start() } }
                .onAppear { if it.to != nil { start() } }
        }

        private func start() {
            guard !go else { return }
            let id = it.id
            withAnimation(padMotion) { go = true } completion: { if flight.item?.id == id { flight.item = nil } }
        }
    }
}

private let ink = SetColor.ink
private let muted = SetColor.muted
private let faint = SetColor.faint
private let amber = Color(red: 1, green: 201 / 255, blue: 143 / 255)

struct PadStage: View {
    let engine: StarEngine
    let model: DayTimelineModel?
    let width: Double
    let flight: PadFlight
    @Environment(\.accessibilityReduceMotion) private var reduce

    private var tl: TimelineState { engine.ui.tl }
    private let side = 30.0

    var body: some View {
        let opened = tl.openRow.flatMap { id in model?.entries.first { $0.id == id } }
        ScrollView(showsIndicators: false) {
            VStack(alignment: .leading, spacing: 0) {
                if let e = opened, let m = model { page(e); waiting(m, but: e.id) } else if let m = model { now(m); waiting(m) } else { waitingCards }
            }
            .padding(.horizontal, side).padding(.top, 18).padding(.bottom, PadHome.barRoom)          // its foot clear of the bar
            .frame(maxWidth: .infinity, alignment: .leading)
            .id(opened?.id ?? "now")
            .transition(reduce ? .opacity : .opacity.combined(with: .offset(x: 12)))
        }
        .scrollBounceBehavior(.basedOnSize)
        .animation(padMotion, value: opened?.id)
        .onChange(of: opened?.id) { _, id in
            guard let id, let e = model?.entries.first(where: { $0.id == id }) else { return flight.item = nil }
            if reduce { flight.item = nil } else { flight.begin(e) }
        }
    }

    // MARK: a row of the list

    private func page(_ e: TLEntry) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            head(e)
            TLOpenView(e: e, tl: tl, engine: engine, mapWidth: width - 2 * side).padding(.top, 18)
        }
        .padding(.bottom, 28)
    }

    /// the row's icon in its circle, its title large, and its line: the hours, how long, where
    private func head(_ e: TLEntry) -> some View {
        HStack(alignment: .center, spacing: 12) {
            if !e.symbol.isEmpty {
                Image(systemName: e.symbol).font(.system(size: 16, weight: .medium)).foregroundStyle(e.color.map { $0.color } ?? ink.opacity(0.82))
                    .frame(width: 36, height: 36).background(Circle().fill(Color.white.opacity(0.06)))
                    .opacity(flight.item?.id == e.id ? 0 : 1)          // while the row's icon is flying here
                    .onGeometryChange(for: CGRect.self) { $0.frame(in: .named("padHome")) } action: { flight.land(e.id, at: $0) }
            }
            VStack(alignment: .leading, spacing: 3) {
                Text(e.title).font(.system(size: 22, weight: .semibold)).foregroundStyle(e.unknown ? muted : ink).fixedSize(horizontal: false, vertical: true)
                Text(line(e)).font(.system(size: 13)).foregroundStyle(muted).fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    /// the row's line: a row still going already says when it began (`18:50 起`) in its own words at the right, so those stand first and the hours are not said again
    private func line(_ e: TLEntry) -> String {
        guard e.going, e.right.contains(e.time) else { return e.pageLine }
        return [e.right, (e.kind == .segment ? DayTimelineModel.line(e, solo: true) : e.detail) ?? ""].filter { !$0.isEmpty }.joined(separator: " · ")
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
    @ViewBuilder private func waiting(_ m: DayTimelineModel, but shown: String? = nil) -> some View {
        let rows = m.waiting.filter { $0 != shown }.compactMap { id in m.entries.first { $0.id == id } }, cards = Waiting.shared.cards          // the row on the stage is not listed again under it
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
                ForEach(cards) { c in WaitingCardView(card: c).environment(\.cardCompact, true) }
            }
            .padding(.top, 10)
        }
    }

    /// with no day yet, the brain's cards still wait on him
    @ViewBuilder private var waitingCards: some View {
        let cards = Waiting.shared.cards
        if !cards.isEmpty {
            label(Words.Pad.waiting(cards.count), color: amber)
            VStack(alignment: .leading, spacing: 12) { ForEach(cards) { c in WaitingCardView(card: c).environment(\.cardCompact, true) } }.padding(.top, 10)
        }
    }

    private func label(_ s: String, color: Color) -> some View {
        Text(s).font(.system(size: 12.5, weight: .semibold)).foregroundStyle(color).tracking(0.5)
    }
}
