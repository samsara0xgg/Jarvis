import CoreText
import Metal
import QuartzCore
import UIKit

// Her (星核): the view. Layer structure mirrors the prototype's mount/paintLayers:
//   back  (orbit motes behind)  ->  glass (CAMetalLayer, pose transform)  ->  front (blush, voice arc, ripple,
//   eyes drawn apart with a glow, motes in front, particles).
// No display link in here: the owner calls update(...) then paintLayers(scale:) from its own CADisplayLink.

struct HerInput {
    var expr: String? = nil
    var look: (Double, Double)? = nil  // gaze in -1...1
    var hear: Double = 0
    var pressed: Bool = false
    var say: Double? = nil  // nil = not speaking, else voice level 0...1
}

/// The prototype's remaining `update` inputs. Optional: the defaults are what the prototype uses when they are absent.
struct HerExtra {
    var energy = 0.6, joy = 0.5
    var poi: (g: (Double, Double), at: Double)? = nil
    var dim = 0.0
    var vel: (Double, Double) = (0, 0)
    var still = false
    var pet = false
    var light: String? = nil
}

private enum HerMetal {
    static let device = MTLCreateSystemDefaultDevice()
    static let queue = device?.makeCommandQueue()
    private static let lock = NSLock()
    private static var built: MTLRenderPipelineState?
    private static var started = false

    /// Compiles the shader off the main thread on the first call; nil until it is done (never blocks the caller).
    /// The owner may call this early (e.g. at launch) so the first frame is not waiting.
    static func pipelineIfReady() -> MTLRenderPipelineState? {
        lock.lock(); defer { lock.unlock() }
        if !started {
            started = true
            DispatchQueue.global(qos: .userInitiated).async {
                let p = compile()
                lock.lock(); built = p; lock.unlock()
            }
        }
        return built
    }

    private static func compile() -> MTLRenderPipelineState? {
        guard let device = device else { return nil }
        do {
            let lib = try device.makeLibrary(source: HerShaderSource.msl, options: nil)
            let d = MTLRenderPipelineDescriptor()
            d.vertexFunction = lib.makeFunction(name: "herVertex"); d.fragmentFunction = lib.makeFunction(name: "herFragment")
            d.colorAttachments[0].pixelFormat = .bgra8Unorm
            return try device.makeRenderPipelineState(descriptor: d)
        } catch {
            NSLog("HerView: glass shader failed to build: %@", String(describing: error))
            return nil
        }
    }
}

private final class HerDrawLayer: CALayer {
    var painter: ((CGContext) -> Void)?
    override func draw(in ctx: CGContext) { painter?(ctx) }
}

private struct HerHash {
    var h: UInt64 = 0xcbf2_9ce4_8422_2325
    mutating func add(_ v: Double, _ k: Double) { add(Int64((v * k).rounded())) }
    mutating func add(_ i: Int64) { h = (h ^ UInt64(bitPattern: i)) &* 0x100_0000_01b3 }
}

private enum HerGfx {
    static let space = CGColorSpace(name: CGColorSpace.sRGB)!
    static let blush: CGGradient = gradient([(1, 0.47, 0.667, 1), (1, 0.47, 0.667, 0)], [0, 1])
    static let flash: CGGradient = gradient([(1, 1, 1, 0.95), (235 / 255, 240 / 255, 1, 0.6), (235 / 255, 240 / 255, 1, 0)], [0, 0.7, 1])
    /// the "z" of sleep, as a path at font size 100 (y up)
    static let zPath: CGPath? = {
        let font = UIFont.monospacedSystemFont(ofSize: 100, weight: .semibold) as CTFont
        var g = CGGlyph(0)
        var ch: UniChar = 122
        guard CTFontGetGlyphsForCharacters(font, &ch, &g, 1) else { return nil }
        return CTFontCreatePathForGlyph(font, g, nil)
    }()

    static func gradient(_ c: [(CGFloat, CGFloat, CGFloat, CGFloat)], _ loc: [CGFloat]) -> CGGradient {
        var comps = [CGFloat]()
        for k in c { comps += [k.0, k.1, k.2, k.3] }
        return CGGradient(colorSpace: space, colorComponents: comps, locations: loc, count: c.count)!
    }
}

final class HerView: UIView {
    var extra = HerExtra()
    /// Ray-march quality of the glass (the prototype's `q`, 3).
    var quality = 3.0

    private let R: CGFloat
    private let side: CGFloat
    private let core = HerCore()
    private let backLayer = HerDrawLayer()
    private let glassLayer = CAMetalLayer()
    private let frontLayer = HerDrawLayer()
    private let inflight = DispatchSemaphore(value: 2)

    private var scale: CGFloat = 0
    private var poseKey: (Int, Int, Int, Int)? = nil
    private var frontSig: UInt64? = nil
    private var frontTick: UInt64 = 0
    private var backSig: UInt64 = 0
    private var orbitGrad: CGGradient? = nil
    private var orbitKey = -1

    /// Start compiling the glass shader off the main thread (optional; the first paintLayers does it otherwise).
    static func prewarm() { _ = HerMetal.pipelineIfReady() }

    var rgb: (Int, Int, Int) { core.rgb }
    var isBusy: Bool { core.busy }

    init(radius: CGFloat) {
        R = radius
        side = ceil(4.4 * radius)
        super.init(frame: CGRect(x: 0, y: 0, width: side, height: side))
        isUserInteractionEnabled = false
        backgroundColor = .clear
        isOpaque = false
        core.reduced = UIAccessibility.isReduceMotionEnabled
        NotificationCenter.default.addObserver(self, selector: #selector(motionChanged),
                                               name: UIAccessibility.reduceMotionStatusDidChangeNotification, object: nil)

        for l in [backLayer, frontLayer] {
            l.frame = bounds
            l.isOpaque = false
            l.contentsFormat = .RGBA8Uint
            l.actions = ["contents": NSNull(), "contentsScale": NSNull(), "bounds": NSNull(), "position": NSNull()]
            l.drawsAsynchronously = true
        }
        backLayer.painter = { [unowned self] c in self.paintBack(c) }
        frontLayer.painter = { [unowned self] c in self.paintFront(c) }

        let g = 2 * CGFloat(herB) * radius
        glassLayer.bounds = CGRect(x: 0, y: 0, width: g, height: g)
        glassLayer.position = CGPoint(x: side / 2, y: side / 2)
        glassLayer.device = HerMetal.device
        glassLayer.pixelFormat = .bgra8Unorm
        glassLayer.framebufferOnly = true
        glassLayer.isOpaque = false
        glassLayer.backgroundColor = nil
        glassLayer.maximumDrawableCount = 3
        glassLayer.actions = ["contents": NSNull(), "transform": NSNull(), "bounds": NSNull(), "position": NSNull()]

        layer.addSublayer(backLayer)
        layer.addSublayer(glassLayer)
        layer.addSublayer(frontLayer)
    }

    required init?(coder: NSCoder) { fatalError("HerView is code-only") }

    @objc private func motionChanged() { core.reduced = UIAccessibility.isReduceMotionEnabled }

    // MARK: API

    /// now in milliseconds (CACurrentMediaTime() * 1000), dt in seconds. Pure state update, no drawing.
    func update(now: Double, dt: Double, input: HerInput) {
        var ci = HerCoreInput()
        ci.expr = input.expr ?? "02"
        if let l = input.look { ci.look = HerV2(l.0, l.1) }
        ci.pressed = input.pressed; ci.hear = input.hear; ci.say = input.say
        ci.still = extra.still; ci.pet = extra.pet; ci.vel = HerV2(extra.vel.0, extra.vel.1)
        ci.energy = extra.energy; ci.joy = extra.joy; ci.dim = extra.dim
        if let p = extra.poi { ci.poi = HerPoi(g: HerV2(p.g.0, p.g.1), at: p.at) }
        if let n = extra.light { ci.light = HerLights.byName[n] }
        core.update(now: now, dt: dt, input: ci)
    }

    func lookAt(_ g: (Double, Double), now: Double, ms: Double) { core.lookAt(HerV2(g.0, g.1), now, ms) }
    func effect(_ name: String, now: Double) { core.effect(name, now) }

    /// One frame (the prototype's paintLayers(px)); scale = screen pixels per point.
    func paintLayers(scale d: CGFloat) {
        let pose = core.pose, R = Double(self.R), dd = Double(d)
        if d != scale {
            scale = d
            for l in [backLayer, frontLayer, glassLayer] as [CALayer] { l.contentsScale = d }
            frontSig = nil; backSig = .max
        }

        // glass: render only when the picture would visibly change
        let S = max(8, Int((2 * herB * R * dd).rounded()))
        if Int(glassLayer.drawableSize.width) != S { glassLayer.drawableSize = CGSize(width: S, height: S) }
        if var u = core.glassFrame(S: S, q: quality), submit(&u) { core.didRender(u) }

        let key = (Int((pose.jx * R * 100).rounded()), Int((pose.jy * R * 100).rounded()),
                   Int((pose.bx * 10000).rounded()), Int((pose.by * 10000).rounded()))
        if poseKey == nil || key != poseKey! {
            poseKey = key
            CATransaction.begin(); CATransaction.setDisableActions(true)
            glassLayer.setAffineTransform(CGAffineTransform(a: pose.bx, b: 0, c: 0, d: pose.by, tx: pose.jx * R, ty: pose.jy * R))
            CATransaction.commit()
        }

        // back: only while motes orbit (and once more to clear)
        let st = core.st
        var hb = HerHash()
        if st.orbitK > 0.01 {
            let k = R * dd * 2
            hb.add(st.t * 1.5 * 1.24 * k, 1); hb.add(st.orbitK, 100); hb.add(st.yOff, k)
            for v in [core.light.glow.x, core.light.glow.y, core.light.glow.z] { hb.add(v, 255) }
        } else { hb.h = 0 }
        if hb.h != backSig { backSig = hb.h; backLayer.setNeedsDisplay() }

        // front: only when something visible moved (half-pixel resolution)
        let sig = frontSignature(pose: pose, R: R, d: dd)
        if sig != frontSig { frontSig = sig; frontLayer.setNeedsDisplay() }
    }

    // MARK: glass (Metal)

    private func submit(_ u: inout HerUniforms) -> Bool {
        guard let queue = HerMetal.queue, let pipe = HerMetal.pipelineIfReady() else { return false }
        if inflight.wait(timeout: .now()) != .success { return false }  // never block the main thread on the GPU
        guard let drawable = glassLayer.nextDrawable(), let cb = queue.makeCommandBuffer() else { inflight.signal(); return false }
        let pass = MTLRenderPassDescriptor()
        pass.colorAttachments[0].texture = drawable.texture
        pass.colorAttachments[0].loadAction = .clear
        pass.colorAttachments[0].clearColor = MTLClearColor(red: 0, green: 0, blue: 0, alpha: 0)
        pass.colorAttachments[0].storeAction = .store
        guard let enc = cb.makeRenderCommandEncoder(descriptor: pass) else { inflight.signal(); return false }
        enc.setRenderPipelineState(pipe)
        enc.setVertexBytes(&u, length: MemoryLayout<HerUniforms>.stride, index: 0)
        enc.setFragmentBytes(&u, length: MemoryLayout<HerUniforms>.stride, index: 0)
        enc.drawPrimitives(type: .triangle, vertexStart: 0, vertexCount: 3)
        enc.endEncoding()
        let sem = inflight
        cb.addCompletedHandler { _ in sem.signal() }
        cb.present(drawable)
        cb.commit()
        return true
    }

    // MARK: 2D layers

    private func frontSignature(pose: (jx: Double, jy: Double, bx: Double, by: Double), R: Double, d: Double) -> UInt64 {
        let st = core.st, k = R * d * 2
        if !core.fx.isEmpty { frontTick &+= 1; return frontTick }  // particles move every frame
        var h = HerHash()
        h.add(pose.jx, k); h.add(pose.jy, k); h.add(pose.bx, k); h.add(pose.by, k)
        for e in core.eyes {
            h.add(e.c.x, k); h.add(e.c.y, k); h.add(e.w, k); h.add(e.lid, 64); h.add(e.cut, k); h.add(e.cutT, 4); h.add(e.cutB, k)
            h.add(e.a, 64); h.add(e.head, 4)
            for i in 0..<e.n { h.add(e.pts[i].x, k); h.add(e.pts[i].y, k) }
        }
        let l = core.light
        for v in [l.glow.x, l.glow.y, l.glow.z, l.eye.x, l.eye.y, l.eye.z] { h.add(v, 255) }
        h.add(st.bright, 100); h.add(st.blush, 100)
        if st.voice > 0.01 { h.add(st.voice, 100); h.add(st.env, 100) }
        if st.ripple >= 0 { h.add(st.ripple, k) }
        if st.flash >= 0 { h.add(st.flash, k) }
        if st.orbitK > 0.01 { h.add(st.t * 1.5 * 1.24 * k, 1); h.add(st.orbitK, 100); h.add(st.yOff, k) }
        return h.h
    }

    private func paintBack(_ c: CGContext) {
        c.clear(CGRect(x: 0, y: 0, width: side, height: side))
        c.translateBy(x: side / 2, y: side / 2)
        orbit(c, side: -1)
    }

    private func paintFront(_ c: CGContext) {
        c.clear(CGRect(x: 0, y: 0, width: side, height: side))
        c.translateBy(x: side / 2, y: side / 2)
        let pose = core.pose, R = Double(self.R), st = core.st
        // what sits on her glass but is not the glass
        c.saveGState()
        c.translateBy(x: pose.jx * R, y: pose.jy * R); c.scaleBy(x: pose.bx, y: pose.by)
        extras(c, R, st)
        glassOverlay(c, R, st)
        c.restoreGState()
        // the eyes, drawn apart and glowing: shadow on the whole group
        let glow = core.light.glow
        let blur = 0.34 * min(1.2, 0.4 + 0.6 * st.bright)
        c.saveGState()
        c.setShadow(offset: .zero, blur: CGFloat(blur * R),
                    color: CGColor(srgbRed: glow.x, green: glow.y, blue: glow.z, alpha: 0.9))
        c.beginTransparencyLayer(auxiliaryInfo: nil)
        c.translateBy(x: pose.jx * R, y: pose.jy * R); c.scaleBy(x: pose.bx, y: pose.by)
        let eye = core.light.eye
        for E in core.eyes where E.a >= 0.01 { drawEye(c, R, E, eye) }
        c.endTransparencyLayer()
        c.restoreGState()
        orbit(c, side: 1)
        particles(c, R)
    }

    private func extras(_ c: CGContext, _ R: Double, _ st: HerState) {
        if st.blush > 0.01 {
            c.saveGState(); c.setBlendMode(.plusLighter); c.setAlpha(CGFloat(0.42 * st.blush))
            for e in core.eyes {
                c.saveGState()
                c.translateBy(x: e.c.x * R * 1.25, y: (e.c.y + 0.22) * R); c.scaleBy(x: 0.24 * R, y: 0.24 * R)
                c.drawRadialGradient(HerGfx.blush, startCenter: .zero, startRadius: 0, endCenter: .zero, endRadius: 1, options: [])
                c.restoreGState()
            }
            c.restoreGState()
        }
        if st.voice > 0.01 {
            let a = st.voice * (0.25 + 0.75 * st.env / max(0.2, st.voice)), g = core.light.glow
            c.saveGState()
            c.setStrokeColor(red: g.x, green: g.y, blue: g.z, alpha: 0.55 * a)
            c.setLineCap(.round); c.setLineWidth(R * 0.06)
            c.addArc(center: .zero, radius: R * 0.84, startAngle: (90 - 22 - 18 * st.env) * herDEG,
                     endAngle: (90 + 22 + 18 * st.env) * herDEG, clockwise: false)
            c.strokePath()
            c.restoreGState()
        }
    }

    private func glassOverlay(_ c: CGContext, _ R: Double, _ st: HerState) {
        let g = core.light.glow
        if st.ripple >= 0 {
            let k = st.ripple
            c.saveGState()
            c.addArc(center: .zero, radius: R, startAngle: 0, endAngle: herTAU, clockwise: false); c.clip()
            c.setStrokeColor(red: g.x, green: g.y, blue: g.z, alpha: 0.6 * (1 - k)); c.setLineWidth(R * 0.07 * (1 - k * 0.5))
            c.addArc(center: CGPoint(x: 0, y: 0.05 * R), radius: R * (0.15 + 0.95 * herSmooth(0, 1, k)), startAngle: 0, endAngle: herTAU, clockwise: false)
            c.strokePath()
            c.restoreGState()
        }
        if st.flash >= 0 {
            let a = (1 - st.flash) * (1 - st.flash), r = R * (1.05 + 0.35 * st.flash)
            c.saveGState(); c.setAlpha(CGFloat(a)); c.scaleBy(x: r, y: r)
            c.drawRadialGradient(HerGfx.flash, startCenter: .zero, startRadius: 0, endCenter: .zero, endRadius: 1, options: [])
            c.restoreGState()
        }
    }

    private func drawEye(_ c: CGContext, _ R: Double, _ E: HerEyeGeom, _ col: HerV3) {
        c.saveGState()
        c.setAlpha(CGFloat(E.a))
        c.translateBy(x: E.c.x * R, y: E.c.y * R)
        let lid = max(0.06, E.lid), hw = E.w * R / 2
        var x0 = 1e9, x1 = -1e9, y0 = 1e9, y1 = -1e9
        for i in 0..<E.n {
            let x = (E.pts[i].x - E.c.x) * R, y = (E.pts[i].y - E.c.y) * R
            x0 = min(x0, x); x1 = max(x1, x); y0 = min(y0, y * lid); y1 = max(y1, y * lid)
        }
        let top = y0 - hw * lid, bot = y1 + hw * lid, H = bot - top, W = x1 - x0 + 2 * hw, big = W + H + 20
        if E.cut > 0.004 {
            let yc = top + E.cut * H, k = tan(((E.side < 0 ? E.cutT : -E.cutT) + E.head) * herDEG)
            c.beginPath()
            c.move(to: CGPoint(x: -big, y: yc - k * big)); c.addLine(to: CGPoint(x: big, y: yc + k * big))
            c.addLine(to: CGPoint(x: big, y: big)); c.addLine(to: CGPoint(x: -big, y: big)); c.closePath(); c.clip()
        }
        if E.cutB > 0.004 {
            let yb = bot - E.cutB * H, half = W / 2 + 2, sag = H * 0.4, mx = (x0 + x1) / 2
            c.beginPath()
            c.move(to: CGPoint(x: mx - half, y: yb + sag))
            c.addQuadCurve(to: CGPoint(x: mx + half, y: yb + sag), control: CGPoint(x: mx, y: yb - sag))
            c.addLine(to: CGPoint(x: mx + half, y: -big)); c.addLine(to: CGPoint(x: mx - half, y: -big)); c.closePath(); c.clip()
        }
        c.scaleBy(x: 1, y: CGFloat(lid))
        c.setLineCap(.round); c.setLineJoin(.round)
        func path() {
            c.beginPath()
            for i in 0..<E.n {
                let p = CGPoint(x: (E.pts[i].x - E.c.x) * R, y: (E.pts[i].y - E.c.y) * R)
                if i == 0 { c.move(to: p) } else { c.addLine(to: p) }
            }
        }
        path()
        c.setStrokeColor(red: col.x, green: col.y, blue: col.z, alpha: 1); c.setLineWidth(E.w * R); c.strokePath()
        c.setBlendMode(.plusLighter)
        path()
        c.setStrokeColor(red: 1, green: 1, blue: 1, alpha: 0.45); c.setLineWidth(E.w * R * 0.42); c.strokePath()
        c.restoreGState()
    }

    private func orbit(_ c: CGContext, side sd: Double) {
        let st = core.st, R = Double(self.R)
        if st.orbitK < 0.01 { return }
        let g = core.light.glow
        let gk = Int((g.x * 255).rounded()) << 16 | Int((g.y * 255).rounded()) << 8 | Int((g.z * 255).rounded())
        if gk != orbitKey || orbitGrad == nil {
            orbitKey = gk
            orbitGrad = HerGfx.gradient([(g.x, g.y, g.z, 1), (g.x, g.y, g.z, 0)], [0, 1])
        }
        c.saveGState(); c.setBlendMode(.plusLighter)
        for i in 0..<5 {
            let a = st.t * 1.5 + Double(i) * herTAU / 5, z = sin(a)
            if (z > 0 ? 1.0 : z < 0 ? -1.0 : 0) != sd { continue }
            let x = cos(a) * 1.24 * R, y = (-0.62 + 0.2 * z) * R + st.yOff * R, r = R * (0.045 + 0.02 * z)
            c.saveGState()
            c.translateBy(x: x, y: y); c.scaleBy(x: r * 3, y: r * 3); c.setAlpha(CGFloat(0.5 * st.orbitK))
            c.drawRadialGradient(orbitGrad!, startCenter: .zero, startRadius: 0, endCenter: .zero, endRadius: 1, options: [])
            c.restoreGState()
            c.setFillColor(red: 1, green: 1, blue: 1, alpha: 0.9 * st.orbitK * (0.6 + 0.4 * z * sd))
            c.fillEllipse(in: CGRect(x: x - r, y: y - r, width: 2 * r, height: 2 * r))
        }
        c.restoreGState()
    }

    private func particles(_ c: CGContext, _ R: Double) {
        let glow = core.light.glow
        for p in core.fx {
            let k = p.age / p.life, fade = min(1, p.age / 0.2) * (1 - herSmooth(0.6, 1, k))
            c.saveGState()
            if p.kind == .z {
                if let zp = HerGfx.zPath {
                    let size = ((p.s + 0.14 * k) * R * 10).rounded() / 10
                    c.translateBy(x: p.x * R, y: p.y * R); c.scaleBy(x: size / 100, y: -size / 100)
                    c.addPath(zp)
                    c.setFillColor(red: glow.x, green: glow.y, blue: glow.z, alpha: 0.85 * fade); c.fillPath()
                }
            } else {
                c.translateBy(x: p.x * R, y: p.y * R); c.rotate(by: p.r)
                let sc = p.c ?? glow
                c.setShadow(offset: .zero, blur: CGFloat(R * 0.1), color: CGColor(srgbRed: sc.x, green: sc.y, blue: sc.z, alpha: 0.9))
                let r = p.s * R * (p.kind == .spark ? sin(k * herPI) : 1), f = p.c ?? HerV3(1, 1, 1)
                c.beginPath()
                c.move(to: CGPoint(x: 0, y: -r))
                c.addQuadCurve(to: CGPoint(x: r, y: 0), control: .zero); c.addQuadCurve(to: CGPoint(x: 0, y: r), control: .zero)
                c.addQuadCurve(to: CGPoint(x: -r, y: 0), control: .zero); c.addQuadCurve(to: CGPoint(x: 0, y: -r), control: .zero)
                c.setFillColor(red: f.x, green: f.y, blue: f.z, alpha: fade); c.fillPath()
            }
            c.restoreGState()
        }
    }
}
