import SwiftUI
import UIKit

// Her on the iPad: the phone's own HerView (the Metal port of the Mac's her), standing alone at the top centre of every page with no glow and no plate. On the phone the star
// stage drives her and the engine every frame (Sky/StarStage.swift); here this small stage does, with nothing else to draw.

/// her ball's diameter (the mock's 60)
let padHerSize = 60.0

/// Her alone: one display link steps the engine (her face, her voice, the talk) and paints her. It drops to 20 fps when nothing moves, as the phone's stage does.
final class PadHerStage: UIView, HerDrive {
    let engine: StarEngine
    private let her = HerView(radius: padHerSize / 2)
    private var link: CADisplayLink?
    private var last = 0.0

    init(engine: StarEngine) {
        self.engine = engine
        super.init(frame: .zero)
        isUserInteractionEnabled = false          // the touches are the SwiftUI layer's over her (PadHer)
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

/// Her at the top centre with her touch, as on the phone: a tap talks (the phone's tap on her), a hold of 0.28 s or a touch slid off her goes to 聊天 with the keyboard (`StarEngine.typeToHer`).
struct PadHer: View {
    let engine: StarEngine
    @State private var down = false
    @State private var gone = false                     // this touch has gone to 聊天: its lift is no tap
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
            .gesture(DragGesture(minimumDistance: 0)
                .onChanged { v in changed(v) }
                .onEnded { _ in ended() })
            .accessibilityElement()
            .accessibilityLabel(Words.Home.her).accessibilityHint(Words.Home.herHint)
            .accessibilityAddTraits(.isButton)
            .accessibilityAction { engine.micTap() }
            .accessibilityAction(named: Words.Home.type) { engine.typeToHer() }
    }

    private func changed(_ v: DragGesture.Value) {
        if !down {
            down = true; gone = false
            held = Task { @MainActor in
                try? await Task.sleep(nanoseconds: 280_000_000)
                if !Task.isCancelled { type() }
            }
            return
        }
        if hypot(v.translation.width, v.translation.height) > padHerSize / 2 + 8 { type() }          // touched and slid off her: at once
    }

    private func ended() {
        held?.cancel(); held = nil
        down = false
        if !gone { engine.tap(.zero, true) }          // a tap on her, as on the phone: she listens (paired), or a talk that is on gives way
    }

    private func type() {
        guard !gone else { return }
        gone = true; held?.cancel()
        engine.typeToHer()
    }

    /// every few seconds a slow drift of up to 3 pt, and back to her place in between; none under Reduce Motion
    private func wander() async {
        guard !reduce else { drift = .zero; return }
        while !Task.isCancelled {
            try? await Task.sleep(nanoseconds: 5_000_000_000)
            let a = Double.random(in: 0..<(2 * .pi)), r = Double.random(in: 1.5...3)
            withAnimation(.easeInOut(duration: 2.5)) { drift = down ? .zero : CGSize(width: r * cos(a), height: r * sin(a)) }
            try? await Task.sleep(nanoseconds: 3_000_000_000)
            withAnimation(.easeInOut(duration: 2.5)) { drift = .zero }
        }
    }
}
