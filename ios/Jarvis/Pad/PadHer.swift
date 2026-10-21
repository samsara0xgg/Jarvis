import SwiftUI
import UIKit

// Her on the iPad: the phone's own HerView (the Metal port of the Mac's her), standing alone at the right of the top band with no glow and no plate, and the menu she
// puts out when held. On the phone the star stage drives her and the engine every frame (Sky/StarStage.swift); here this small stage does, with nothing else to draw.

/// her ball's diameter in the band
let padHerSize = 56.0

/// Her alone: one display link steps the engine (her face, her voice, the talk) and paints her. It drops to 20 fps when nothing moves, as the phone's stage does.
final class PadHerStage: UIView, HerDrive {
    let engine: StarEngine
    private let her = HerView(radius: padHerSize / 2)
    private var link: CADisplayLink?
    private var last = 0.0

    init(engine: StarEngine) {
        self.engine = engine
        super.init(frame: .zero)
        isUserInteractionEnabled = false          // the touches are the SwiftUI layer's over her (PadHerTouch)
        backgroundColor = .clear
        engine.herDrive = self
        layer.addSublayer(her.layer)
        HerView.prewarm()
    }
    required init?(coder: NSCoder) { fatalError() }
    deinit { link?.invalidate() }

    override func didMoveToWindow() {
        super.didMoveToWindow()
        link?.invalidate(); link = nil
        guard window != nil else { return }
        engine.herDrive = self
        let l = CADisplayLink(target: PadLinkProxy(self), selector: #selector(PadLinkProxy.tick(_:)))
        l.preferredFrameRateRange = CAFrameRateRange(minimum: 8, maximum: 60, preferred: 60)
        l.add(to: .main, forMode: .common)
        link = l
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        CATransaction.begin(); CATransaction.setDisableActions(true)
        her.layer.position = CGPoint(x: bounds.midX, y: bounds.midY)
        CATransaction.commit()
    }

    fileprivate func tick(_ l: CADisplayLink) {
        let now = nowMs(), dt = min(last > 0 ? (now - last) / 1000 : 1 / 60, 1 / 20)
        last = now
        let scale = Double(window?.screen.scale ?? 2)
        engine.step(now: now, dt: dt, scale: scale)
        if engine.herFresh {
            CATransaction.begin(); CATransaction.setDisableActions(true)
            her.paintLayers(scale: CGFloat(scale))
            CATransaction.commit()
        }
        let want: Float = engine.isBusy || her.isBusy ? 60 : engine.covered ? 10 : 20
        if l.preferredFrameRateRange.preferred != want { l.preferredFrameRateRange = CAFrameRateRange(minimum: 8, maximum: 60, preferred: want) }
    }

    func herUpdate(now: Double, dt: Double, expr: String, look: (Double, Double)?, hear: Double, pressed: Bool, say: Double?) {
        her.update(now: now, dt: dt, input: HerInput(expr: expr, look: look, hear: hear, pressed: pressed, say: say))
    }
    func herLookAt(_ g: (Double, Double), now: Double, ms: Double) { her.lookAt(g, now: now, ms: ms) }
    func herEffect(_ name: String, now: Double) { her.effect(name, now: now) }
}

private final class PadLinkProxy {
    weak var target: PadHerStage?
    init(_ t: PadHerStage) { target = t }
    @objc func tick(_ l: CADisplayLink) { target?.tick(l) }
}

private struct PadHerRepresentable: UIViewRepresentable {
    let engine: StarEngine
    func makeUIView(context: Context) -> PadHerStage { PadHerStage(engine: engine) }
    func updateUIView(_ v: PadHerStage, context: Context) {}
}

// MARK: - her menu

/// What she offers, as on the phone: 打字, 理一下, 历史, 回到今天 (settings is the gear to her right, not a card).
enum PadMenuItem: CaseIterable {
    case type, tidy, history, today

    var symbol: String {
        switch self {
        case .type: "keyboard"
        case .history: "text.bubble"
        case .tidy: "sparkles"
        case .today: "arrow.uturn.backward"
        }
    }

    @MainActor func title(tidying: Bool) -> String {
        switch self {
        case .type: Words.Pad.type
        case .history: Words.Pad.history
        case .tidy: tidying ? Words.Pad.tidying : Words.Pad.tidy
        case .today: Words.Pad.today
        }
    }

    /// 理一下 only where the phone offers it (today, with a brain)
    static func shown(tidyShown: Bool) -> [PadMenuItem] { allCases.filter { $0 != .tidy || tidyShown } }
}

/// Where the tiles lie: on an arc round her, from just left of straight down to just below the left (she stands at the band's right, so they open down and to the left).
enum PadArc {
    static let radius = 120.0, first = 95.0, last = 175.0

    static func centers(count n: Int, around c: CGPoint) -> [CGPoint] {
        (0..<n).map { i in
            let a = (n < 2 ? first : first + (last - first) * Double(i) / Double(n - 1)) * .pi / 180
            return CGPoint(x: c.x + radius * cos(a), y: c.y + radius * sin(a))
        }
    }
    static func hit(_ p: CGPoint, centers: [CGPoint]) -> Int? {
        let r = TLArc.tile / 2 + TLArc.slop
        return centers.firstIndex { abs(p.x - $0.x) <= r && abs(p.y - $0.y) <= r }
    }
}

/// The menu's state: out or not, the tile a finger still down is over, the tile chosen
@Observable final class PadMenu {
    var mounted = false
    var open = false
    var hover: PadMenuItem?
    var picked: PadMenuItem?
    var holding = false
    @ObservationIgnored private var task: Task<Void, Never>?

    func show(holding: Bool) {
        task?.cancel()
        hover = nil; picked = nil; self.holding = holding
        let there = mounted
        mounted = true
        Haptic.impact(.medium)
        if there { open = true } else { DispatchQueue.main.async { [self] in open = true } }         // mounted shut first, so that they come out of her
    }

    func close() {
        guard mounted else { return }
        holding = false; hover = nil; open = false
        task?.cancel()
        task = Task { @MainActor [self] in
            try? await Task.sleep(nanoseconds: UInt64(TimelineState.arcBack * 1e9))
            if !Task.isCancelled { mounted = false; picked = nil }
        }
    }

    /// a tile chosen: it pulses once, then `act` runs as they go back into her
    func pick(_ k: PadMenuItem, act: @escaping (PadMenuItem) -> Void) {
        guard mounted, picked == nil else { return }
        holding = false; hover = nil; picked = k
        Haptic.impact(.light)
        task?.cancel()
        task = Task { @MainActor [self] in
            try? await Task.sleep(nanoseconds: UInt64(TimelineState.arcPulse * 1e9))
            guard !Task.isCancelled else { return }
            act(k)
            close()
        }
    }

    func follow(_ p: CGPoint, items: [PadMenuItem], centers: [CGPoint]) {
        guard holding else { return }
        let on = PadArc.hit(p, centers: centers).map { items[$0] }
        guard on != hover else { return }
        hover = on
        if on != nil { Haptic.selection() }
    }
}

/// Her in the band with her touch: a tap talks (the phone's tap on her), a hold of 0.28 s or a touch slid off her puts out the menu, and a finger still down slides from tile to tile.
/// `center` is where she stands in the home's own space ("padHome"), which the menu's tiles are laid round.
struct PadHer: View {
    let engine: StarEngine
    let menu: PadMenu
    let items: [PadMenuItem]
    let center: CGPoint
    @State private var downAt: Date?
    @State private var held: Task<Void, Never>?
    @State private var drift = CGSize.zero              // idle, she wanders a few points and comes back
    @Environment(\.accessibilityReduceMotion) private var reduce

    var body: some View {
        PadHerRepresentable(engine: engine)
            .frame(width: padHerSize * 2.2, height: padHerSize * 2.2)          // her eyes and the motes about her stand outside the ball
            .frame(width: padHerSize, height: padHerSize)
            .offset(drift)
            .contentShape(Circle().inset(by: -10))
            .task(id: reduce) { await wander() }
            .gesture(DragGesture(minimumDistance: 0, coordinateSpace: .named("padHome"))
                .onChanged { v in changed(v) }
                .onEnded { v in ended(v) })
            .accessibilityElement()
            .accessibilityLabel(Words.Pad.her)
            .accessibilityAddTraits(.isButton)
            .accessibilityAction { engine.micTap() }
            .accessibilityAction(named: Words.Pad.type) { menu.show(holding: false) }
    }

    private func changed(_ v: DragGesture.Value) {
        if downAt == nil {
            downAt = Date()
            held = Task { @MainActor in
                try? await Task.sleep(nanoseconds: 280_000_000)
                if !Task.isCancelled, !menu.mounted { open() }
            }
            return
        }
        let off = hypot(v.location.x - center.x, v.location.y - center.y)
        if !menu.mounted && off > padHerSize / 2 + 8 { open() }          // touched and slid off her: the menu comes out at once
        if menu.mounted { menu.follow(v.location, items: items, centers: PadArc.centers(count: items.count, around: center)) }
    }

    private func ended(_ v: DragGesture.Value) {
        held?.cancel(); held = nil
        defer { downAt = nil }
        if menu.mounted {
            if menu.holding, let k = menu.hover { menu.pick(k, act: act) } else { menu.holding = false; menu.hover = nil }
            return
        }
        engine.tap(.zero, true)          // a tap on her, as on the phone: she listens (paired), or a talk that is on gives way
    }

    /// every few seconds a slow drift of up to 3 pt, and back to her place in between; none under Reduce Motion
    private func wander() async {
        guard !reduce else { drift = .zero; return }
        while !Task.isCancelled {
            try? await Task.sleep(nanoseconds: 5_000_000_000)
            let a = Double.random(in: 0..<(2 * .pi)), r = Double.random(in: 1.5...3)
            withAnimation(.easeInOut(duration: 2.5)) { drift = downAt == nil ? CGSize(width: r * cos(a), height: r * sin(a)) : .zero }
            try? await Task.sleep(nanoseconds: 3_000_000_000)
            withAnimation(.easeInOut(duration: 2.5)) { drift = .zero }
        }
    }

    private func open() {
        menu.show(holding: true)
        engine.herDrive?.herEffect("ripple", now: nowMs())
    }

    private func act(_ k: PadMenuItem) { PadHer.act(k, engine: engine) }

    @MainActor static func act(_ k: PadMenuItem, engine: StarEngine) {
        switch k {
        case .type: engine.beginEditing?()
        case .history: engine.openPanel()
        case .tidy: engine.ui.tl.tidyDay()
        case .today: engine.ui.tl.goToday()
        }
    }
}

/// The tiles over everything, laid round her: a clear layer takes a tap anywhere else as the way back.
struct PadHerMenu: View {
    let engine: StarEngine
    let menu: PadMenu
    let items: [PadMenuItem]
    let center: CGPoint
    @Environment(\.accessibilityReduceMotion) private var reduce

    var body: some View {
        let centers = PadArc.centers(count: items.count, around: center), tidying = engine.ui.tl.tidy == .running
        ZStack(alignment: .topLeading) {
            if menu.mounted {
                Color.clear.contentShape(Rectangle()).onTapGesture { menu.close() }.accessibilityHidden(true)
                ForEach(Array(items.enumerated()), id: \.element) { i, k in
                    let lit = menu.hover == k || menu.picked == k, out = menu.open
                    Button { menu.pick(k) { PadHer.act($0, engine: engine) } } label: {
                        TLHerTile(title: k.title(tidying: tidying), symbol: k.symbol, enabled: !(k == .tidy && tidying), lit: lit, working: k == .tidy && tidying && !reduce)
                    }
                    .buttonStyle(.plain)
                    .disabled(k == .tidy && tidying)
                    .scaleEffect(out || reduce ? (lit ? 1.08 : 1) : 0.4, anchor: UnitPoint(x: 0.5 + (center.x - centers[i].x) / TLArc.tile, y: 0.5 + (center.y - centers[i].y) / TLArc.tile))
                    .opacity(out ? 1 : 0)
                    .position(centers[i])
                    .animation(reduce ? .easeOut(duration: TLMotion.reducedFade) : out ? .smooth(duration: 0.3).delay(Double(i) * 0.03) : .smooth(duration: 0.2), value: out)
                    .accessibilityLabel(k.title(tidying: tidying))
                }
            }
        }
        .allowsHitTesting(menu.mounted)
        .accessibilityAddTraits(menu.mounted ? .isModal : [])
        .accessibilityAction(.escape) { menu.close() }
    }
}
