import SwiftUI

// Her cards. Held down on the timeline (0.28 s), or touched and slid off at once (Sky/StarFlow.swift), she puts out a few small tiles on a fan under her, over everything else, like thoughts:
// two dots leave her first and each tile pops up where it sits, a little after the one before. 打字, 理一下, the history, the way back to today.
// A finger that is still down slides from tile to tile (a tick for each) and lifting it on one chooses it; lifting it anywhere else leaves the tiles out for a tap. A tap on a tile
// chooses it, a tap anywhere else puts them away without a choice. The chosen tile pulses once and they all go.

/// what she offers, and whether each can be chosen now
struct TLHerMenu {
    enum Kind { case type, tidy, history, today }
    struct Item: Equatable { let kind: Kind, title: String, symbol: String, enabled: Bool }

    /// The field to type to her in; 理一下 only where it can be asked for (today, with a brain), as 在理… and not to be chosen while the brain works; the history; the way back to today.
    @MainActor static func items(tidy: TLTidy, tidyShown: Bool) -> [Item] {
        let running = tidy == .running, w = Words.Home.self
        return [Item(kind: .type, title: w.type, symbol: "keyboard", enabled: true)]
            + (tidyShown ? [Item(kind: .tidy, title: running ? w.tidying : w.tidy, symbol: "wand.and.rays", enabled: !running)] : [])
            + [Item(kind: .history, title: w.history, symbol: "text.bubble", enabled: true), Item(kind: .today, title: w.backToToday, symbol: "arrow.uturn.backward", enabled: true)]
    }
}

/// Where the tiles lie: on a fan under her, its middle 120° round from her right (down and to the left, toward the middle of the screen she sits right of), a tile every 40°, in the chrome's
/// design points. With four that is from down and to the right (60°) round to her left at her own height (180°).
enum TLArc {
    static let tile = 52.0, corner = 14.0, radius = 110.0, middle = 120.0, step = 40.0
    static let dots: [(d: Double, r: Double)] = [(13, 2.5), (25, 4)]      // the thought dots off her toward the middle of the fan: how far past her edge, radius
    static let slop = 4.0                              // a finger this far off a tile is still on it

    /// the angle of each of `n` tiles, in degrees round from her right, y down
    static func angles(count n: Int) -> [Double] { (0..<n).map { middle + step * (Double($0) - Double(n - 1) / 2) } }

    static func centers(count n: Int, around c: CGPoint = TLSeat.home) -> [CGPoint] {
        angles(count: n).map { a in CGPoint(x: c.x + radius * cos(a * .pi / 180), y: c.y + radius * sin(a * .pi / 180)) }
    }

    /// the tile under `p`, if any
    static func hit(_ p: CGPoint, centers: [CGPoint]) -> Int? {
        let r = tile / 2 + slop
        return centers.firstIndex { abs(p.x - $0.x) <= r && abs(p.y - $0.y) <= r }
    }
}

extension TimelineState {
    /// what she offers now (the words are the table's, which is the main thread's: everything here runs on it)
    var herItems: [TLHerMenu.Item] {
        let t = tidy, shown = tidyShown
        return MainActor.assumeIsolated { TLHerMenu.items(tidy: t, tidyShown: shown) }
    }
    static let arcPulse = 0.14, arcBack = 0.3          // how long the chosen tile pulses, and how long the tiles are on screen after they start back

    /// where the tile a finger that is still down is on stands (none: it is on none)
    var arcHoverAt: CGPoint? {
        guard let k = arcHover else { return nil }
        let items = herItems
        return items.firstIndex { $0.kind == k }.map { TLArc.centers(count: items.count)[$0] }
    }

    /// She puts out her cards, always round her home: off on a drift she hurries back to it for them (Sky/DayTimelineSeat.swift), so they are where his hand knows them. `holding`: the
    /// finger that held her down is still down, and may slide onto one.
    func openArc(holding: Bool) {
        arcTask?.cancel()
        arcHover = nil; arcPicked = nil; arcHolding = holding
        let there = arcMounted
        arcMounted = true
        Haptic.impact(.medium)
        if there { arcOpen = true } else { DispatchQueue.main.async { [self] in arcOpen = true } }          // mounted shut first, so that they come out of her
    }

    /// The cards go back into her, with no choice (a tap anywhere else, the day turning, a choice made).
    func closeArc() {
        guard arcMounted else { return }
        arcHolding = false; arcHover = nil; arcOpen = false
        arcTask?.cancel()
        arcTask = Task { @MainActor [self] in
            try? await Task.sleep(nanoseconds: UInt64(Self.arcBack * 1e9))
            if !Task.isCancelled { arcMounted = false; arcPicked = nil }
        }
    }

    /// A card chosen: it pulses once, then what it says is done as they all go back into her. A card that cannot be chosen now (理一下 while the brain works) is not.
    func pickArc(_ k: TLHerMenu.Kind, engine: StarEngine) {
        guard arcMounted, arcPicked == nil, herItems.first(where: { $0.kind == k })?.enabled == true else { return }
        arcHolding = false; arcHover = nil; arcPicked = k
        Haptic.impact(.light)
        arcTask?.cancel()
        arcTask = Task { @MainActor [self] in
            try? await Task.sleep(nanoseconds: UInt64(Self.arcPulse * 1e9))
            guard !Task.isCancelled else { return }
            perform(k, engine: engine)
            closeArc()
        }
    }

    /// what a card says, done (a card chosen, or VoiceOver's action on her)
    func perform(_ k: TLHerMenu.Kind, engine: StarEngine) {
        switch k {
        case .type: engine.beginEditing?()
        case .tidy: tidyDay()
        case .history: engine.openPanel()
        case .today: goToday()
        }
    }

    /// the finger that held her down moves to `p` (design points): the card under it is lit, with a tick for each change
    func arcFollow(_ p: CGPoint) {
        guard arcHolding else { return }
        let items = herItems
        let on = TLArc.hit(p, centers: TLArc.centers(count: items.count)).map { items[$0] }.flatMap { $0.enabled ? $0.kind : nil }
        guard on != arcHover else { return }
        arcHover = on
        if on != nil { Haptic.selection() }
    }

    /// the finger that held her down lifts: on a card it chooses it; anywhere else (or taken away) the cards stay out for a tap
    func arcLetGo(engine: StarEngine, cancelled: Bool) {
        guard arcHolding else { return }
        arcHolding = false
        if !cancelled, let k = arcHover { pickArc(k, engine: engine) } else { arcHover = nil }
    }
}

/// The cards over everything: the day dimmed behind them, a clear layer that takes a tap anywhere outside them as the way back, the thought dots, and the tiles on their fan.
struct HerArc: View {
    let engine: StarEngine
    let ui: StarUI
    @Environment(\.accessibilityReduceMotion) private var reduce

    private var tl: TimelineState { ui.tl }

    var body: some View {
        let items = tl.herItems
        let centers = TLArc.centers(count: items.count)
        ZStack(alignment: .topLeading) {
            if tl.arcMounted {
                shade
                Color.clear.frame(width: W * 3, height: H * 3).offset(x: -W, y: -H)
                    .contentShape(Rectangle())
                    .onTapGesture { tl.closeArc() }
                    .accessibilityHidden(true)
                ForEach(Array(TLArc.dots.enumerated()), id: \.offset) { i, d in dot(d.d, d.r, rank: i) }
                ForEach(Array(items.enumerated()), id: \.element.kind) { i, item in tile(item, rank: i, at: centers[i]) }
            }
        }
        .frame(width: W, height: H, alignment: .topLeading)
        .allowsHitTesting(tl.arcMounted)
        .accessibilityElement(children: .contain)
        .accessibilityAddTraits(tl.arcMounted ? .isModal : [])
        .accessibilityAction(.escape) { tl.closeArc() }
    }

    /// pops where it sits: from small to a touch over its size and back (a spring), `delay` after she is pressed; away, it shrinks and fades in place, all at once
    private func pop(_ delay: Double) -> Animation {
        reduce ? .easeOut(duration: TLMotion.reducedFade) : tl.arcOpen ? .spring(response: 0.3, dampingFraction: 0.58).delay(delay) : .easeIn(duration: 0.16)
    }

    /// While they are out the day under them dims, all but a soft round of sky where she is, so the cards read as hers and the words between them step back.
    private var shade: some View {
        let home = TLSeat.home, clear = TLSeat.radius * TLSeat.listening + 8, soft = 30.0
        return RadialGradient(stops: [.init(color: .black.opacity(0), location: clear / (clear + soft)), .init(color: .black.opacity(0.4), location: 1)],
                              center: UnitPoint(x: (W + home.x) / (3 * W), y: (H + home.y) / (3 * H)), startRadius: 0, endRadius: clear + soft)
            .frame(width: W * 3, height: H * 3).offset(x: -W, y: -H)
            .opacity(tl.arcOpen ? 1 : 0)
            .animation(reduce ? .easeOut(duration: TLMotion.reducedFade) : .easeOut(duration: 0.2), value: tl.arcOpen)
            .allowsHitTesting(false).accessibilityHidden(true)
    }

    /// a thought dot, `d` past her edge toward the middle of the fan
    private func dot(_ d: Double, _ r: Double, rank: Int) -> some View {
        let a = TLArc.middle * .pi / 180, out = tl.arcOpen, from = engine.seatR.value + d
        return Circle().fill(SetColor.ink.opacity(0.55)).frame(width: r * 2, height: r * 2)
            .scaleEffect(out || reduce ? 1 : 0.2).opacity(out ? 1 : 0)
            .position(x: TLSeat.home.x + from * cos(a), y: TLSeat.home.y + from * sin(a))
            .animation(pop(Double(rank) * 0.04), value: out)
            .allowsHitTesting(false).accessibilityHidden(true)
    }

    /// One tile, popping up in its place a little after the dots and the tile before it. Reduce Motion: a fade.
    private func tile(_ item: TLHerMenu.Item, rank: Int, at c: CGPoint) -> some View {
        let out = tl.arcOpen, lit = tl.arcHover == item.kind, picked = tl.arcPicked == item.kind
        let grow = reduce ? 1 : picked ? 1.14 : lit ? 1.08 : 1
        return Button { tl.pickArc(item.kind, engine: engine) } label: { face(item, lit: lit || picked) }
            .buttonStyle(.plain)
            .disabled(!item.enabled)
            .scaleEffect(out || reduce ? grow : 0.5)
            .opacity(out ? 1 : 0)
            .position(x: c.x, y: c.y)
            .animation(pop(0.07 + Double(rank) * 0.045), value: out)
            .animation(.spring(response: 0.22, dampingFraction: 0.6), value: grow)
            .accessibilityLabel(item.title)
            .accessibilityHidden(!tl.arcOpen)
    }

    private func face(_ item: TLHerMenu.Item, lit: Bool) -> some View {
        TLHerTile(title: item.title, symbol: item.symbol, enabled: item.enabled, lit: lit, working: item.kind == .tidy && tl.tidy == .running && !reduce)
    }
}

/// One of her cards: the symbol on top, the word under it, on the chat's dark frosted ground. The iPad's menu (Pad/PadHer.swift) wears the same tiles.
struct TLHerTile: View {
    let title: String
    let symbol: String
    var enabled = true
    var lit = false
    var working = false                 // what it starts is under way: the symbol pulses
    private static let navy = Color(red: 8 / 255, green: 10 / 255, blue: 24 / 255)      // as the chat's tapback bar (Sky/StarTapback.swift)

    var body: some View {
        let shape = RoundedRectangle(cornerRadius: TLArc.corner, style: .continuous)
        VStack(spacing: 4) {
            Image(systemName: symbol).font(.system(size: 18)).symbolEffect(.pulse, options: .repeating, isActive: working)
            Text(title).font(.system(size: 10, weight: .medium)).lineLimit(1).minimumScaleFactor(0.8)
        }
        .foregroundStyle(SetColor.ink.opacity(enabled ? 0.95 : 0.45))
        .frame(width: TLArc.tile, height: TLArc.tile)
        .background(shape.fill(Color.white.opacity(lit ? 0.14 : 0)))
        .background(shape.fill(.ultraThinMaterial)).background(shape.fill(Self.navy.opacity(0.8)))
        .overlay(shape.strokeBorder(Color.white.opacity(lit ? 0.4 : 0.14), lineWidth: 0.5))
    }
}
