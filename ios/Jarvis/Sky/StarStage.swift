import UIKit
import SwiftUI

/// A layer whose content is a closure drawn in the y-down design space of the star chart (origin at the layer's top-left plus `originY`).
final class DrawLayer: CALayer {
    var drawBlock: ((CGContext) -> Void)?
    var originY: CGFloat = 0
    override func draw(in ctx: CGContext) {
        ctx.translateBy(x: 0, y: -originY)
        drawBlock?(ctx)
    }
}

/// The sky, the dial, her, and the finger. Everything with text or buttons is SwiftUI on top (StarChartScreen).
/// One CADisplayLink drives the whole thing; it runs at 60 fps while something moves and drops to 20 when the sky only breathes.
final class StarStage: UIView, HerDrive {
    let engine: StarEngine
    private let design = CALayer()
    private let bg = CAGradientLayer()
    private let starsBox = CALayer(), rot = CALayer(), fade = CAGradientLayer()
    private let glowA = CAGradientLayer(), glowB = CAGradientLayer()
    private let dial = CALayer(), sun = CAGradientLayer(), sunMask = CAShapeLayer(), base = CALayer()
    private let map = DrawLayer(), fxLayer = DrawLayer()
    /// the month's grid: one layer per day and per weekday letter, so the compositor does the fading and scaling and nothing is redrawn per frame
    private let monthBox = CALayer()
    private var cells: [CALayer] = [], heads: [CALayer] = [], cellImg: [CGImage?] = [], headImg: [CGImage?] = []
    private let pole = CALayer(), halo = CAGradientLayer(), spikeH = CAGradientLayer(), spikeV = CAGradientLayer()
    private let her = HerView(radius: R_PS)
    private struct MapSig: Equatable { var F, q, ls, lensTh: Int; var open: String?; var n: Int; var px: Double; var calm: Int; var epoch: Int }
    private var mapSig: MapSig?
    private var link: CADisplayLink?
    private var lastTime = 0.0
    private var k = 1.0, ox = 0.0, oy = 0.0, scale = 3.0
    private var laidPx = -1.0, laidSize = CGSize.zero
    private var monKey = "", haloKey = "", sunKey = "", fxOn = false, monOn = false, skyKey = 0.0, dialKey = "", poleKey = ""

    init(engine: StarEngine) {
        self.engine = engine
        super.init(frame: .zero)
        backgroundColor = UIColor(red: 5 / 255, green: 6 / 255, blue: 13 / 255, alpha: 1)
        isMultipleTouchEnabled = true
        engine.herDrive = self
        engine.onWake = { [weak self] in self?.boost() }
        build()
        NotificationCenter.default.addObserver(self, selector: #selector(visibilityChanged), name: UIApplication.didBecomeActiveNotification, object: nil)
        NotificationCenter.default.addObserver(self, selector: #selector(visibilityChanged), name: UIApplication.willResignActiveNotification, object: nil)
    }
    required init?(coder: NSCoder) { fatalError() }
    deinit { link?.invalidate() }

    // MARK: building the layers (design points, y-down; the `design` layer is scaled to the screen)

    private func build() {
        layer.addSublayer(bg)
        design.anchorPoint = .zero; design.bounds = CGRect(x: 0, y: 0, width: W, height: H)
        layer.addSublayer(design)

        // the star disc: painted once, turned by the compositor; twinkling stars are animated dots inside it
        starsBox.frame = CGRect(x: -198, y: 0, width: 836, height: SKY_H)
        rot.frame = CGRect(x: 0, y: -126, width: 836, height: 836)
        starsBox.addSublayer(rot)
        fade.frame = CGRect(x: 0, y: 452, width: 836, height: 188)
        fade.colors = [UIColor(red: 7 / 255, green: 8 / 255, blue: 24 / 255, alpha: 0).cgColor, UIColor(red: 7 / 255, green: 8 / 255, blue: 24 / 255, alpha: 1).cgColor]
        fade.locations = [0, 0.92]
        starsBox.addSublayer(fade)
        let m = CAGradientLayer()
        m.type = .radial; m.frame = starsBox.bounds
        m.colors = [UIColor(white: 0, alpha: 0.55).cgColor, UIColor(white: 0, alpha: 0.55).cgColor, UIColor(white: 0, alpha: 1).cgColor]
        m.locations = [0, 0.784, 1]
        m.startPoint = CGPoint(x: 0.5, y: CY / SKY_H); m.endPoint = CGPoint(x: 0.5 + 250 / 836, y: CY / SKY_H + 250 / SKY_H)
        starsBox.mask = m
        design.addSublayer(starsBox)
        if !UIAccessibility.isReduceMotionEnabled {
            for s in STARS where s.tw {
                let col = starRGB(s), sz = 4 + 4 * s.b, a = 0.5 + 0.5 * s.b
                let d = CAGradientLayer()
                d.type = .radial; d.bounds = CGRect(x: 0, y: 0, width: sz, height: sz); d.position = CGPoint(x: RS + s.x, y: RS + s.y)
                d.colors = [col.cg(a), col.cg(a), col.cg(a * 0.28), col.cg(0)]
                d.locations = [0, 0.23, 0.43, 1]
                d.startPoint = CGPoint(x: 0.5, y: 0.5); d.endPoint = CGPoint(x: 1, y: 1)
                let an = CABasicAnimation(keyPath: "opacity")
                an.fromValue = 0.28; an.toValue = 1; an.duration = 2.2 + 3.4 * s.t; an.autoreverses = true; an.repeatCount = .infinity
                an.timeOffset = 4 * s.t; an.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
                d.add(an, forKey: "tw")
                rot.addSublayer(d)
            }
        }
        // the glow behind the dial
        for (g, rx, ry, cy, col, a, stop) in [(glowA, 250.0, 250.0, CY, RGB(43, 42, 122), 0.34, 0.72), (glowB, 396.0, 306.0, H, RGB(91, 58, 168), 0.13, 0.7)] {
            g.type = .radial
            g.frame = CGRect(x: -200, y: 0, width: 840, height: H)
            g.colors = [col.cg(a), col.cg(0)]; g.locations = [0, NSNumber(value: stop)]
            g.startPoint = CGPoint(x: 0.5, y: cy / H)
            g.endPoint = CGPoint(x: 0.5 + rx / 840, y: cy / H + ry / H)
            design.addSublayer(g)
        }
        // the dial
        dial.bounds = CGRect(x: 0, y: 0, width: W, height: SKY_H)
        dial.anchorPoint = CGPoint(x: CX / W, y: CY / SKY_H); dial.position = CGPoint(x: CX, y: CY)
        sun.type = .conic; sun.frame = dial.bounds; sun.mask = sunMask
        sunMask.frame = dial.bounds; sunMask.fillColor = nil; sunMask.strokeColor = UIColor.white.cgColor; sunMask.lineWidth = 2.4
        sunMask.path = CGPath(ellipseIn: CGRect(x: CX - R_BAND, y: CY - R_BAND, width: 2 * R_BAND, height: 2 * R_BAND), transform: nil)
        dial.addSublayer(sun)
        base.frame = CGRect(x: 0, y: MAP_Y, width: W, height: MAP_H)
        dial.addSublayer(base)
        map.frame = base.frame; map.originY = CGFloat(MAP_Y)
        map.drawsAsynchronously = true
        map.drawBlock = { [weak self] c in self?.engine.drawMap(c, nowMs()) }
        dial.addSublayer(map)
        design.addSublayer(dial)
        monthBox.frame = CGRect(x: 0, y: 0, width: W, height: SKY_H); monthBox.isHidden = true
        for _ in "一二三四五六日" { let l = CALayer(); l.isHidden = true; monthBox.addSublayer(l); heads.append(l); headImg.append(nil) }
        for _ in 0..<31 { let l = CALayer(); l.isHidden = true; monthBox.addSublayer(l); cells.append(l); cellImg.append(nil) }
        design.addSublayer(monthBox)
        fxLayer.frame = monthBox.frame; fxLayer.isHidden = true; fxLayer.drawsAsynchronously = true
        fxLayer.drawBlock = { [weak self] c in self?.engine.drawFx(c, nowMs()) }
        design.addSublayer(fxLayer)
        // her, with her halo
        pole.bounds = .zero; pole.position = CGPoint(x: CX, y: CY)
        halo.type = .radial; halo.frame = CGRect(x: -170, y: -170, width: 340, height: 340)
        halo.startPoint = CGPoint(x: 0.5, y: 0.5); halo.endPoint = CGPoint(x: 1, y: 1)
        halo.locations = [0, 0.46, 1]
        pole.addSublayer(halo)
        spikeH.frame = CGRect(x: -84, y: -0.5, width: 168, height: 1); spikeH.startPoint = CGPoint(x: 0, y: 0.5); spikeH.endPoint = CGPoint(x: 1, y: 0.5)
        spikeV.frame = CGRect(x: -0.5, y: -84, width: 1, height: 168); spikeV.startPoint = CGPoint(x: 0.5, y: 0); spikeV.endPoint = CGPoint(x: 0.5, y: 1)
        pole.addSublayer(spikeH); pole.addSublayer(spikeV)
        her.layer.position = .zero
        pole.addSublayer(her.layer)
        design.addSublayer(pole)
    }

    // MARK: layout

    override func layoutSubviews() {
        super.layoutSubviews()
        guard bounds.size != laidSize else { return }
        laidSize = bounds.size
        let w = Double(bounds.width), h = Double(bounds.height)
        k = max(0.5, min(w / W, h / (H - 40)))
        ox = (w - W * k) / 2; oy = (h - H * k) / 2
        scale = Double(window?.screen.scale ?? traitCollection.displayScale)
        CATransaction.begin(); CATransaction.setDisableActions(true)
        design.position = CGPoint(x: ox, y: oy); design.transform = CATransform3DMakeScale(k, k, 1)
        // the sky runs to the edges: a gradient in design rows, held flat beyond them
        bg.frame = bounds
        let rows: [(Double, UIColor)] = [(0, rgbHex(0x04050c)), (300, rgbHex(0x080a1b)), (470, rgbHex(0x090a1d)), (630, rgbHex(0x070818)), (956, rgbHex(0x06070f))]
        bg.colors = rows.map { $0.1.cgColor }
        bg.locations = rows.map { NSNumber(value: clamp((oy + $0.0 * k) / h, 0, 1)) }
        CATransaction.commit()
        relayout()
    }

    private func relayout() {
        let p = min(3, (scale * k * 4).rounded() / 4)
        if p == laidPx { return }
        laidPx = p
        engine.px = p
        engine.sprites.px = CGFloat(min(3, max(1.5, p))); engine.sprites.clear()
        CATransaction.begin(); CATransaction.setDisableActions(true)
        for l in [map, fxLayer] { l.contentsScale = CGFloat(p) }
        rot.contents = paintField(scale: CGFloat(min(2, p)))
        rot.contentsScale = CGFloat(min(2, p))
        paintBase(CGFloat(p))
        monKey = ""
        CATransaction.commit()
    }

    /// the fixed parts of the dial, painted once
    private func paintBase(_ d: CGFloat) {
        let was = (engine.cam, engine.lensM)
        engine.cam = nil; engine.lensM = 1
        let img = render(size: CGSize(width: W, height: MAP_H), scale: d) { c in
            c.translateBy(x: 0, y: -CGFloat(MAP_Y))
            self.engine.frame(c)
        }
        (engine.cam, engine.lensM) = was
        base.contents = img; base.contentsScale = d
    }

    // MARK: the clock

    override func didMoveToWindow() {
        super.didMoveToWindow()
        if window != nil { start() } else { stop() }
    }
    @objc private func visibilityChanged() { link?.isPaused = UIApplication.shared.applicationState != .active }
    private func start() {
        if link != nil { return }
        let l = CADisplayLink(target: LinkProxy(self), selector: #selector(LinkProxy.tick))
        l.preferredFrameRateRange = CAFrameRateRange(minimum: 8, maximum: 60, preferred: 60)
        l.add(to: .main, forMode: .common)
        link = l; lastTime = 0
    }
    private func stop() { link?.invalidate(); link = nil }
    private func boost() {
        guard let l = link, l.preferredFrameRateRange.preferred != 60 else { return }
        l.preferredFrameRateRange = CAFrameRateRange(minimum: 8, maximum: 60, preferred: 60)
    }

    fileprivate func tick(_ l: CADisplayLink) {
        let now = nowMs()
        let dt = min(lastTime > 0 ? (now - lastTime) / 1000 : 1 / 60, 1 / 20); lastTime = now
        engine.step(now: now, dt: dt, scale: laidPx)
        apply(now)
        let busy = engine.isBusy || her.isBusy
        if !busy && now - engine.born > 1500 { engine.warmMonth() }
        let want: Float = busy ? 60 : engine.covered ? 10 : 20
        if l.preferredFrameRateRange.preferred != want { l.preferredFrameRateRange = CAFrameRateRange(minimum: 8, maximum: 60, preferred: want) }
    }

    private func apply(_ now: Double) {
        let e = engine
        CATransaction.begin(); CATransaction.setDisableActions(true)
        defer { CATransaction.commit() }
        // the compositor turns the field and moves the dial and her for the month
        if e.sky != skyKey { rot.transform = CATransform3DMakeRotation(e.sky, 0, 0, 1); skyKey = e.sky }
        // the timeline instead of the dial (`tm` 1): the star disc dims to a third, the dial fades and shrinks a little, and she glides up to the corner, smaller
        let tm = e.tm, tlFull = e.timelineFull
        let em = Ease.inOut(e.mon), cell = e.cellOf(Int(clamp(Double(floorDay(e.F)), Double(M_FIRST), Double(M_LAST)))), intro = smooth(0, 900, now - e.born)
        let dk = String(format: "%.4f|%.0f|%.3f|%.4f", em, cell.x * 10 + cell.y, intro, tm)
        if dk != dialKey {
            dialKey = dk
            let s = (1 + (MR / R_BEZ - 1) * em) * (1 - 0.03 * tm)
            dial.transform = em > 0 || tm > 0 ? CATransform3DConcat(CATransform3DMakeScale(s, s, 1), CATransform3DMakeTranslation((cell.x - CX) * em, (cell.y - CY) * em, 0)) : CATransform3DIdentity
            dial.opacity = Float((1 - smooth(0.5, 0.95, e.mon)) * intro * (1 - tm))
            starsBox.opacity = Float(lerp(0.35 + 0.65 * intro, 0.35, tm))
        }
        let pk = String(format: "%.4f|%.2f|%.2f|%.4f|%.4f", em, e.herP.0, e.herP.1, e.herP.2, tm)
        if pk != poleKey {
            poleKey = pk
            var s = 1.0, dx = 0.0, dy = 0.0
            if em > 0 { s = 1 - 0.52 * em; dy = (POLE_MY - CY) * em }
            else if e.cam != nil { s = e.herP.2; dx = e.herP.0 - CX; dy = e.herP.1 - CY }
            if tm > 0 { s = lerp(s, TLLayout.herScale, tm); dx = lerp(dx, TLLayout.her.x - CX, tm); dy = lerp(dy, TLLayout.her.y - CY, tm) }
            pole.transform = s == 1 && dx == 0 && dy == 0 ? CATransform3DIdentity : CATransform3DConcat(CATransform3DMakeScale(s, s, 1), CATransform3DMakeTranslation(dx, dy, 0))
            halo.opacity = Float(lerp(1, 0.6, tm))
        }
        // the dial given way, nothing of it is drawn; when it comes back it is drawn again at once
        if dial.isHidden != tlFull { dial.isHidden = tlFull; if !tlFull { mapSig = nil } }
        base.isHidden = e.baseOff
        // the sky comes a little closer too
        let ss = e.fl > 0 ? 1 + 0.2 * e.camE : 1
        starsBox.transform = ss == 1 ? CATransform3DIdentity : CATransform3DMakeScale(ss, ss, 1)
        // the dial's daylight band: a conic gradient of its own when seen from above
        let flat = e.cam == nil && e.mon <= 0.985
        sun.isHidden = !flat
        if flat {
            let sk = String(format: "%.3f|%.4f|%.3f|%.3f", e.F, e.z, e.lensM, e.lensTh)
            if sk != sunKey {
                sunKey = sk
                let (a0, stops) = e.sunStops()
                var last = 0.0
                sun.colors = stops.map { $0.1.cg($0.2) }
                sun.locations = stops.map { s in last = max(last, s.0); return NSNumber(value: last) }
                sun.startPoint = CGPoint(x: CX / W, y: CY / SKY_H)
                sun.endPoint = CGPoint(x: (CX + 100 * cos(a0)) / W, y: (CY + 100 * sin(a0)) / SKY_H)
            }
        }
        // the dial is drawn again when something on it moved; at rest only its slow pulses (a waiting star breathing) need it, ten times a second
        if e.mon <= 0.985 && !tlFull {
            let sig = MapSig(F: Int((e.F * 1e4).rounded()), q: Int((e.q * 1e5).rounded()), ls: Int((e.ls.value * 1e4).rounded()), lensTh: Int((e.lensTh * 1e4).rounded()),
                             open: e.openId, n: e.EV.count, px: laidPx, calm: Int((e.calmK * 20).rounded()), epoch: e.epoch)
            let hot = sig != mapSig || now < e.wakeUntil || e.flyer != nil || e.EV.contains { now - $0.flash < 700 || now - $0.ign < 900 || now - $0.born < 650 }
            if hot || (e.pulsing && now - e.mapAt > 100 && !e.covered) { map.setNeedsDisplay(); e.mapAt = now; mapSig = sig }
        }
        map.isHidden = e.mon > 0.985
        // trails, her listening, a star flying out
        let fb = e.fxBusy && !tlFull
        if fb { fxLayer.isHidden = false; fxLayer.setNeedsDisplay(); fxOn = true } else if fxOn { fxLayer.isHidden = true; fxOn = false }
        if e.mon < 0.004 || tlFull { if monOn { monthBox.isHidden = true; monOn = false; monKey = "" } } else {
            let key = String(format: "%.3f|%.2f|%d|%d", e.mon, laidPx, floorDay(e.F), e.epoch)
            if key != monKey { monKey = key; monthBox.isHidden = false; monOn = true; layoutMonth() }
        }
        // her
        if e.herFresh { her.paintLayers(scale: CGFloat(scale)) }
        let (r, g, b) = her.rgb, hk = "\(r),\(g),\(b)"
        e.herRGB = (r, g, b)
        if hk != haloKey {
            haloKey = hk
            let c = RGB(Double(r), Double(g), Double(b))
            halo.colors = [c.cg(0.24), c.cg(0.08), c.cg(0)]
            spikeH.colors = [c.cg(0), c.cg(0.34), c.cg(0)]; spikeV.colors = spikeH.colors
        }
    }

    /// where each day's sprite sits for the month's opening: the same fades and growth the prototype draws, set on layers
    private func layoutMonth() {
        let e = engine, fd = Int(clamp(Double(floorDay(e.F)), Double(M_FIRST), Double(M_LAST))), (_, _, r0, c0) = e.cellOf(fd)
        let headA = Float(smooth(0.3, 0.9, e.mon) * 0.5)
        for (i, ch) in "一二三四五六日".enumerated() {
            let w = e.sprites.word(String(ch), .sans(500, 11), .ink, 11), l = heads[i]
            if headImg[i] !== w.img { headImg[i] = w.img; l.contents = w.img }
            l.bounds = CGRect(x: 0, y: 0, width: w.w, height: w.h)
            l.position = CGPoint(x: GX0 + GW * (Double(i) + 0.5), y: GY0 - 26)
            l.opacity = headA; l.isHidden = false
        }
        for d in M_FIRST...M_LAST {
            let cell = e.cellOf(d), far = hypot(Double(cell.row - r0), Double(cell.col - c0)), ap = smooth(0.12 + far * 0.06, 0.55 + far * 0.07, e.mon), l = cells[d - M_FIRST]
            if ap < 0.01 { l.isHidden = true; continue }
            let sp = e.miniSprite(d), sc = 0.7 + 0.3 * Ease.out(ap)
            if cellImg[d - M_FIRST] !== sp.img { cellImg[d - M_FIRST] = sp.img; l.contents = sp.img }
            l.bounds = CGRect(x: 0, y: 0, width: sp.w * sc, height: sp.h * sc)
            l.position = CGPoint(x: cell.x, y: cell.y - (MR + 3) * sc + sp.h * sc / 2)
            l.opacity = Float(ap); l.isHidden = false
        }
    }

    // MARK: HerDrive

    func herUpdate(now: Double, dt: Double, expr: String, look: (Double, Double)?, hear: Double, pressed: Bool, say: Double?) {
        her.update(now: now, dt: dt, input: HerInput(expr: expr, look: look, hear: hear, pressed: pressed, say: say))
    }
    func herLookAt(_ g: (Double, Double), now: Double, ms: Double) { her.lookAt(g, now: now, ms: ms) }
    func herEffect(_ name: String, now: Double) { her.effect(name, now: now) }

    // MARK: touch: forwarded to the engine in design points

    private func pt(_ t: UITouch) -> CGPoint {
        let p = t.location(in: self)
        return CGPoint(x: (Double(p.x) - ox) / k, y: (Double(p.y) - oy) / k)
    }
    /// the dial's hit area: the band between the top row and the panel's grabber, running past the screen's sides
    override func point(inside point: CGPoint, with event: UIEvent?) -> Bool {
        let p = CGPoint(x: (Double(point.x) - ox) / k, y: (Double(point.y) - oy) / k)
        if engine.tm > 0.5 { return hypot(Double(p.x) - TLLayout.her.x, Double(p.y) - TLLayout.her.y) < 26 }        // the timeline: only she is touched here
        return p.y >= 58 && p.y <= 528
    }
    private func id(_ t: UITouch) -> Int { ObjectIdentifier(t).hashValue }
    override func touchesBegan(_ touches: Set<UITouch>, with event: UIEvent?) {
        for t in touches { if engine.ui.typing { engine.endEditing?() }; engine.pointerDown(id(t), pt(t), t.timestamp * 1000) }
    }
    override func touchesMoved(_ touches: Set<UITouch>, with event: UIEvent?) {
        for t in touches {
            let co = event?.coalescedTouches(for: t) ?? [t]
            engine.pointerMove(id(t), (co.isEmpty ? [t] : co).map { (pt($0), $0.timestamp * 1000) })
        }
    }
    override func touchesEnded(_ touches: Set<UITouch>, with event: UIEvent?) { for t in touches { engine.pointerUp(id(t), t.timestamp * 1000, cancelled: false) } }
    override func touchesCancelled(_ touches: Set<UITouch>, with event: UIEvent?) { for t in touches { engine.pointerUp(id(t), t.timestamp * 1000, cancelled: true) } }
}

private final class LinkProxy {
    weak var target: StarStage?
    init(_ t: StarStage) { target = t }
    @objc func tick(_ l: CADisplayLink) { target?.tick(l) }
}

func rgbHex(_ v: Int) -> UIColor { UIColor(red: CGFloat((v >> 16) & 255) / 255, green: CGFloat((v >> 8) & 255) / 255, blue: CGFloat(v & 255) / 255, alpha: 1) }

struct StageRepresentable: UIViewRepresentable {
    let engine: StarEngine
    func makeUIView(context: Context) -> StarStage { StarStage(engine: engine) }
    func updateUIView(_ v: StarStage, context: Context) {}
}
