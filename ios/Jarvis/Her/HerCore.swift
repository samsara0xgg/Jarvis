import Foundation
import simd

// Her (星核): the state machine. Ported from the HTML prototype's springs, `eyeSet`/`project`, and `class Core`
// (update, gaze, life layers, antics, effects). Drawing lives in HerView.swift, the glass shader in Her.metal.

let herPI = Double.pi, herTAU = 2 * Double.pi, herDEG = Double.pi / 180
let herB = 1.3  // half-size of the square the glass layer covers, in ball radii

@inline(__always) func herClamp(_ v: Double, _ a: Double, _ b: Double) -> Double { v < a ? a : v > b ? b : v }
@inline(__always) func herSmooth(_ a: Double, _ b: Double, _ x: Double) -> Double {
    let t = herClamp((x - a) / (b - a), 0, 1); return t * t * (3 - 2 * t)
}
@inline(__always) func herRnd(_ a: Double, _ b: Double) -> Double { Double.random(in: a...b) }
@inline(__always) func herLerp(_ a: Double, _ b: Double, _ k: Double) -> Double { a + (b - a) * k }

// MARK: springs

/// Damped harmonic spring; damping 1 never overshoots. `step` returns whether it still moves.
struct HerSpring {
    var value: Double
    var velocity = 0.0
    init(_ v: Double) { value = v }

    @inline(__always)
    mutating func step(_ goal: Double, _ hz: Double, _ damping: Double, _ dt: Double) -> Bool {
        let w = 2 * Double.pi * hz
        var left = dt
        while left > 1e-6 {
            let h = min(left, 1.0 / 240)
            velocity += (-w * w * (value - goal) - 2 * damping * w * velocity) * h
            value += velocity * h
            left -= 1.0 / 240
        }
        if abs(value - goal) < 1e-3 && abs(velocity) < 1e-2 { value = goal; velocity = 0; return false }
        return true
    }
}

struct HerEyeTarget {
    var x = 0.0, y = 0.0, len = 0.0, w = 0.0, rot = 0.0, bend = 0.0, lid = 1.0, cut = 0.0, cutT = 0.0, cutB = 0.0, a = 1.0
}

struct HerEyeSprings {
    var x, y, len, w, rot, bend, lid, cut, cutT, cutB, a: HerSpring
    init(_ t: HerEyeTarget) {
        x = HerSpring(t.x); y = HerSpring(t.y); len = HerSpring(t.len); w = HerSpring(t.w); rot = HerSpring(t.rot)
        bend = HerSpring(t.bend); lid = HerSpring(t.lid); cut = HerSpring(t.cut); cutT = HerSpring(t.cutT)
        cutB = HerSpring(t.cutB); a = HerSpring(t.a)
    }

    mutating func step(_ t: HerEyeTarget, hz: Double, damping: Double, dt: Double) -> Bool {
        var m = false
        if x.step(t.x, hz, damping, dt) { m = true }
        if y.step(t.y, hz, damping, dt) { m = true }
        if len.step(t.len, hz, damping, dt) { m = true }
        if w.step(t.w, hz, damping, dt) { m = true }
        if rot.step(t.rot, hz, damping, dt) { m = true }
        if bend.step(t.bend, hz, damping, dt) { m = true }
        if lid.step(t.lid, hz, damping, dt) { m = true }
        if cut.step(t.cut, hz, damping, dt) { m = true }
        if cutT.step(t.cutT, hz, damping, dt) { m = true }
        if cutB.step(t.cutB, hz, damping, dt) { m = true }
        if a.step(t.a, 14, 1, dt) { m = true }  // alpha always uses its own fast critically damped spring
        return m
    }

    var values: HerEyeTarget {
        HerEyeTarget(x: x.value, y: y.value, len: len.value, w: w.value, rot: rot.value, bend: bend.value, lid: lid.value,
                     cut: cut.value, cutT: cutT.value, cutB: cutB.value, a: a.value)
    }
}

func herEyeTargets(_ s: HerShape) -> (HerEyeTarget, HerEyeTarget) {
    (HerEyeTarget(x: -s.sep, y: s.y, len: s.len, w: s.w, rot: 90 + s.tilt + s.lean, bend: s.bend, lid: s.lid, cut: s.cut,
                  cutT: s.cutT, cutB: s.cutB, a: s.aL ?? 1),
     HerEyeTarget(x: s.sep, y: s.y + (s.yR ?? 0), len: s.lenR ?? s.len, w: s.wR ?? s.w, rot: 90 - (s.tiltR ?? s.tilt) + s.lean,
                  bend: s.bendR ?? s.bend, lid: s.lidR ?? s.lid, cut: s.cutR ?? s.cut, cutT: s.cutTR ?? s.cutT,
                  cutB: s.cutBR ?? s.cutB, a: s.aR ?? 1))
}

func herMoodShape(_ s: HerShape, _ joy: Double) -> HerShape {
    var o = s
    let flat = abs(s.tilt) > 60
    if joy > 0.55 && !flat { o.cutB = max(o.cutB, (joy - 0.55) * 0.62) }
    if joy > 0.55 && flat && s.bend > 0 { o.bend = s.bend + (joy - 0.55) * 0.12 }
    if joy < 0.45 && !flat && s.cut < 0.05 { o.cut = (0.45 - joy) * 0.55; o.cutT = -16; o.y += (0.45 - joy) * 0.08 }
    return o
}

// MARK: eye geometry (projected onto the glass)

struct HerEyeGeom {
    var pts = [HerV2](repeating: .zero, count: 9)  // preallocated; `n` of them are live
    var n = 0
    var c = HerV2.zero
    var w = 0.0, lid = 1.0, cut = 0.0, cutT = 0.0, cutB = 0.0, a = 1.0, side = -1.0, head = 0.0
}

@inline(__always)
private func project(_ x: Double, _ y: Double, _ yaw: Double, _ pitch: Double) -> (Double, Double, Double) {
    let z = (max(0, 1 - x * x - y * y)).squareRoot()
    let x1 = x * cos(yaw) + z * sin(yaw), z1 = -x * sin(yaw) + z * cos(yaw)
    let y1 = y * cos(pitch) + z1 * sin(pitch), z2 = -y * sin(pitch) + z1 * cos(pitch)
    return (x1, y1, z2)
}

/// `eyeSet` of the prototype, written into preallocated storage.
func herEyeSet(_ out: inout [HerEyeGeom], _ L: HerEyeTarget, _ R: HerEyeTarget, _ gx: Double, _ gy: Double, _ turn: Double, _ head: Double) {
    let rf = 0.9, yaw = gx * 0.5 + turn, pitch = gy * 0.4, h = head * herDEG
    for (idx, e) in [L, R].enumerated() {
        let side = idx == 0 ? -1.0 : 1.0
        let cx = e.x * cos(h) - e.y * sin(h), cy = e.x * sin(h) + e.y * cos(h)
        let a = (e.rot + head) * herDEG, ux = cos(a) * e.len / 2, uy = sin(a) * e.len / 2
        var nx = -sin(a), ny = cos(a)
        if ny > 0 { nx = -nx; ny = -ny }
        let b = e.bend * 2, n = abs(e.bend) > 0.005 ? 9 : 2
        out[idx].n = n
        for i in 0..<n {
            let t = Double(i) / Double(n - 1), u = 1 - t
            let x = cx + u * u * -ux + 2 * u * t * nx * b + t * t * ux
            let y = cy + u * u * -uy + 2 * u * t * ny * b + t * t * uy
            let p = project(x / rf, y / rf, yaw, pitch)
            out[idx].pts[i] = HerV2(p.0 * rf, p.1 * rf)
        }
        let pc = project(cx / rf, cy / rf, yaw, pitch)
        out[idx].c = HerV2(pc.0 * rf, pc.1 * rf)
        out[idx].w = e.w * (0.5 + 0.5 * max(0, pc.2))
        out[idx].lid = e.lid; out[idx].cut = e.cut; out[idx].cutT = e.cutT; out[idx].cutB = e.cutB
        out[idx].a = e.a * herSmooth(-0.02, 0.22, pc.2)
        out[idx].side = side; out[idx].head = head
    }
}

private func blinkCurve(_ k: Double) -> Double {
    k < 0 ? 1 : k < 0.075 ? 1 - 0.92 * pow(k / 0.075, 2) : k < 0.11 ? 0.08
        : k < 0.26 ? 0.08 + 0.92 * (1 - pow(1 - (k - 0.11) / 0.15, 3)) : 1
}

// MARK: input / output types

struct HerPoi { var g: HerV2; var at: Double }

/// Everything `Core.update` reads. `HerView` fills the public subset from `HerInput`; the rest comes from `HerExtra`.
struct HerCoreInput {
    var expr = "02"
    var look: HerV2? = nil
    var still = false
    var pressed = false
    var pet = false
    var vel = HerV2.zero
    var hear = 0.0
    var say: Double? = nil
    var energy = 0.6, joy = 0.5
    var poi: HerPoi? = nil
    var dim = 0.0
    var light: HerLight? = nil
}

struct HerParticle {
    enum Kind { case star, z, spark }
    var kind: Kind
    var x: Double, y: Double, vx: Double, vy: Double, age = 0.0, life: Double, s: Double, r: Double
    var c: HerV3? = nil
}

private enum HerAct { case nod, flinch, wince, bulge, wiggle, drift, yawn, sigh, glow, swirl }
private struct HerActRun { var kind: HerAct; var at: Double; var dur: Double; var amp: Double; var dir: Double }
private func actMs(_ k: HerAct) -> Double {
    switch k {
    case .nod: return 460
    case .flinch: return 620
    case .wince: return 700
    case .bulge: return 700
    case .wiggle: return 800
    case .drift: return 2600
    case .yawn: return 2100
    case .sigh: return 1700
    case .glow: return 600
    case .swirl: return 900
    }
}

/// What the renderer reads after `update`.
struct HerState {
    var head = 0.0, gx = 0.0, gy = 0.0, yaw = 0.0, t = 0.0, env = 0.0
    var sx = 1.0, sy = 1.0, yOff = 0.0, jx = 0.0, dx = 0.0, lx = 0.0, ly = 0.0
    var spin = 0.0, bright = 1.0, blush = 0.0, orbitK = 0.0, voice = 0.0
    var ripple = -1.0, flash = -1.0
}

// MARK: core

final class HerCore {
    static let you = HerV2(0, -0.03)
    private static let listen: Set<String> = ["35"]

    var reduced = false

    // springs
    private var eyeSp: [HerEyeSprings]
    private var head = HerSpring(0), gxS = HerSpring(0), gyS = HerSpring(0), stretch = HerSpring(1), lift = HerSpring(0)
    private var spinV = HerSpring(0.22), ex = HerSpring(0), ey = HerSpring(0)
    private var lagX = HerSpring(0), lagY = HerSpring(0), lagB = HerSpring(0), pet = HerSpring(0)
    private var energy = HerSpring(0.6), joy = HerSpring(0.5)

    // expression
    private(set) var exprId: String? = nil
    private var lastReq = ""
    private var x = HerExpr.table["02"]!
    private var fListen = false, fIdle = false, f30 = false, fAsk = false
    private var t0 = 0.0
    private var fired = Set<Int>()

    // state
    private var from: HerShape? = nil
    private var swapAt = 0.0, swapUntil = 0.0, drowse = 0.0
    private var twice = false
    private var perkAt = -1.0
    private var micro = HerV2.zero, microAt = 0.0
    private var attnOn = false, attnLast = HerV2.zero, attnCheckAt = 0.0, attnBored = 0.0, attnBack = 0.0, attnSince = 0.0
    private(set) var light = HerLights.base
    private var bright = 1.0, blush = 0.0, orbitK = 0.0, voiceK = 0.0
    private let seed = Double.random(in: 0..<10)
    private var spin = Double.random(in: 0..<herTAU), spinStart = -1.0, spinDur = 1100.0
    private var blinkAt = 0.0, blinkStart = -1.0, sacAt = 0.0, sac = HerV2.zero
    private var hopStart = -1.0, hopH = 0.0, hopDur = 380.0, anticAt = 0.0
    private var shakeAt = -1.0, rippleAt = -1.0, flashAt = -1.0
    private(set) var fx: [HerParticle] = []
    private var zAt = 0.0, sparkAt = 0.0
    private let temper = 0.5
    private var acts: [HerActRun] = []
    private var focus: (g: HerV2, until: Double)? = nil
    private var idleAt = 0.0, checkAt = 0.0, sighAt = 0.0, hearK = 0.0, sayK = 0.0
    private var talkFrom = -1.0, hushAt = -1.0, nodAt = 0.0, sayFrom = -1.0, sayHush = -1.0
    private var tiltSide = 1.0, tiltAt = 0.0, poiAt = -1.0, heardAt = -1e9, yawnAt = 0.0, tired = false

    private(set) var st = HerState()
    private(set) var eyes = [HerEyeGeom](repeating: HerEyeGeom(), count: 2)
    /// True while something moves fast enough that the owner should run at full frame rate.
    private(set) var busy = false

    private var lastGlass: HerUniforms? = nil
    private var tEpoch: Double? = nil

    init() {
        let t = herEyeTargets(HerShape.base)
        eyeSp = [HerEyeSprings(t.0), HerEyeSprings(t.1)]
        fx.reserveCapacity(32)
        acts.reserveCapacity(8)
    }

    // MARK: antics and effects

    private func hop(_ now: Double, _ h: Double, _ dur: Double = 380) {
        if reduced { return }
        stretch.velocity -= 1.2 + 5 * h; hopStart = now + 90
        hopH = h; hopDur = dur
    }

    private func perk(_ now: Double) { perkAt = now; if !reduced { lift.velocity -= 1.6 } }

    private func act(_ kind: HerAct, _ now: Double, _ amp: Double = 1, dir: Double = Bool.random() ? -1 : 1) {
        if reduced { return }
        if kind == .swirl { spinV.velocity += 5 * amp }
        acts.append(HerActRun(kind: kind, at: now, dur: actMs(kind), amp: amp, dir: dir))
    }

    func lookAt(_ g: HerV2, _ now: Double, _ ms: Double) {
        if blinkStart < 0 && hypot(g.x - ex.value, g.y - ey.value) > 0.5 && Double.random(in: 0..<1) < 0.5 { blinkStart = now }
        focus = (g, now + ms)
    }

    private func idle(_ now: Double, _ e: Double, _ j: Double) {
        let lively = temper
        var opts: [(() -> Void, Double)] = []
        opts.append(({ self.lookAt(HerV2(herRnd(-0.65, 0.65), herRnd(-0.4, 0.25)), now, herRnd(700, 1500)) }, 26 - 10 * lively))
        opts.append(({ self.lookAt(HerCore.you, now, herRnd(1000, 1700)) }, 14))
        opts.append(({ self.act(.bulge, now, 0.6 + 0.6 * lively) }, 6 + 14 * lively))
        opts.append(({ self.act(.wiggle, now, 0.6 + 0.6 * lively) }, 4 + 12 * lively))
        opts.append(({ self.act(.drift, now, 0.7 + 0.5 * lively) }, 10))
        opts.append(({ self.act(.swirl, now, 0.6 + 0.8 * lively) }, 6 + 8 * lively))
        if e < 0.4 { opts.append(({ self.act(.yawn, now, 1) }, 60 * (0.4 - e) / 0.4 + 10)) }
        if j < 0.42 { opts.append(({ self.act(.sigh, now, 1) }, 16)) }
        var r = Double.random(in: 0..<1) * opts.reduce(0) { $0 + $1.1 }
        for (run, w) in opts { r -= w; if r <= 0 { run(); return } }
    }

    private func life(_ now: Double, _ e: Double, _ j: Double, _ poi: HerPoi?) {
        if fListen {
            if hearK > 0.15 { heardAt = now }
            if hearK > 0.2 {
                if talkFrom < 0 { talkFrom = now }
                hushAt = -1
                if tiltAt == 0 { tiltAt = now + herRnd(3200, 4800) }
                else if now > tiltAt { tiltSide *= -1; tiltAt = now + herRnd(3200, 5200) }
            } else if hearK < 0.08 && talkFrom >= 0 {
                if hushAt < 0 { hushAt = now }
                else if now - hushAt > 200 {
                    if now - talkFrom > 600 && now - nodAt > 1100 && Double.random(in: 0..<1) < 0.8 { nodAt = now; act(.nod, now, 0.8 + 0.4 * temper) }
                    talkFrom = -1; hushAt = -1
                }
            }
        }
        if voiceK > 0.5 {
            if sayK > 0.15 { if sayFrom < 0 { sayFrom = now }; sayHush = -1 }
            else if sayK < 0.05 && sayFrom >= 0 {
                if sayHush < 0 { sayHush = now }
                else if now - sayHush > 240 {
                    if now - sayFrom > 600 {
                        if blinkStart < 0 { blinkStart = now }
                        if Double.random(in: 0..<1) < 0.7 { lookAt(HerCore.you, now, 560) }
                        if Double.random(in: 0..<1) < 0.35 { act(.nod, now, 0.5) }
                    }
                    sayFrom = -1; sayHush = -1
                }
            }
        }
        if checkAt != 0 && now > checkAt && (f30 || fAsk) {
            lookAt(HerCore.you, now, f30 ? 650 : 900)
            checkAt = now + (f30 ? herRnd(4200, 6000) : herRnd(3000, 5000))
        }
        if let poi = poi, poi.at != poiAt { poiAt = poi.at; act(.flinch, now, 0.45); lookAt(poi.g, now, 1400) }
        if poi == nil { poiAt = -1 }
        if (e < 0.3) != tired { tired = e < 0.3; if tired { yawnAt = now + herRnd(1500, 3000) } }
        if yawnAt != 0 && now > yawnAt && (fIdle || fAsk) { yawnAt = 0; act(.yawn, now, 1) }
        if fAsk && now > sighAt { act(.sigh, now, 1); sighAt = now + herRnd(8000, 12000) }
        if (fIdle || fAsk) && now >= idleAt {
            if idleAt != 0 { idle(now, e, j) }
            idleAt = now + herRnd(5000, 11000) * herLerp(1.6, 0.4, temper) * (1 + 1.2 * max(0, 0.6 - e)) * (fAsk ? 1.6 : 1)
        }
    }

    func effect(_ name: String, _ now: Double) {
        switch name {
        case "hop": hop(now, 0.2)
        case "jolt": stretch.velocity += 2.4; lift.velocity -= 1.4
        case "squash": stretch.velocity -= 2.2
        case "shake": shakeAt = now
        case "ripple": rippleAt = now
        case "flash": flashAt = now
        case "spin": if !reduced { spinStart = now; spinDur = 1100 }
        case "burst":
            if reduced { return }
            for _ in 0..<18 {
                let a = -herPI / 2 + herRnd(-1.35, 1.35), sp = herRnd(1.2, 2.6)
                fx.append(HerParticle(kind: .star, x: herRnd(-0.25, 0.25), y: herRnd(-0.35, 0.1), vx: cos(a) * sp, vy: sin(a) * sp - 0.3,
                                      life: herRnd(1, 1.7), s: herRnd(0.05, 0.1), r: herRnd(0, herPI),
                                      c: Bool.random() ? HerV3(1, 0.88, 0.55) : HerV3(1, 1, 1)))
            }
        default: break
        }
    }

    private func enter(_ idIn: String, _ now: Double) {
        let id = HerExpr.table[idIn] != nil ? idIn : "02"
        let wasId = exprId
        let wasShape = x.baseShape
        exprId = id; t0 = now; fired.removeAll(keepingCapacity: true)
        x = HerExpr.table[id]!
        fListen = HerCore.listen.contains(id); fIdle = HerExpr.idle.contains(id); f30 = id == "30"; fAsk = id == "ask"
        for f in x.enter { effect(f, now) }
        anticAt = now + herRnd(6000, 14000) * herLerp(1.8, 0.6, temper)
        if x.blink != nil { blinkAt = now + herRnd(600, 2400) }
        tiltAt = 0; talkFrom = -1; hushAt = -1; sayFrom = -1; sayHush = -1
        checkAt = now + (f30 ? 3200 : fAsk ? 2800 : 0); sighAt = now + 11000
        guard let was = wasId else { return }
        if HerExpr.sleepy.contains(id) { drowse = now + 1600; return }
        drowse = 0
        let first = x.seq?.frames.first?.eyes
        let lidded = first.map { $0.lid != nil || $0.lidR != nil } ?? false
        let waking = HerExpr.sleepy.contains(was)
        if lidded || !(waking || wasShape.gap(to: x.baseShape) >= 1) { return }
        from = wasShape; blinkStart = now; swapAt = now + 70; swapUntil = now + 300; twice = waking
    }

    // MARK: gaze

    private func gaze(_ x: HerExpr, _ gyOv: Double?, _ now: Double, _ t: Double, _ look: HerV2?, _ poi: HerPoi?) -> (Double, Double, Bool) {
        let gx0 = x.gx ?? 0, gy0 = gyOv ?? x.gy ?? 0
        if now >= microAt { micro = HerV2(herRnd(-0.035, 0.035), herRnd(-0.025, 0.025)); microAt = now + herRnd(350, 900) }
        if let f = focus, now < f.until { return (f.g.x + micro.x, f.g.y + micro.y, false) }
        focus = nil
        if fListen && now - heardAt < 1600 { return (HerCore.you.x + 0.06 * tiltSide + micro.x, HerCore.you.y + micro.y, false) }
        if exprId == "ask", let p = poi { return (p.g.x + micro.x, p.g.y + micro.y, false) }
        switch x.gaze {
        case .free:
            if let look = look {
                if now >= attnCheckAt {
                    let jump = hypot(look.x - attnLast.x, look.y - attnLast.y), moved = jump > 0.02
                    attnLast = look; attnCheckAt = now + 60
                    if moved && jump > 0.5 && blinkStart < 0 && Double.random(in: 0..<1) < 0.6 { blinkStart = now }
                    if moved {
                        if !attnOn { attnOn = true; attnSince = now; perk(now) }
                        attnBored = now + herRnd(1800, 3400)
                        if now - attnSince > herRnd(6000, 9000) { attnBored = now }
                    } else if attnOn && now > attnBored {
                        attnOn = false; attnBack = now + herRnd(2600, 5600); sacAt = now + herRnd(120, 360)
                    } else if !attnOn && now > attnBack { attnOn = true; attnBored = now + herRnd(700, 1200) }
                }
                if attnOn { return (look.x + micro.x, look.y + micro.y, false) }
            } else { attnOn = false }
            if now >= sacAt {
                let far = Double.random(in: 0..<1) < 0.4
                let next = HerV2(herRnd(-1, 1) * (far ? 0.55 : 0.18), herRnd(-1, 1) * (far ? 0.3 : 0.1))
                if blinkStart < 0 && hypot(next.x - sac.x, next.y - sac.y) > 0.5 && Double.random(in: 0..<1) < 0.6 { blinkStart = now }
                sac = next; sacAt = now + herRnd(700, 3300)
            }
            return (sac.x + micro.x, sac.y + micro.y, false)
        case .away: return (gx0 + 0.05 * sin(t * 0.7), gy0 + 0.04 * sin(t * 0.9), false)
        case .up: return (0.45 + 0.15 * sin(t * 0.8), -0.5 + 0.05 * sin(t * 1.3), false)
        case .loop: return (0.32 * cos(t * 4.4), 0.1 + 0.2 * sin(t * 4.4), true)
        case .scan:
            let ph = (t * 1.7).truncatingRemainder(dividingBy: 2), tri = ph < 1 ? ph : 2 - ph
            return (-0.65 + 1.3 * herSmooth(0, 1, tri), 0.12, true)
        case .recall: return (-0.35 + 0.12 * sin(t * 0.6), -0.5 + 0.06 * sin(t * 0.9), false)
        case .read:
            let ph = (t * 0.55).truncatingRemainder(dividingBy: 1), line = Double(Int((t * 0.55).truncatingRemainder(dividingBy: 3)))
            return (ph < 0.85 ? -0.35 + 0.7 * ph / 0.85 : 0.35 - 0.7 * (ph - 0.85) / 0.15, 0.08 + 0.07 * line, ph >= 0.85)
        case .still: return (gx0, gy0, false)
        }
    }

    // MARK: update

    func update(now: Double, dt dtRaw: Double, input: HerCoreInput) {
        let dt = min(max(dtRaw, 0), 0.25)
        if input.expr != lastReq {
            lastReq = input.expr
            let id = HerExpr.table[lastReq] != nil ? lastReq : "02"
            if id != exprId { enter(id, now) }
        }
        let x = self.x
        let t = now / 1000, el = now - t0
        let seqOn = x.seq != nil && el < x.seq!.end
        step2(&energy, input.energy, dt); step2(&joy, input.joy, dt)
        let e = herClamp(energy.value, 0, 1), j = herClamp(joy.value, 0, 1), low = max(0, 0.6 - e), high = max(0, e - 0.6)
        hearK += (input.hear - hearK) * (1 - exp(-(input.hear > hearK ? 25 : 7) * dt))
        let sayOn = input.say != nil, say = input.say ?? 0
        sayK += (say - sayK) * (1 - exp(-(say > sayK ? 30 : 10) * dt))
        let lean = fListen ? hearK : 0
        let effort = f30 ? herSmooth(0, 7000, el) : 0
        var ovIdx = -1
        if seqOn, let frames = x.seq?.frames {
            for i in frames.indices where el >= frames[i].at {
                ovIdx = i
                if let fxn = frames[i].fx, !fired.contains(i) { fired.insert(i); effect(fxn, now) }
            }
        }
        let ov: HerFrame? = ovIdx >= 0 ? x.seq!.frames[ovIdx] : nil
        let calm = reduced || input.still ? 0.0 : 1.0
        if calm != 0 { life(now, e, j, input.poi) }
        var moving = false
        var shape = now < swapAt && from != nil ? from! : (ovIdx >= 0 ? x.frameShapes[ovIdx] : x.baseShape)
        let near = x.gaze == .free && input.look != nil && attnOn && hypot(input.look!.x, input.look!.y) < 0.3
        if pet.step(near || input.pet ? 1 : 0, 4, 1, dt) { moving = true }
        if pet.value > 0.005 { shape.cutB = max(shape.cutB, 0.36 * pet.value); shape.y += 0.03 * pet.value }
        shape = herMoodShape(shape, j)
        if effort != 0 { shape.cut = max(shape.cut, 0.16 * effort); shape.cutT = 6 }
        let tg = herEyeTargets(shape)
        var (eh, ed) = x.spring ?? (ov != nil ? (9, 0.8) : (5.2, 0.62))
        if now >= swapAt && now < swapUntil { (eh, ed) = (18, 1) }
        else if now < drowse { (eh, ed) = (1.3, 1) }
        if eyeSp[0].step(tg.0, hz: eh, damping: ed, dt: dt) { moving = true }
        if eyeSp[1].step(tg.1, hz: eh, damping: ed, dt: dt) { moving = true }
        var (gx, gy, fast) = gaze(x, ov?.gy, now, t, input.look, input.poi)
        gy += 0.22 * low
        if shakeAt >= 0 {
            let k = (now - shakeAt) / 800
            if k < 1 { gx = 0.55 * sin(k * 3 * herTAU) * (1 - k); fast = true } else { shakeAt = -1 }
        }
        if calm == 0 && x.gaze != .still { gx = 0; gy = 0 }
        if !fast {
            // gaze springs are left out of `moving`: every micro saccade would keep her "busy"; their velocity is checked below
            _ = ex.step(gx, 12, 0.9, dt); _ = ey.step(gy, 12, 0.9, dt)
            _ = gxS.step(gx, 2.1, 0.95, dt); _ = gyS.step(gy, 2.1, 0.95, dt)
        } else {
            _ = gxS.step(gx, 9, 0.9, dt); _ = gyS.step(gy, 9, 0.9, dt)
            ex = gxS; ey = gyS
        }
        let sway = x.sway.map { $0.0 * sin(t * herTAU / $0.1) } ?? 0
        let tilt = lean != 0 ? 5 * tiltSide * min(1, hearK * 3) : 0
        _ = head.step(shape.head + calm * (sway * (1 - 0.7 * lean) + tilt + 2.2 * sin(t * 0.37 + seed)), 3, 0.8, dt)
        if x.blink != nil && calm != 0 && blinkStart < 0 && now >= blinkAt && !input.pressed { blinkStart = now }
        var bl = 1.0, br = 1.0
        if blinkStart >= 0 {
            let k = (now - blinkStart) / 1000 * (x.blinkSlow ? 0.45 : 1) * (1 - 0.9 * low)
            bl = blinkCurve(k); br = blinkCurve(k - 0.022)
            if k > 0.3 {
                blinkStart = -1
                let range = x.blink ?? (3000, 6000)
                blinkAt = now + (Double.random(in: 0..<1) < 0.18 ? 150 : herRnd(range.0, range.1) * (1 + 1.5 * low - 0.4 * high))
                if twice { twice = false; blinkStart = now + 110 }
            }
        }
        if let w = x.lidWave {
            let s = sin(t * herTAU / w)
            bl *= 1 - 0.92 * herSmooth(0.1, 0.9, s); br *= 1 - 0.92 * herSmooth(0.1, 0.9, -s)
        }
        if !x.antics.isEmpty && calm != 0 && now >= anticAt {
            if x.antics[Int.random(in: 0..<x.antics.count)] == .spin { effect("spin", now) } else { hop(now, 0.14) }
            anticAt = now + herRnd(9000, 18000) * herLerp(1.8, 0.6, temper)
        }
        var yaw = 0.0
        if spinStart >= 0 {
            let k = (now - spinStart) / spinDur
            if k < 1 { yaw = herTAU * herSmooth(0, 1, k) } else { spinStart = -1 }
        }
        if stretch.step(input.pressed ? 0.86 : 1, input.pressed ? 10 : 4.2, input.pressed ? 0.9 : 0.45, dt) { moving = true }
        if lift.step(0, 3, 0.8, dt) { moving = true }
        voiceK += ((x.voice ? 1 : 0) - voiceK) * (1 - exp(-3 * dt))
        let phrase = pow(0.5 + 0.5 * sin(t * 1.7 - 0.8), 2), syl = pow(0.5 + 0.5 * sin(t * 7.3 + 1.3 * sin(t * 2.1)), 2)
        let env = sayOn ? sayK * voiceK : calm * voiceK * phrase * (0.35 + 0.65 * syl)
        var hopY = 0.0, rise = 0.0
        if hopStart >= 0 && now >= hopStart {
            let k = (now - hopStart) / hopDur
            if k >= 1 { hopStart = -1; stretch.velocity -= hopH > 0.3 ? 2.4 : 1.4 }
            else { hopY = -hopH * 4 * k * (1 - k); rise = 0.1 * max(0, 1 - 2.2 * k) }
        }
        let pk = perkAt >= 0 ? (now - perkAt) / 360 : -1
        if pk >= 1 { perkAt = -1 }
        let bump = pk >= 0 && pk < 1 ? sin(pk * herPI) : 0
        var br0 = x.breathe ?? (0.007, 4.2)
        br0 = (br0.0 * (1 + 1.4 * low), br0.1 * (1 + 0.9 * low - 0.3 * high))
        var aGy = 0.0, aY = 0.0, aSt = 1.0, aLid = 1.0, aLen = 1.0, aHead = 0.0, aX = 0.0, aBr = 1.0, aCheek = 0.0, aJx = 0.0
        var slowOnly = true
        for a in acts {
            let k = (now - a.at) / a.dur
            if k >= 1 || k < 0 { continue }
            let env1 = sin(herPI * k) * a.amp
            switch a.kind {
            case .nod: aGy += 0.22 * env1; aY += 0.035 * env1; aLid *= 1 - 0.22 * env1; slowOnly = false
            case .flinch:
                let shr = k < 0.35 ? sin(herPI * k / 0.35) * a.amp : 0, wide = k > 0.3 ? sin(herPI * (k - 0.3) / 0.7) * a.amp : 0
                aSt *= 1 - 0.13 * shr; aLid *= 1 - 0.75 * shr; aLen *= 1 + 0.14 * wide; aY -= 0.03 * wide; aBr *= 1 + 0.2 * wide
                slowOnly = false
            case .wince:
                aLid *= 1 - 0.45 * env1; aCheek = max(aCheek, 0.3 * env1); aJx += 0.012 * env1 * sin(now * 0.06); aSt *= 1 - 0.05 * env1
                slowOnly = false
            case .bulge: aSt *= 1 + 0.075 * sin(herTAU * k) * (1 - k) * a.amp
            case .wiggle: aHead += 8 * sin(3 * herTAU * k) * (1 - k) * a.amp; slowOnly = false
            case .drift:
                aX += 0.16 * a.dir * herSmooth(0, 0.35, k) * (1 - herSmooth(0.6, 1, k)) * a.amp; aHead += 4 * a.dir * env1
            case .yawn:
                let c = herSmooth(0, 0.3, k) * (1 - herSmooth(0.7, 1, k)) * a.amp
                aLid *= 1 - 0.8 * c; aCheek = max(aCheek, 0.25 * c); aSt *= 1 + 0.08 * c; aGy -= 0.28 * c; aBr *= 1 - 0.18 * c
            case .sigh:
                let inh = k < 0.45 ? sin(herPI * k / 0.45) : 0, exh = k > 0.4 ? sin(herPI * (k - 0.4) / 0.6) : 0
                aSt *= 1 + (0.04 * inh - 0.03 * exh) * a.amp; aBr *= 1 - 0.15 * exh * a.amp; aGy += 0.1 * exh * a.amp
                aLid *= 1 - 0.2 * exh * a.amp
            case .glow: aBr *= 1 + 0.35 * env1; aLen *= 1 + 0.06 * env1; slowOnly = false
            case .swirl: break
            }
        }
        acts.removeAll { now - $0.at >= $0.dur }
        let stretchV = stretch.value * (1 + calm * br0.0 * sin(t * herTAU / br0.1) + 0.028 * env) * (1 + rise) * aSt * (1 + 0.035 * lean)
        let bounce = x.bounce != nil && calm != 0 ? -x.bounce!.0 * abs(sin(herPI * now / x.bounce!.1)) : 0
        let bob = calm * (x.bob ?? 0.03) * (0.55 + 0.75 * e) * sin(t * herTAU / 3.3 + seed)
        let trem = (ov?.tremble ?? x.tremble ?? 0) * (reduced ? 0 : 1)
        let jx = trem * (sin(t * 57.1) + sin(t * 83.7 + 1.3)) * 0.5 + aJx
        _ = spinV.step(calm * (ov?.spin ?? x.spin ?? 0.22) * (0.45 + 0.9 * e) * (1 + 0.9 * effort), 1.2, 1, dt)
        spin += spinV.value * dt
        var L = input.light ?? ov?.light ?? x.light ?? HerLights.base
        if j > 0.55 { L = L.blended(with: HerLights.warm, (j - 0.55) * 0.8) }
        L = L.blended(with: HerLights.dim, min(0.6, max(0, 0.45 - j) * 0.8 + max(0, 0.4 - e) * 0.9))
        if input.dim != 0 { L = L.blended(with: HerLights.dim, input.dim) }
        let lightGap = light.mix(to: L, 1 - exp(-(ov?.lightK ?? x.lightK ?? 5) * dt))
        var bt = (ov?.bright ?? 1) * L.b * (1 + 0.25 * env) * (1 + 0.25 * bump) * (0.82 + 0.3 * e) * (1 + 0.2 * lean)
        if let fl = x.flicker, calm != 0 { bt *= 1 - fl * (0.5 + 0.5 * sin(t * 37) * sin(t * 23.3)) }
        bright += (bt - bright) * (1 - exp(-(ov != nil ? 14 : 6) * dt))
        blush += ((x.blush ?? 0) - blush) * (1 - exp(-2 * dt))
        orbitK += ((x.orbit ? 1 : 0) - orbitK) * (1 - exp(-3 * dt))
        if x.zzz && calm != 0 && now >= zAt {
            fx.append(HerParticle(kind: .z, x: 0.5, y: -0.72, vx: 0.18, vy: -0.4, life: 2.8, s: 0.14, r: 0)); zAt = now + 1300
        }
        if x.sparkle && calm != 0 && now >= sparkAt {
            for _ in 0..<2 {
                let a = herRnd(0, herTAU), rr = herRnd(1.08, 1.28)
                fx.append(HerParticle(kind: .spark, x: cos(a) * rr, y: sin(a) * rr - 0.1, vx: 0, vy: -0.05, life: 0.8, s: herRnd(0.07, 0.11), r: 0))
            }
            sparkAt = now + herRnd(1300, 2600)
        }
        var starsLive = false
        for i in fx.indices {
            fx[i].age += dt; fx[i].x += fx[i].vx * dt; fx[i].y += fx[i].vy * dt
            if fx[i].kind == .star { fx[i].vy += 3.2 * dt; fx[i].r += dt * 4; starsLive = true }
        }
        fx.removeAll { $0.age >= $0.life }
        let lidMul = 1 - 1.1 * pow(low, 1.5), lenMul = (1 + 0.12 * high) * aLen * (1 + 0.1 * lean)
        func shaped(_ v: HerEyeTarget, _ blink: Double) -> HerEyeTarget {
            var o = v
            o.lid = max(0.04, o.lid * blink * (input.pressed ? 0.35 : 1) * lidMul * aLid)
            o.len = max(0, o.len * (1 + 0.08 * env + 0.08 * bump) * lenMul); o.w = max(0.02, o.w * (1 + 0.16 * bump))
            o.a = herClamp(o.a, 0, 1)
            o.cutB = max(o.cutB, aCheek)
            return o
        }
        let eL = shaped(eyeSp[0].values, bl), eR = shaped(eyeSp[1].values, br)
        if rippleAt >= 0 && (now - rippleAt) / 900 >= 1 { rippleAt = -1 }
        if flashAt >= 0 && (now - flashAt) / 420 >= 1 { flashAt = -1 }
        let body = hopY + bounce
        _ = lagX.step(herClamp(-input.vel.x * 0.012, -0.1, 0.1), 7, 0.5, dt)
        _ = lagY.step(herClamp(-input.vel.y * 0.012, -0.1, 0.1), 7, 0.5, dt)
        _ = lagB.step(body, 9, 0.5, dt)
        let ox = lagX.value, oy = lagY.value + lagB.value - body
        herEyeSet(&eyes, eL, eR, ex.value, ey.value + aGy, yaw, head.value + aHead)
        if ox != 0 || oy != 0 {
            for i in 0..<2 {
                eyes[i].c.x += ox; eyes[i].c.y += oy
                for k in 0..<eyes[i].n { eyes[i].pts[k].x += ox; eyes[i].pts[k].y += oy }
            }
        }
        st = HerState(
            head: head.value + aHead, gx: gxS.value, gy: gyS.value + 0.5 * aGy, yaw: yaw, t: t, env: env,
            sx: (1 + 0.012 * env) / stretchV.squareRoot(), sy: stretchV,
            yOff: hopY + bob + bounce + lift.value + aY + 0.03 * lean, jx: jx, dx: aX,
            lx: 0.04 * gxS.value, ly: 0.025 * gyS.value,
            spin: spin, bright: bright * aBr, blush: blush, orbitK: orbitK, voice: voiceK,
            ripple: rippleAt >= 0 ? (now - rippleAt) / 900 : -1, flash: flashAt >= 0 ? (now - flashAt) / 420 : -1)

        // Busy = something moves too fast for a ~24 fps redraw. Idle sinusoids (breath, bob, sway, micro saccades,
        // star spin, z and sparkle drift) stay below the line on purpose.
        busy = moving || input.pressed || input.hear > 0.03 || (sayOn && sayK > 0.02) || lightGap > 0.01
            || abs(bt - bright) > 0.02 || blinkStart >= 0 || spinStart >= 0 || hopStart >= 0 || perkAt >= 0 || shakeAt >= 0
            || rippleAt >= 0 || flashAt >= 0 || seqOn || starsLive || !slowOnly || orbitK > 0.01 || voiceK > 0.3
            || fast || x.tremble != nil || x.flicker != nil || x.lidWave != nil || x.bounce != nil
            || abs(ex.velocity) > 1.5 || abs(ey.velocity) > 1.5 || abs(head.velocity) > 25 || abs(spinV.value) > 0.7
            || lagX.value != 0 || lagY.value != 0
    }

    @inline(__always) private func step2(_ s: inout HerSpring, _ goal: Double, _ dt: Double) { _ = s.step(goal, 0.5, 1, dt) }

    // MARK: render inputs

    var rgb: (Int, Int, Int) {
        (Int((light.glow.x * 255).rounded()), Int((light.glow.y * 255).rounded()), Int((light.glow.z * 255).rounded()))
    }

    /// [jx, jy, bx, by] of the prototype's `pose(1)`: translate (in R) and scale of the glass.
    var pose: (jx: Double, jy: Double, bx: Double, by: Double) {
        (st.jx + st.lx + st.dx, st.yOff + st.ly, st.sx, st.sy)
    }

    /// Inputs of the glass shader for this frame, or nil when nothing the eye can see changed since the last submitted frame.
    /// Call `didRender` after the frame was actually submitted.
    func glassFrame(S: Int, q: Double) -> HerUniforms? {
        if tEpoch == nil { tEpoch = st.t }
        var u = HerUniforms()
        let cy = Float(cos(-(st.spin + st.gx * 0.5 + st.yaw))), sy = Float(sin(-(st.spin + st.gx * 0.5 + st.yaw)))
        let pitch = 0.3 + st.gy * 0.4
        let cp = Float(cos(pitch)), sp = Float(sin(pitch))
        u.rot = simd_float3x3(columns: (SIMD3<Float>(cy, 0, -sy), SIMD3<Float>(sy * sp, cp, cy * sp), SIMD3<Float>(sy * cp, -sp, cy * cp)))
        func f3(_ v: HerV3) -> SIMD3<Float> { SIMD3<Float>(Float(v.x), Float(v.y), Float(v.z)) }
        u.n1 = f3(light.n1); u.n2 = f3(light.n2); u.n3 = f3(light.n3); u.rim = f3(light.rim); u.eyeC = f3(light.glow)
        func spill(_ e: HerEyeGeom) -> SIMD3<Float> {
            SIMD3<Float>(Float(e.c.x), Float(e.c.y),
                         Float(e.a * herClamp(e.lid, 0, 1) * (0.75 + 0.6 * st.env) * min(1.3, st.bright + 0.2)))
        }
        u.eyeL = spill(eyes[0]); u.eyeR = spill(eyes[1])
        u.px = Float(2 * herB / Double(S)); u.t = Float(st.t - tEpoch!); u.q = Float(q); u.bright = Float(st.bright)
        // glass skin
        u.neb = 0.3; u.stars = 1; u.soft = 0; u.gal = 0; u.aur = 0; u.refr = 1; u.b = Float(herB)
        if let l = lastGlass, !u.differs(from: l, tStep: busy ? 0.015 : 0.04) { return nil }
        return u
    }

    func didRender(_ u: HerUniforms) { lastGlass = u }
}

// MARK: shader uniforms (layout mirrors `HerU` in Her.metal)

private func maxAbs(_ v: SIMD3<Float>) -> Float { max(abs(v.x), abs(v.y), abs(v.z)) }

struct HerUniforms {
    var rot = simd_float3x3(1)
    var n1 = SIMD3<Float>(), n2 = SIMD3<Float>(), n3 = SIMD3<Float>(), rim = SIMD3<Float>()
    var eyeC = SIMD3<Float>(), eyeL = SIMD3<Float>(), eyeR = SIMD3<Float>()
    var px: Float = 0, t: Float = 0, q: Float = 3, neb: Float = 0.3, stars: Float = 1, soft: Float = 0
    var gal: Float = 0, aur: Float = 0, refr: Float = 1, bright: Float = 1, b: Float = 1.3, pad: Float = 0

    /// Whether the picture would visibly differ: sub-pixel rotation, 1-LSB colour and tiny light changes do not count.
    func differs(from o: HerUniforms, tStep: Float) -> Bool {
        if px != o.px || q != o.q { return true }
        if abs(t - o.t) >= tStep { return true }
        let rotTol = 0.35 * px
        for c in 0..<3 where maxAbs(rot[c] - o.rot[c]) > rotTol { return true }
        let colTol: Float = 0.004
        for (a, b) in [(n1, o.n1), (n2, o.n2), (n3, o.n3), (rim, o.rim), (eyeC, o.eyeC)] where maxAbs(a - b) > colTol { return true }
        for (a, b) in [(eyeL, o.eyeL), (eyeR, o.eyeR)] {
            if abs(a.x - b.x) > 0.004 || abs(a.y - b.y) > 0.004 || abs(a.z - b.z) > 0.004 { return true }
        }
        return abs(bright - o.bright) > 0.003
    }
}
