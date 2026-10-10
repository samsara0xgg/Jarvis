import Foundation

// Her (星核): light palettes, eye shapes and the expression table.
// Ported from the HTML prototype's `LIGHT`, `BASE`..`LINES` and `EXPRESSIONS` (glass skin only).

typealias HerV2 = SIMD2<Double>
typealias HerV3 = SIMD3<Double>

// MARK: light

struct HerLight {
    var eye: HerV3, glow: HerV3, n1: HerV3, n2: HerV3, n3: HerV3, rim: HerV3
    var b: Double
}

private func hex(_ s: String) -> HerV3 {
    let v = UInt32(s.dropFirst(), radix: 16) ?? 0
    return HerV3(Double((v >> 16) & 255), Double((v >> 8) & 255), Double(v & 255)) / 255
}

private func lt(_ eye: String, _ glow: String, _ n: [String], _ rim: String, _ b: Double) -> HerLight {
    HerLight(eye: hex(eye), glow: hex(glow), n1: hex(n[0]), n2: hex(n[1]), n3: hex(n[2]), rim: hex(rim), b: b)
}

enum HerLights {
    static let base = lt("#f5f7ff", "#9db4ff", ["#2b2a78", "#5b3aa8", "#1f5a8a"], "#8fa4ff", 1)
    static let dim = lt("#aab2c8", "#5b6690", ["#161a38", "#24204a", "#132a44"], "#46507a", 0.5)
    static let cool = lt("#f7fbff", "#a8d4ff", ["#1e3a86", "#3a5ac0", "#1a6a9a"], "#9cc8ff", 1.15)
    static let warm = lt("#fff8ef", "#ffc98f", ["#6a3450", "#a8563a", "#3f2a78"], "#ffb98a", 1.1)
    static let gold = lt("#fffaf0", "#ffd77a", ["#7a4a1a", "#c08a2a", "#5a2e6e"], "#ffd27a", 1.3)
    static let blush = lt("#fff5f9", "#ff9ecb", ["#7a2a5e", "#c0487e", "#4a2a80"], "#ff9ec8", 1.05)
    static let angry = lt("#fff1ee", "#ff6a55", ["#6a0f10", "#b42a18", "#3a0a1c"], "#ff5a48", 1.15)
    static let alert = lt("#ffeceb", "#ff4d4d", ["#7a0c14", "#c21e22", "#40081a"], "#ff3b3b", 1.2)
    static let think = lt("#f8f4ff", "#bb94ff", ["#3a1f86", "#7a3ac0", "#23307a"], "#b98cff", 1.1)
    static let listen = lt("#f2fbff", "#8fe3ff", ["#0f4a6e", "#1f7aa0", "#2a3a8a"], "#8fe3ff", 1.05)
    static let speak = lt("#ffffff", "#e8eeff", ["#46527e", "#8090c4", "#56689e"], "#e2e9ff", 1.2)
    static let done = lt("#f2fff9", "#6fe0b4", ["#0e5044", "#1f8a70", "#1f3a78"], "#6fe0b4", 1.1)
    static let work = lt("#f2f6ff", "#6c9cff", ["#12307e", "#2a5ad0", "#1a2a6a"], "#6c9cff", 1.15)
    static let memory = lt("#fbf6ff", "#e0b4ff", ["#4a2a6a", "#8a4a8a", "#2a3a7a"], "#e6b8ff", 0.95)
    static let deny = lt("#e8dcdc", "#c86a6a", ["#3a1418", "#5a1e24", "#221a3a"], "#b85a5a", 0.7)
    static let off = lt("#6b7080", "#2a2f40", ["#0a0c16", "#0e0f1c", "#0a1220"], "#2a3048", 0.08)
    static let deep = lt("#f1edff", "#9a86ff", ["#170d4a", "#351c7a", "#10184a"], "#9a86ff", 0.92)

    static let byName: [String: HerLight] = [
        "base": base, "dim": dim, "cool": cool, "warm": warm, "gold": gold, "blush": blush, "angry": angry,
        "alert": alert, "think": think, "listen": listen, "speak": speak, "done": done, "work": work,
        "memory": memory, "deny": deny, "off": off, "deep": deep,
    ]
}

private func amax(_ v: HerV3) -> Double { max(abs(v.x), abs(v.y), abs(v.z)) }

extension HerLight {
    /// Eases toward `t` by `a`; returns how far apart they were (the largest channel gap).
    mutating func mix(to t: HerLight, _ a: Double) -> Double {
        var gap = 0.0
        func m(_ c: inout HerV3, _ t: HerV3) { let d = t - c; gap = max(gap, amax(d)); c += d * a }
        m(&eye, t.eye); m(&glow, t.glow); m(&rim, t.rim)
        m(&n1, t.n1); m(&n2, t.n2); m(&n3, t.n3)
        return gap
    }

    func blended(with o: HerLight, _ k: Double) -> HerLight {
        if k <= 0 { return self }
        func l(_ a: HerV3, _ b: HerV3) -> HerV3 { a + (b - a) * k }
        return HerLight(eye: l(eye, o.eye), glow: l(glow, o.glow), n1: l(n1, o.n1), n2: l(n2, o.n2), n3: l(n3, o.n3),
                        rim: l(rim, o.rim), b: b + (o.b - b) * k)
    }
}

// MARK: eye shapes

/// A partial eye shape: nil means "inherit BASE".
struct HerEyeSpec {
    var sep: Double? = nil, y: Double? = nil, len: Double? = nil, w: Double? = nil, tilt: Double? = nil
    var lean: Double? = nil, bend: Double? = nil, lid: Double? = nil, cut: Double? = nil, cutT: Double? = nil
    var cutB: Double? = nil, head: Double? = nil
    var lenR: Double? = nil, wR: Double? = nil, tiltR: Double? = nil, yR: Double? = nil, bendR: Double? = nil
    var lidR: Double? = nil, cutR: Double? = nil, cutTR: Double? = nil, cutBR: Double? = nil
    var aL: Double? = nil, aR: Double? = nil

    func with(_ f: (inout HerEyeSpec) -> Void) -> HerEyeSpec { var s = self; f(&s); return s }
}

struct HerShape {
    var sep = 0.3, y = 0.08, len = 0.52, w = 0.21, tilt = 14.0, lean = 0.0, bend = 0.0, lid = 1.0
    var cut = 0.0, cutT = 0.0, cutB = 0.0, head = 0.0
    var lenR: Double? = nil, wR: Double? = nil, tiltR: Double? = nil, yR: Double? = nil, bendR: Double? = nil
    var lidR: Double? = nil, cutR: Double? = nil, cutTR: Double? = nil, cutBR: Double? = nil
    var aL: Double? = nil, aR: Double? = nil

    static let base = HerShape()

    func applying(_ s: HerEyeSpec) -> HerShape {
        var o = self
        if let v = s.sep { o.sep = v }; if let v = s.y { o.y = v }; if let v = s.len { o.len = v }
        if let v = s.w { o.w = v }; if let v = s.tilt { o.tilt = v }; if let v = s.lean { o.lean = v }
        if let v = s.bend { o.bend = v }; if let v = s.lid { o.lid = v }; if let v = s.cut { o.cut = v }
        if let v = s.cutT { o.cutT = v }; if let v = s.cutB { o.cutB = v }; if let v = s.head { o.head = v }
        if let v = s.lenR { o.lenR = v }; if let v = s.wR { o.wR = v }; if let v = s.tiltR { o.tiltR = v }
        if let v = s.yR { o.yR = v }; if let v = s.bendR { o.bendR = v }; if let v = s.lidR { o.lidR = v }
        if let v = s.cutR { o.cutR = v }; if let v = s.cutTR { o.cutTR = v }; if let v = s.cutBR { o.cutBR = v }
        if let v = s.aL { o.aL = v }; if let v = s.aR { o.aR = v }
        return o
    }

    /// `gapOf` in the prototype: how different two shapes read (>= 1 means "swap through a blink").
    func gap(to b: HerShape) -> Double {
        max(abs(tilt + lean - b.tilt - b.lean) / 50, abs(len - b.len) / 0.24,
            abs((lenR ?? len) - (b.lenR ?? b.len)) / 0.24, abs(w - b.w) / 0.14, abs((wR ?? w) - (b.wR ?? b.w)) / 0.14,
            abs(head - b.head) / 18, abs(cut - b.cut) / 0.3, abs(sep - b.sep) / 0.1)
    }
}

// MARK: expressions

struct HerFrame {
    var at: Double
    var eyes = HerEyeSpec()
    var light: HerLight? = nil
    var bright: Double? = nil
    var spin: Double? = nil
    var lightK: Double? = nil
    var tremble: Double? = nil
    var gy: Double? = nil
    var fx: String? = nil
}

struct HerSeq {
    var frames: [HerFrame]
    var end: Double
}

enum HerGaze { case free, still, up, away, loop, read, scan, recall }
enum HerAntic { case spin, hop }

struct HerExpr {
    var eyes: HerEyeSpec
    var light: HerLight? = nil
    var gaze: HerGaze = .free
    var blink: (Double, Double)? = nil
    var gx: Double? = nil, gy: Double? = nil
    var spin: Double? = nil
    var breathe: (Double, Double)? = nil
    var bob: Double? = nil
    var zzz = false, sparkle = false, orbit = false
    var bounce: (Double, Double)? = nil
    var enter: [String] = []
    var antics: [HerAntic] = []
    var spring: (Double, Double)? = nil
    var seq: HerSeq? = nil
    var lightK: Double? = nil
    var blush: Double? = nil
    var sway: (Double, Double)? = nil
    var tremble: Double? = nil
    var flicker: Double? = nil
    var lidWave: Double? = nil
    var voice = false
    var blinkSlow = false
    // filled by `prepared()`: BASE + eyes, and the same with each sequence frame's eyes on top
    var baseShape = HerShape.base
    var frameShapes: [HerShape] = []

    // every argument defaulted, in a readable order
    init(eyes: HerEyeSpec, light: HerLight? = nil, gaze: HerGaze = .free, gx: Double? = nil, gy: Double? = nil,
         blink: (Double, Double)? = nil, spin: Double? = nil, breathe: (Double, Double)? = nil, bob: Double? = nil,
         zzz: Bool = false, sparkle: Bool = false, orbit: Bool = false, bounce: (Double, Double)? = nil,
         enter: [String] = [], antics: [HerAntic] = [], spring: (Double, Double)? = nil,
         lightK: Double? = nil, blush: Double? = nil, sway: (Double, Double)? = nil, tremble: Double? = nil,
         flicker: Double? = nil, lidWave: Double? = nil, voice: Bool = false, blinkSlow: Bool = false, seq: HerSeq? = nil) {
        self.eyes = eyes; self.light = light; self.gaze = gaze; self.blink = blink; self.gx = gx; self.gy = gy
        self.spin = spin; self.breathe = breathe; self.bob = bob; self.zzz = zzz; self.sparkle = sparkle
        self.orbit = orbit; self.bounce = bounce; self.enter = enter; self.antics = antics; self.spring = spring
        self.seq = seq; self.lightK = lightK; self.blush = blush; self.sway = sway; self.tremble = tremble
        self.flicker = flicker; self.lidWave = lidWave; self.voice = voice; self.blinkSlow = blinkSlow
    }

    func prepared() -> HerExpr {
        var o = self
        o.baseShape = HerShape.base.applying(eyes)
        o.frameShapes = (seq?.frames ?? []).map { o.baseShape.applying($0.eyes) }
        return o
    }

    static let sleepy: Set<String> = ["home", "doze", "00"]
    static let idle: Set<String> = ["rest", "02", "peek", "home", "glance"]

    private static let SLEEP = HerEyeSpec(sep: 0.3, y: 0.18, len: 0.3, w: 0.075, tilt: 90, bend: -0.05)
    private static let SMILE = HerEyeSpec(sep: 0.31, y: 0.1, len: 0.46, w: 0.15, tilt: 90, bend: 0.17)
    private static let TILT = HerEyeSpec(sep: 0.28, y: 0.06, len: 0.42, w: 0.22, tilt: 0, head: 12, lenR: 0.3)
    private static let LINES = HerEyeSpec(sep: 0.42, y: 0.04, len: 0.42, w: 0.19, tilt: 90)

    static let table: [String: HerExpr] = {
        let L = HerLights.self
        var t: [String: HerExpr] = [:]
        t["home"] = HerExpr(eyes: LINES, gaze: .still)
        t["rest"] = HerExpr(eyes: HerEyeSpec(sep: 0.3, y: 0.08, len: 0.36, w: 0.245, tilt: 9), gaze: .free, blink: (2600, 6000))
        t["doze"] = HerExpr(eyes: LINES, light: L.dim, gaze: .still, breathe: (0.02, 5), bob: 0.01, zzz: true)
        t["glance"] = HerExpr(eyes: HerEyeSpec(sep: 0.36, y: 0.02, len: 0.18, w: 0.18, tilt: 0), gaze: .free)
        t["peek"] = HerExpr(eyes: HerEyeSpec(sep: 0.27, y: 0.32, len: 0.36, w: 0.245, tilt: 9), gaze: .free, blink: (2400, 6600))
        t["00"] = HerExpr(eyes: SLEEP.with { $0.head = -4 }, light: L.dim, gaze: .still, gy: 0.2, spin: 0.05,
                          breathe: (0.022, 5.5), bob: 0.012, zzz: true)
        t["02"] = HerExpr(eyes: HerEyeSpec(y: 0.1, len: 0.36, w: 0.245, tilt: 9), gaze: .free, blink: (2400, 6600), antics: [.spin, .hop])
        t["10"] = HerExpr(eyes: SMILE, light: L.warm, spin: 0.6, sparkle: true, bounce: (0.035, 900), enter: ["hop"], antics: [.spin])
        t["13"] = HerExpr(eyes: HerEyeSpec(sep: 0.3, y: 0.03, len: 0.02, w: 0.37, tilt: 0), light: L.cool, gaze: .still, gy: -0.05,
                          blink: (5000, 9000), enter: ["jolt"], spring: (8, 0.38),
                          seq: HerSeq(frames: [HerFrame(at: 0, eyes: HerEyeSpec(sep: 0.31, w: 0.42), bright: 1.8),
                                               HerFrame(at: 260, bright: 1.1)], end: 500))
        t["14"] = HerExpr(eyes: HerEyeSpec(sep: 0.27, y: 0.13, len: 0.28, w: 0.17, tilt: 8, head: -9), light: L.blush, gaze: .away,
                          gx: -0.6, gy: 0.35, blink: (1200, 2800), spin: 0.3, lightK: 1.6, blush: 1, sway: (2, 3))
        t["30"] = HerExpr(eyes: HerEyeSpec(sep: 0.27, y: -0.02, len: 0.36, w: 0.19, tilt: 0, lean: 16), light: L.think, gaze: .up,
                          blink: (3000, 6000), spin: 1.1, orbit: true)
        t["31"] = HerExpr(eyes: TILT, light: L.listen, gaze: .still, blink: (2400, 6000),
                          seq: HerSeq(frames: [HerFrame(at: 0, eyes: HerEyeSpec(lid: 0.08)),
                                               HerFrame(at: 120, eyes: HerEyeSpec(head: -8), bright: 1.4, gy: 0.25, fx: "ripple"),
                                               HerFrame(at: 460)], end: 800))
        t["32"] = HerExpr(eyes: HerEyeSpec(sep: 0.27, y: 0.1, len: 0.3, w: 0.2, tilt: 0, cut: 0.15, cutT: 8), light: L.work, gaze: .loop,
                          blink: (3000, 6000), spin: 1.7)
        t["33"] = HerExpr(eyes: SMILE, light: L.warm, spin: 0.5, enter: ["hop", "burst"],
                          seq: HerSeq(frames: [HerFrame(at: 0, light: L.gold, bright: 1.4, spin: 3),
                                               HerFrame(at: 1400, light: L.warm, bright: 1, spin: 0.5)], end: 1500))
        t["34"] = HerExpr(eyes: HerEyeSpec(sep: 0.3, y: 0.05, len: 0.04, w: 0.3, tilt: 0, cut: 0.12, cutT: 18), light: L.alert, gaze: .still,
                          blink: (3000, 6000), spin: 0.3, tremble: 0.004, flicker: 0.15,
                          seq: HerSeq(frames: [HerFrame(at: 0, light: L.alert, lightK: 40, tremble: 0.03),
                                               HerFrame(at: 140, light: L.base, lightK: 40, tremble: 0.03),
                                               HerFrame(at: 280, light: L.alert, lightK: 40, tremble: 0.02),
                                               HerFrame(at: 420, light: L.base, lightK: 40),
                                               HerFrame(at: 560, light: L.alert, lightK: 14)], end: 800))
        t["35"] = HerExpr(eyes: TILT, light: L.listen, gaze: .still, gx: 0.15, gy: -0.05, blink: (2400, 6000), spin: 0.35,
                          breathe: (0.012, 2.8), sway: (3, 3.2))
        t["36"] = HerExpr(eyes: HerEyeSpec(sep: 0.29, y: 0.07, len: 0.42, w: 0.21, tilt: 0), light: L.work, gaze: .still, spin: 0.7, lidWave: 0.9)
        t["37"] = HerExpr(eyes: HerEyeSpec(sep: 0.28, y: 0.05, len: 0.56, w: 0.22, tilt: 0), light: L.memory, gaze: .recall,
                          blink: (3000, 6000), spin: -0.5)
        t["38"] = HerExpr(eyes: HerEyeSpec(sep: 0.3, y: 0.12, len: 0.32, w: 0.25, tilt: 0, cut: 0.38, cutT: -4), light: L.deny, gaze: .still,
                          gx: -0.4, gy: 0.3, blink: (2500, 5000), spin: 0.15, enter: ["shake"])
        t["39"] = HerExpr(eyes: TILT, light: L.speak, gaze: .read, blink: (2400, 5000), spin: 0.8, voice: true)
        t["39b"] = HerExpr(eyes: TILT, light: L.speak, gaze: .still, gy: 0.05, blink: (2400, 5000), spin: 0.8, sway: (7, 1.5), voice: true)
        t["40"] = HerExpr(eyes: HerEyeSpec(sep: 0.28, y: 0.07, len: 0.34, w: 0.2, tilt: 0), light: L.work, gaze: .scan,
                          blink: (4000, 8000), spin: 2.4)
        t["ask"] = HerExpr(eyes: HerEyeSpec(len: 0.52, w: 0.21, tilt: 14), light: L.warm, gaze: .still, gx: 0, gy: 0.32,
                           blink: (2400, 6000), spin: 0.35, breathe: (0.012, 2.8), enter: ["hop"], sway: (3, 3.2))
        t["deep"] = HerExpr(eyes: HerEyeSpec(sep: 0.27, y: -0.02, len: 0.3, w: 0.17, tilt: 0, cut: 0.16), light: L.deep, gaze: .still,
                            gx: 0.38, gy: -0.52, blink: (4200, 8000), spin: 0.35, breathe: (0.014, 5), blinkSlow: true)
        t["fin"] = HerExpr(eyes: SMILE, light: L.done, spin: 0.5, enter: ["hop", "burst"],
                           seq: HerSeq(frames: [HerFrame(at: 0, light: L.gold, bright: 1.4, spin: 3),
                                                HerFrame(at: 1400, light: L.done, bright: 1, spin: 0.5)], end: 1500))
        return t.mapValues { $0.prepared() }
    }()
}
