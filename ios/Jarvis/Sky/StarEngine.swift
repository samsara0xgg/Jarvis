import UIKit
import CoreGraphics

// 星盘 — your time as a planisphere turning around her, the pole star. One turn of the sky is a day.
// This file is the geometry and the state of the dial; StarDraw.swift paints it; StarFlow.swift is what the touches and her talking do to it.
// Names follow the HTML prototype so the two can be read side by side.

let W = 440.0, H = 956.0, CX = 220.0, CY = 292.0
let R_PS = 34.0, R_HER = 96.0, R_YOU = 142.0, R_SC = 178.0, R_BAND = 182.5, R_NUM = 194.0, R_BEZ = 206.0
let SKY_H = 640.0
let MAP_Y = 74.0, MAP_H = 452.0
let POLE_MY = 118.0                      // where she rests above the month
let GX0 = 22.0, GW = (W - 44) / 7, MR = 20.0
// five rows fit above the month's title; a month that spans six (Nov 2026) packs them a little closer and starts a little higher
var GY0: Double { Cal.cur.rows > 5 ? 182.0 : 206.0 }
var GH: Double { Cal.cur.rows > 5 ? 56.0 : 64.0 }

struct Cam { var ox, oy, oh, fx, fy, fh, rx, ry, ux, uy, uh, foc, X0, Y0, D: Double }
struct NearParams: Codable, Equatable {
    var gr = 142.0, ga = 1.37, gh = 46.0, tr = 142.0, ta = 2.35, foc = 240.0, X = 265.0, Y = 262.0

    // the debug panel (Sky/StarTune.swift) shows ga and ta as minutes from the focus: before it, and after it
    private static let rad = 2 * Double.pi / 1440
    var gaMin: Double { get { (Double.pi / 2 - ga) / Self.rad } set { ga = Double.pi / 2 - newValue * Self.rad } }
    var taMin: Double { get { (ta - Double.pi / 2) / Self.rad } set { ta = Double.pi / 2 + newValue * Self.rad } }

    /// his tuned values survive a relaunch; the defaults are the absence of the key
    static let key = "nearParamsOverride"
    static func load(_ d: UserDefaults = .standard) -> NearParams {
        d.data(forKey: key).flatMap { try? JSONDecoder().decode(NearParams.self, from: $0) } ?? NearParams()
    }
    func save(_ d: UserDefaults = .standard) {
        if self == NearParams() { d.removeObject(forKey: Self.key) } else { d.set(try? JSONEncoder().encode(self), forKey: Self.key) }
    }
}

enum Mode { case seek, idle, drag, script }

final class StarEngine {
    // ---------- data ----------
    var EV: [Ev] = Cal.cur.real ? [] : makeEvents()
    let talks = makeTalks()
    func byId(_ id: String) -> Ev? { EV.first { $0.id == id } }
    let sprites = SpriteCache()

    // ---------- clock ----------
    var t0 = Date()
    func nowT() -> Double { Cal.cur.real ? Cal.cur.nowT() : NOW0 + Date().timeIntervalSince(t0) / 60 }

    // ---------- state ----------
    var fs = Spring(NOW1 - 330)       // the focus time, in minutes; the sky turns in from earlier tonight
    var qs = Spring(0)                // zoom: -1 close, 0 a day, 1 a week, 2 the month
    var tms = Spring(HomeModule.shared.isTimeline ? 1 : 0)      // the middle of the screen: 0 the dial, 1 the timeline (Sky/StarModule.swift)
    var moduleSeen = HomeModule.shared.kind
    var mode: Mode = .seek
    var goal = NOW1, stiff = 2.6, qGoal = 0.0, qHeld = false
    var F = NOW1 - 330, z = 1.0, amp = 8.5, k = TAU / DAY, q = 0.0, mon = 0.0, fl = 0.0
    var sky = 0.0, prevF = NOW1 - 330, omega = 0.0, omegaS = 0.0, Trev = NOW1, prevRev = NOW1
    var herP: (Double, Double, Double) = (CX, CY, 1)
    var herRGB: (Int, Int, Int) = (157, 180, 255)
    var px = 3.0
    var baseOff = false
    var openId: String?
    var replay = false
    var script: Double?
    var pressed = false
    var hear = 0.0
    var sonar: [Double] = []
    var flyer: (e: Ev, at: Double)?
    var lastTick = 0.0, lastTock = 0.0
    let born = nowMs()
    // the brain's days (Brain/EngineBrain.swift): what has been adopted, and what has been asked for
    var epoch = 0, seenVersion = -1, seenWaiting = -1, seenTopics = -1, seenOpen = 0, monthAsked = false, lastAdopt = -1e9

    // the lens: hold a finger on the ring and the time under it opens up
    var ls = Spring(0)
    var lensGoal = 0.0, lensTh = Double.pi / 2, lensM = 1.0
    var lensT: Double?, prevLensT: Double?

    // ---------- the camera ----------
    var cam: Cam?
    var PX = 0.0, PY = 0.0, PS = 1.0, PZ = 0.0, camE = 0.0, cenX = CX, cenY = CY, cenS = 1.0
    var names: [(Double, Double, Double, Double)] = []
    var labelHits: [(id: String, box: (Double, Double, Double, Double))] = []      // the names placed this frame, for a tap to find
    var numeralBoxes: [(Double, Double, Double, Double)] = []      // where the near view's hour numerals stand this frame, for the names to keep out of
    var NEARP = NearParams.load()       // read every frame by setCam, so a change shows on the next one

    // ---------- flows, touches, panel ----------
    let ui = StarUI()
    var gen = 0
    var ptr: [Int: CGPoint] = [:]
    var g: Gest?
    var talk: String?
    var expr: String?
    var look: (Double, Double)?
    var speaking: Say?
    var release: Gate?
    var ti = 0
    var rowKey = "", viewKey = "", panelKey = ""
    // her phone voice (Sky/StarVoice.swift): the session state last seen and when it changed, the last trouble and when it appeared
    var voiceState = VoiceSession.State.idle, voiceAt = 0.0, voiceErr: String?, voiceErrAt = -1e9
    #if DEBUG
    var voiceFake: ((Double) -> VoiceShot)?
    #endif
    // what he typed and attached on its way to the brain (Sky/StarBrain): the line over the composer, and whether a send is in flight
    var brainNote: String?, brainNoteUntil = 0.0, sendingToBrain = false
    var sentTurn: SentTurn?         // the typed turn last taken by the brain, for a failed one to go again
    var unsent: [Int: SentTurn] = [:]       // typed messages whose send did not finish, by their row in the talk: the talk stays while one is there, and 点重试 sends it again with its own id
    var deliver: @MainActor (_ utterance: String, _ text: String, _ ids: [String], _ about: About?) async -> Delivery = StarEngine.deliverToBrain         // the words out; a test puts its own under it
    // the talk under the dial (Sky/StarTalk.swift): the row ids being written, the talk's start in the conversation, and when the talk goes if nobody touches it
    var turnSeq = 0, talkId = 0, talkBase = 0, speakId: Int?, vHis: Int?, vHer: Int?, vHerKey: [String] = [], vHerSaid = 0, vShown = 0.0, vShownAt = 0.0, lingerUntil = 0.0, lingerHold = false
    var statusTurn: Int?            // the live row standing in for her until her first words (Sky/StarStatus.swift)
    var lightRef: (id: String, until: Double)?     // a star her answer named that the days have not brought yet
    var aboutDropped: String?       // the item whose chip he took off: it does not come back while that star is the open one
    // the panel's second height: when it opened, whether this talk has used up its one self-opening, what the stream was last built from, when the pinned part was
    var panelSince = 0.0, autoOpenSpent = false, streamSeen = (-1, false, -1, -1, false), panelAt = 0.0
    /// the first conversation row the stream lists (nil: the newest page, which slides as rows come). Held while the panel is open, so a row coming in at the
    /// bottom takes nothing away above what he reads; a page earlier each time he scrolls to the top; the newest page again a moment after the panel shuts.
    var streamFrom: Int?
    var dateD: Int?, dateEpoch = -1
    weak var herDrive: HerDrive?
    var endEditing: (() -> Void)?
    #if DEBUG
    var beginEditing: (() -> Void)?
    #endif
    /// asks the stage to go to 60 fps right now, so a touch or a button does not wait for the next 20 fps tick
    var onWake: () -> Void = {}
    func wake() { wakeUntil = nowMs() + 450; onWake() }
    // the sky is turning because a finger turned it (a drag, or the glide after a fling), not because a button sent it somewhere
    var byHand = false, hand = 0.0
    // render on demand: the dial is drawn again only when something on it moved, and at rest only for its slow pulses
    var mapAt = -1e9, wakeUntil = 0.0, pulsing = true
    var herDt = 0.0, herN = 0, herFresh = true, detFast = false
    /// a sheet is over the dial and her: they nearly stop
    var covered: Bool { (ui.panelOpen && nowMs() - panelSince > 450) || ui.settingsOpen }
    /// turning fast, the dial is only its marks: names and the light on what the meridian points at wait until it slows enough to read
    var calmK: Double { 1 - smooth(cam != nil ? 1.5 : 0.6, cam != nil ? 3.5 : 1.8, abs(omegaS)) }

    /// true while something moves fast enough to want 60 fps
    var isBusy: Bool {
        mode != .idle || abs(omegaS) > 0.01 || qHeld || abs(qs.value - qGoal) > 0.002 || abs(qs.velocity) > 0.002 || abs(ls.value - lensGoal) > 0.002
            || g != nil || talk != nil || flyer != nil || !sonar.isEmpty || speaking != nil || (ui.panelOpen && nowMs() - panelSince < 450)
            || abs(tms.value - (HomeModule.shared.isTimeline ? 1 : 0)) > 0.002 || abs(tms.velocity) > 0.002
    }

    func sound(_ cue: SoundCue, _ gain: Float) { Sound.shared.play(cue, gain: gain) }

    // MARK: geometry

    func ang(_ t: Double) -> Double {
        let th = Double.pi / 2 + (t - F) * k
        if lensM < 1.002 { return th }
        return th + wrapAngle(lensTh + 2 * atan(lensM * tan(wrapAngle(th - lensTh) / 2)) - th)
    }
    func magAt(_ t: Double) -> Double {      // how much the lens stretches the dial at t
        if lensM < 1.002 { return 1 }
        let h = wrapAngle(Double.pi / 2 + (t - F) * k - lensTh) / 2, c = cos(h), s2 = sin(h)
        return lensM / (c * c + lensM * lensM * s2 * s2)
    }
    func visT(_ t: Double) -> Double { 1 - smooth(0.62, 0.97, abs(t - F) / (DAY * z / 2)) }
    func span() -> Double { DAY * z / 2 * 0.975 }
    func laneR(_ e: Ev) -> Double { e.lane == .her ? R_HER : R_YOU + (e.t <= Trev ? amp * actAt(e.t) : 0) }

    /// close up the camera comes down and floats just over the ring, looking ahead
    func setCam(_ f: Double) {
        if f <= 0 { cam = nil; camE = 0; cenX = CX; cenY = CY; cenS = 1; return }
        let N = NEARP, e = f * f * (3 - 2 * f); camE = e
        func L(_ a: Double, _ b: Double) -> Double { a + (b - a) * e }
        func G(_ a: Double, _ b: Double) -> Double { exp(L(log(a), log(b))) }
        let cx1 = N.gr * cos(N.ga), cy1 = N.gr * sin(N.ga), tx1 = N.tr * cos(N.ta), ty1 = N.tr * sin(N.ta), run = hypot(tx1 - cx1, ty1 - cy1)
        let ps1 = -Double.pi / 2 + wrapAngle(atan2(ty1 - cy1, tx1 - cx1) + Double.pi / 2)
        let th = L(Double.pi / 2, atan2(N.gh, run)), psi = L(-Double.pi / 2, ps1), D = G(1600, hypot(N.gh, run)), foc = G(1600, N.foc)
        let fx = cos(th) * cos(psi), fy = cos(th) * sin(psi), fh = -sin(th), rx = -sin(psi), ry = cos(psi)
        let tx = L(0, tx1), ty = L(0, ty1)
        cam = Cam(ox: tx - D * fx, oy: ty - D * fy, oh: D * sin(th), fx: fx, fy: fy, fh: fh, rx: rx, ry: ry, ux: -fh * ry, uy: fh * rx, uh: fx * ry - fy * rx,
                  foc: foc, X0: L(CX, N.X), Y0: L(CY, N.Y), D: D)
        _ = Pxy(0, 0); cenX = PX; cenY = PY; cenS = PS
    }
    /// A point of the dial's plane (x, y from its centre, h above it) is seen at PX, PY, PS pixels per unit, PZ deep; false behind the camera.
    @discardableResult func Pxy(_ x: Double, _ y: Double, _ h: Double = 0) -> Bool {
        guard let c = cam else { PX = CX + x; PY = CY + y; PS = 1; PZ = 0; return true }
        let dx = x - c.ox, dy = y - c.oy, dh = h - c.oh, zc = dx * c.fx + dy * c.fy + dh * c.fh
        if zc < 18 { return false }
        PS = c.foc / zc; PZ = zc
        PX = c.X0 + (dx * c.rx + dy * c.ry) * PS
        PY = c.Y0 - (dx * c.ux + dy * c.uy + dh * c.uh) * PS
        return true
    }
    @discardableResult func P(_ r: Double, _ a: Double) -> Bool { Pxy(r * cos(a), r * sin(a)) }
    func fog() -> Double { guard let c = cam else { return 1 }; return 1 - 0.72 * smooth(c.D * 1.15, c.D * 2.9, PZ) }   // far things dim
    /// A near-view name's look, by its rank (-1 open, 0 waiting, 1 in progress, 2 next, 3 done) and its star's perspective scale PS.
    /// On screen PS runs from about 1.1 at the horizon to 6 at the camera; the size follows √PS in half points, so a name grows the whole way in.
    /// The open and the waiting are the largest, boldest and brightest, and stay bright far out; the rest dim with distance, never gone.
    static func nameStyle(_ rank: Int, _ ps: Double) -> (size: Double, weight: Int, alpha: Double) {
        let t = min(3, max(0, rank)), k = [1.05, 0.95, 0.85, 0.8][t], lo = [11.0, 10, 9, 9][t], w = [600, 500, 500, 400][t]
        let aNear = [1, 0.95, 0.8, 0.72][t], aFar = [0.85, 0.7, 0.5, 0.42][t]
        let n = clamp((k * 8.2 * max(ps, 0).squareRoot() * 2).rounded() / 2, lo, 20)
        return (n, w, aFar + (aNear - aFar) * smooth(1.2, 3, ps))
    }
    /// the angle and radius on the dial under a point of the screen; nil above the horizon
    func unP(_ X: Double, _ Y: Double) -> (Double, Double)? {
        guard let c = cam else { return (atan2(Y - CY, X - CX), hypot(X - CX, Y - CY)) }
        let xn = (X - c.X0) / c.foc, yn = (c.Y0 - Y) / c.foc
        let dx = c.fx + xn * c.rx + yn * c.ux, dy = c.fy + xn * c.ry + yn * c.uy, dh = c.fh + yn * c.uh
        if dh > -1e-4 { return nil }
        let t = c.oh / -dh, x = c.ox + t * dx, y = c.oy + t * dy
        return (atan2(y, x), hypot(x, y))
    }

    // MARK: the pole (her) and the month's cells

    func poleXY() -> (Double, Double) { tm > 0.5 ? (TLLayout.her.x, TLLayout.her.y) : mon > 0.5 ? (CX, POLE_MY) : (herP.0, herP.1) }
    func cellOf(_ d: Int) -> (x: Double, y: Double, row: Int, col: Int) {
        let c = Cal.cur, i = d - c.mFirst + c.firstCol, row = i / 7, col = i % 7
        return (GX0 + GW * (Double(col) + 0.5), GY0 + GH * Double(row) + MR + 2, row, col)
    }
    func cellAt(_ x: Double, _ y: Double) -> Int? {
        for d in M_FIRST...M_LAST {
            let c = cellOf(d)
            if abs(x - c.x) < GW / 2 && y > c.y - MR - 8 && y < c.y + MR + 22 { return d }
        }
        return nil
    }

    // MARK: what the meridian points at

    func kdOf(_ e: Ev) -> Kd {
        let ahead = e.t > Trev && (replay || e.st != .wait)
        if ahead || e.st == .next { return .next }
        if e.st == .done { return e.lane == .you ? .lit : .done }
        return e.st == .wait ? .wait : .work
    }
    func focusSet(_ T0: Double? = nil, win win0: Double? = nil) -> [Ev] {
        let T = T0 ?? F, win = win0 ?? 26 * z
        let nT = nowT(), atNow = abs(T - nT) < win, now = nowMs()
        return EV.filter { e in
            e.born <= now && (atNow && e.st == .wait && e.t <= nT + win ? true : e.d != nil ? (T >= e.t - win && T <= e.t + e.d! + win) : abs(e.t - T) <= win)
        }.sorted { a, b in
            a.st.rawValue != b.st.rawValue ? a.st.rawValue < b.st.rawValue : abs(a.t - T) < abs(b.t - T)
        }.prefix(3).map { $0 }
    }
    func nearestEvent(_ t: Double, _ win: Double) -> Double? {
        var best: Double?, bd = win
        for e in EV {
            if e.born > nowMs() || visT(e.t) < 0.3 { continue }
            let dd = abs(e.t - t)
            if dd < bd { bd = dd; best = e.t }
        }
        let nT = nowT()
        if abs(nT - t) < min(bd, win) { best = nT }
        return best
    }
}
