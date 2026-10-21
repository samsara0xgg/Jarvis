// words: legacy
import SwiftUI

// Her cards. Held down on the timeline (0.28 s), or touched and slid off at once (Sky/StarFlow.swift), she puts out a few small tiles in the quarter to her right and below, along an arc,
// over everything else, like thoughts: two dots leave her first and each tile pops up where it sits, a little after the one before. 理一下, the history, the way back to today.
// A finger that is still down slides from tile to tile (a tick for each) and lifting it on one chooses it; lifting it anywhere else leaves the tiles out for a tap. A tap on a tile
// chooses it, a tap anywhere else puts them away without a choice. The chosen tile pulses once and they all go.

/// what she offers, and whether each can be chosen now
struct TLHerMenu {
    enum Kind { case tidy, history, today }
    struct Item: Equatable { let kind: Kind, title: String, symbol: String, enabled: Bool }

    /// 理一下 only where the header has it (today, with a brain), as 在理… and not to be chosen while the brain works; the history; the way back to today
    static func items(tidy: TLTidy, tidyShown: Bool) -> [Item] {
        let running = tidy == .running
        return (tidyShown ? [Item(kind: .tidy, title: running ? "在理…" : "理一下", symbol: "sparkles", enabled: !running)] : [])
            + [Item(kind: .history, title: "聊天记录", symbol: "text.bubble", enabled: true), Item(kind: .today, title: "回到今天", symbol: "arrow.uturn.backward", enabled: true)]
    }
}

/// where the tiles lie: on an arc centred on her, from 5° below the horizontal to 85° (toward straight down), in the chrome's design points
enum TLArc {
    static let tile = 52.0, corner = 14.0, radius = 105.0, first = 5.0, last = 85.0
    static let dots: [(d: Double, r: Double)] = [(28, 2.5), (40, 4)]      // the thought dots off her toward the middle tile: distance from her, radius
    static let slop = 4.0                              // a finger this far off a tile is still on it

    static func centers(count n: Int, around c: CGPoint = TLLayout.her) -> [CGPoint] {
        (0..<n).map { i in
            let a = (n < 2 ? first : first + (last - first) * Double(i) / Double(n - 1)) * .pi / 180
            return CGPoint(x: c.x + radius * cos(a), y: c.y + radius * sin(a))
        }
    }

    /// the tile under `p`, if any
    static func hit(_ p: CGPoint, centers: [CGPoint]) -> Int? {
        let r = tile / 2 + slop
        return centers.firstIndex { abs(p.x - $0.x) <= r && abs(p.y - $0.y) <= r }
    }
}

extension TimelineState {
    var herItems: [TLHerMenu.Item] { TLHerMenu.items(tidy: tidy, tidyShown: tidyShown) }
    static let arcPulse = 0.14, arcBack = 0.3          // how long the chosen tile pulses, and how long the tiles are on screen after they start back

    /// She puts out her cards; `holding`: the finger that held her down is still down, and may slide onto one.
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
            switch k {
            case .tidy: tidyDay()
            case .history: engine.openPanel()
            case .today: goToday()
            }
            closeArc()
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

/// The cards over everything: a clear layer that takes a tap anywhere outside them as the way back, the thought dots, and the tiles on their arc.
struct HerArc: View {
    let engine: StarEngine
    let ui: StarUI
    @Environment(\.accessibilityReduceMotion) private var reduce

    private var tl: TimelineState { ui.tl }
    private static let navy = Color(red: 8 / 255, green: 10 / 255, blue: 24 / 255)      // as the chat's tapback bar (Sky/StarTapback.swift)

    var body: some View {
        let items = tl.herItems
        let centers = TLArc.centers(count: items.count)
        ZStack(alignment: .topLeading) {
            if tl.arcMounted {
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

    private func dot(_ d: Double, _ r: Double, rank: Int) -> some View {
        let a = 45.0 * .pi / 180, out = tl.arcOpen
        return Circle().fill(SetColor.ink.opacity(0.55)).frame(width: r * 2, height: r * 2)
            .scaleEffect(out || reduce ? 1 : 0.2).opacity(out ? 1 : 0)
            .position(x: TLLayout.her.x + d * cos(a), y: TLLayout.her.y + d * sin(a))
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

    /// the symbol on top, the word under it, on the chat's dark frosted ground
    private func face(_ item: TLHerMenu.Item, lit: Bool) -> some View {
        let working = item.kind == .tidy && tl.tidy == .running && !reduce
        let shape = RoundedRectangle(cornerRadius: TLArc.corner, style: .continuous)
        return VStack(spacing: 4) {
            Image(systemName: item.symbol).font(.system(size: 18)).symbolEffect(.pulse, options: .repeating, isActive: working)
            Text(item.title).font(.system(size: 10, weight: .medium)).lineLimit(1).minimumScaleFactor(0.8)
        }
        .foregroundStyle(SetColor.ink.opacity(item.enabled ? 0.95 : 0.45))
        .frame(width: TLArc.tile, height: TLArc.tile)
        .background(shape.fill(Color.white.opacity(lit ? 0.14 : 0)))
        .background(shape.fill(.ultraThinMaterial)).background(shape.fill(Self.navy.opacity(0.8)))
        .overlay(shape.strokeBorder(Color.white.opacity(lit ? 0.4 : 0.14), lineWidth: 0.5))
    }
}
