import SwiftUI

// The day's bar (Sky/DayTimeline.swift puts it under her sentence): what he did, as pieces of one bar with a hair of ground between them, the night, the places in their colours, the
// trips hatched, a stretch nothing speaks to hatched faintly, and past now the part that has not happened with what is planned on it. Each piece wears the richest word that fits it; on
// the phone every stretch is given room for its icon (`TLAxis.fair`). Now is a warm line that breathes. A frame goes round the block of the row open where it stands, round a time for a
// moment (`TLBlink`), and round the part of the day the list shows while he scrolls it (`TLViewportFrame`). The pieces, the axis, the labels and the paint are plain values (below), so
// what is drawn can be tested without a screen.

// MARK: - what the bar is made of

/// a span of the bar and what fills it
struct TLPiece: Equatable, Identifiable {
    enum Fill: Equatable { case place(RGB, kept: Bool), trip, sleep, doing(RGB), blank, ahead }
    let id: String                  // the item it is (a stay, a trip, the night), or what it is not; a stay that the night cuts in two is two pieces of one id
    var t0: Double, t1: Double
    var fill: Fill
    var name: String?               // the word it may wear: the place, the route, the way he moved
    var icon: String?               // and its symbol
}

/// something planned, drawn on the part of the bar that has not happened: an event as a dashed box over its time, a reminder or a bus as a tick
struct TLAhead: Equatable, Identifiable {
    enum Shape: Equatable { case box, tick }
    let id: String
    var shape: Shape
    var t0: Double, t1: Double
    var name: String?, icon: String?
}

struct TLBarScene: Equatable {
    var span: ClosedRange<Double>
    var now: Double?
    var pieces: [TLPiece]           // in time order, one after the other over the whole span
    var ahead: [TLAhead]
    var ticks: [Int]                // the hours under the bar

    static let empty = TLBarScene(span: 360...DAY, now: nil, pieces: [], ahead: [], ticks: [])
}

/// A time framed on the bar for a moment, `length` seconds: the block of a row with nothing more to say (`row`), a line of the open row (`line`). The frame comes in over the first 12% of
/// it and goes over the last 30%; `n` tells one from the next, so the same time framed again frames it again.
struct TLBlink: Equatable {
    var spans: [ClosedRange<Double>]
    var length: Double
    var n: Int

    static let row = 1.3, line = 1.9
}

extension TLBarScene {
    /// where each stretch of the bar begins, in time order (what has not happened yet is not one): what VoiceOver's up and down on the bar steps through
    var stretchStarts: [Double] { pieces.filter { $0.fill != .ahead }.map(\.t0) }

    /// The start of the next stretch after minute `t`, or of the one before it going back; with no minute yet the first (the last, going back); at either end it stays.
    func step(from t: Double?, forward: Bool) -> Double? {
        let starts = stretchStarts
        guard let t else { return forward ? starts.first : starts.last }
        return forward ? starts.first { $0 > t + 0.5 } ?? starts.last : starts.last { $0 < t - 0.5 } ?? starts.first
    }
}

extension DayTimelineModel {
    /// The bar as pieces. The night is on top of a trip and a trip of a stay (`seg(at:)`); where nothing was recorded there is a blank piece, and on today everything after now
    /// is one piece that has not happened. A piece that goes on across a cut (the night under a stay, a long stay split by nothing) is one piece.
    var scene: TLBarScene {
        let lo = span.lowerBound, hi = span.upperBound
        let byId = Dictionary(entries.map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })
        var cuts: Set<Double> = [lo, hi]
        for s in segs { cuts.insert(clamp(s.t0, lo, hi)); cuts.insert(clamp(s.t1, lo, hi)) }
        if let now, now > lo, now < hi { cuts.insert(now) }
        let edges = cuts.sorted()
        var pieces: [TLPiece] = []
        for (a, b) in zip(edges, edges.dropFirst()) where b - a > 0.01 {
            let mid = (a + b) / 2
            var p: TLPiece
            if let now, mid > now {
                p = TLPiece(id: "ahead", t0: a, t1: b, fill: .ahead)
            } else if let s = seg(at: mid) {
                let fill: TLPiece.Fill
                switch s.kind {
                case .place(let c, let kept): fill = .place(c, kept: kept)
                case .move: fill = .trip
                case .sleep: fill = .sleep
                case .doing(let c): fill = c.map { .doing($0) } ?? .blank          // a stretch nobody could say anything of is a hole, as where nothing was recorded
                }
                p = TLPiece(id: s.id, t0: a, t1: b, fill: fill, name: byId[s.id]?.barName, icon: byId[s.id]?.barIcon)
            } else {
                p = TLPiece(id: "blank-\(Int(a))", t0: a, t1: b, fill: .blank)
            }
            if let last = pieces.last, last.id == p.id || (last.fill == .blank && p.fill == .blank) { pieces[pieces.count - 1].t1 = b } else { pieces.append(p) }
        }
        var ahead: [TLAhead] = []
        if let now {
            for e in entries where e.kind == .event || e.kind == .reminder || e.kind == .departure {
                let end = max(e.t1 ?? e.t0, e.t0)
                guard end > now, e.t0 < hi, end > lo else { continue }
                // a moment (a reminder, a bus) is a tick; an event with a length is a box over it
                let box = e.kind == .event && end - e.t0 >= 1
                ahead.append(TLAhead(id: e.id, shape: box ? .box : .tick, t0: e.t0, t1: box ? end : e.t0, name: e.barName, icon: e.barIcon))
            }
        }
        return TLBarScene(span: span, now: now, pieces: pieces, ahead: ahead, ticks: ticks)
    }
}

// MARK: - where a minute is drawn

/// Where each minute of the bar is drawn, and the minute at a point of it. `linear` shares the width by time. `fair` (the phone's timeline) gives every stretch room for its icon first: a
/// piece its time would draw narrower than `floor` (16 pt and the hair of ground) is drawn that wide, and the rest of the width is shared by the time of the others, again until no other
/// falls under it. What is still to come and a blank of under five minutes are not lifted, and where the floors would take nine tenths of the width or more the bar is linear. Inside a
/// piece time runs evenly.
struct TLAxis: Equatable {
    let span: ClosedRange<Double>
    let width: Double
    private var ts: [Double]            // the minutes where the pieces meet, from the bar's start to its end
    private var xs: [Double]            // and where they are drawn

    static let floor = 17.0
    static let shortBlank = 5.0

    static func linear(_ span: ClosedRange<Double>, width: Double) -> TLAxis {
        TLAxis(span: span, width: width, ts: [span.lowerBound, span.upperBound], xs: [0, width])
    }

    static func fair(_ scene: TLBarScene, width: Double) -> TLAxis {
        let lo = scene.span.lowerBound, hi = scene.span.upperBound
        let ps = scene.pieces.map { (t0: clamp($0.t0, lo, hi), t1: clamp($0.t1, lo, hi), lifts: Self.lifts($0)) }.filter { $0.t1 > $0.t0 }
        let d = ps.map { $0.t1 - $0.t0 }, floors = ps.map { $0.lifts ? Self.floor : 0 }
        guard let first = ps.first, d.reduce(0, +) > 0 else { return linear(scene.span, width: width) }
        var lifted = [Bool](repeating: false, count: ps.count)
        var k = width / d.reduce(0, +)
        for _ in 0..<30 {
            var fixed = 0.0, free = 0.0
            for i in ps.indices {
                if lifted[i] || d[i] * k < floors[i] { lifted[i] = true; fixed += floors[i] } else { free += d[i] }
            }
            guard fixed < 0.9 * width, free > 0 else { return linear(scene.span, width: width) }
            let next = (width - fixed) / free
            if abs(next - k) < 1e-9 { break }
            k = next
        }
        var ts = [first.t0], xs = [0.0]
        for i in ps.indices {
            ts.append(ps[i].t1); xs.append(xs[xs.count - 1] + (lifted[i] ? floors[i] : d[i] * k))
        }
        let end = xs[xs.count - 1]
        return TLAxis(span: scene.span, width: width, ts: ts, xs: xs.map { end > 0 ? $0 / end * width : $0 })
    }

    /// a piece that is given room for its icon: what has happened, but a blank of a few minutes
    static func lifts(_ p: TLPiece) -> Bool { p.fill != .ahead && !(p.fill == .blank && p.t1 - p.t0 < shortBlank) }

    /// where minute `t` is drawn, a minute off the bar at its start or end
    func x(_ t: Double) -> Double { Self.along(t, ts, xs) }
    /// the minute at `x` points from the bar's left edge, a point off the bar at its start or end
    func minute(_ x: Double) -> Double { Self.along(x, xs, ts) }

    /// `v` on the line through the points (`from`, `to`), both rising, held at their ends
    private static func along(_ v: Double, _ from: [Double], _ to: [Double]) -> Double {
        guard let a = from.first, let b = from.last else { return 0 }
        if v <= a { return to[0] }
        if v >= b { return to[to.count - 1] }
        var lo = 0, hi = from.count - 1
        while hi - lo > 1 { let m = (lo + hi) / 2; if from[m] <= v { lo = m } else { hi = m } }
        return to[lo] + (to[hi] - to[lo]) * (v - from[lo]) / max(1e-9, from[hi] - from[lo])
    }
}

// MARK: - paint

enum TLPaint {
    static let ink = RGB(238, 240, 251)
    static let darkInk = RGB(11, 13, 26)                // the text on a light fill
    static let warm = RGB(255, 240, 220)                // now, and the frames round what he opened or tapped
    static let trip = RGB(196, 208, 255)
    static let sleep = RGB(112, 120, 205)
    static let ground = RGB(9, 10, 20)                  // what the bar sits on: the sky under the scrim
    static let hole = RGB(196, 204, 238)                // the faint hatch of a stretch nothing speaks to
    static let icon = RGB(214, 220, 246)                // the glyph of a button on the glass (the gear, the footprint's map)

    /// how much a fill is drawn at, and its colour
    static func look(_ f: TLPiece.Fill) -> (rgb: RGB, alpha: Double)? {
        switch f {
        case .place(let c, let kept): return (c, kept ? 0.9 : 0.32)
        case .trip: return (trip, 0.28)
        case .sleep: return (sleep, 0.9)
        case .doing(let c): return (c, 0.85)
        case .blank, .ahead: return nil
        }
    }

    static let rem = RGB(150, 160, 235)                 // the lighter blue of REM, in the opened night's hypnogram

    /// WCAG relative luminance of a colour laid at `alpha` over the ground
    static func luminance(_ c: RGB, alpha: Double) -> Double {
        func lin(_ v: Double) -> Double { let s = v / 255; return s <= 0.03928 ? s / 12.92 : pow((s + 0.055) / 1.055, 2.4) }
        func mix(_ a: Double, _ g: Double) -> Double { lin(a * alpha + g * (1 - alpha)) }
        return 0.2126 * mix(c.r, ground.r) + 0.7152 * mix(c.g, ground.g) + 0.0722 * mix(c.b, ground.b)
    }
    /// Dark ink on a light fill and ink on a dark one: the line is where the two read alike (0.18), a little to the light side, so the night's indigo keeps its light text.
    static func textIsDark(_ f: TLPiece.Fill) -> Bool {
        guard let l = look(f) else { return false }
        return luminance(l.rgb, alpha: l.alpha) > 0.2
    }
}

extension RGB {
    var color: Color { Color(.sRGB, red: r / 255, green: g / 255, blue: b / 255, opacity: 1) }
    func color(_ a: Double) -> Color { Color(.sRGB, red: r / 255, green: g / 255, blue: b / 255, opacity: a) }
}

// MARK: - what a piece says

/// The richest of icon and name, name, icon, nothing that fits a piece with 6 pt of room each side.
enum TLBarLabel {
    enum Fit: Equatable { case iconName, name, icon, none }
    static let pad = 6.0, icon = 12.0, gap = 4.0

    static func fit(name: String?, icon hasIcon: Bool, width: Double, measure: (String) -> Double) -> Fit {
        let room = width - 2 * pad
        let nameW = name.map(measure)
        if hasIcon, let nameW, icon + gap + nameW <= room { return .iconName }
        if let nameW, nameW <= room { return .name }
        if hasIcon, icon + 4 <= width { return .icon }          // an icon alone needs only 2 pt each side, so a short stretch still says what it was
        return .none
    }

    /// how wide what it wears is
    static func labelWidth(_ f: Fit, name: String?, measure: (String) -> Double) -> Double {
        let nameW = name.map(measure) ?? 0
        switch f {
        case .iconName: return icon + gap + nameW
        case .name: return nameW
        case .icon: return icon
        case .none: return 0
        }
    }

    /// the width of a word set 11 pt semibold, the bar's type
    static func measure(_ s: String) -> Double {
        ceil((s as NSString).size(withAttributes: [.font: UIFont.systemFont(ofSize: 11, weight: .semibold)]).width)
    }

    struct Placed: Equatable, Identifiable {
        let item: String                // the piece or box it is on (a stay the night cuts in two has a piece on each side of it, and a word on each that has room)
        var x: Double                   // where its icon or word begins: centred on what it is on
        var fit: Fit
        var name: String?, icon: String?
        var dark: Bool                  // dark ink on a light fill
        var quiet = false               // a stretch nothing speaks to: its question mark at half ink
        var id: String { "\(item)@\(Int(x))" }       // one place on the bar: the same item may stand there twice, never at the same x
    }

    /// what each piece (and each box of an event) wears where the axis draws it
    static func placed(_ scene: TLBarScene, axis: TLAxis, measure: (String) -> Double = TLBarLabel.measure) -> [Placed] {
        var out: [Placed] = []
        for (p, r) in zip(scene.pieces, TLGeo.rects(scene.pieces, axis: axis, height: 1)) {
            let f = fit(name: p.name, icon: p.icon != nil, width: r.width, measure: measure)
            if f != .none {
                out.append(Placed(item: p.id, x: r.midX - labelWidth(f, name: p.name, measure: measure) / 2, fit: f, name: p.name, icon: p.icon, dark: TLPaint.textIsDark(p.fill),
                                  quiet: p.fill == .blank))
            }
        }
        for a in scene.ahead where a.shape == .box {
            let x0 = axis.x(a.t0), x1 = axis.x(a.t1)
            let f = fit(name: a.name, icon: a.icon != nil, width: x1 - x0, measure: measure)
            if f != .none { out.append(Placed(item: a.id, x: (x0 + x1) / 2 - labelWidth(f, name: a.name, measure: measure) / 2, fit: f, name: a.name, icon: a.icon, dark: false)) }
        }
        return out
    }
}

enum TLGeo {
    /// where each piece is drawn: a hair of ground (1 pt) between neighbours, and never narrower than 2 pt, so nothing recorded is lost to the gap
    static func rects(_ pieces: [TLPiece], axis: TLAxis, height: Double) -> [CGRect] {
        pieces.enumerated().map { i, p in
            let x0 = axis.x(p.t0), x1 = axis.x(p.t1)
            let l = i == 0 ? x0 : x0 + 0.5, r = i == pieces.count - 1 ? x1 : x1 - 0.5
            let mid = (l + r) / 2, half = max(r - l, 2) / 2
            return CGRect(x: mid - half, y: 0, width: half * 2, height: height)
        }
    }
}

/// How wide the day's bar is drawn: the phone's list width, or the iPad's whole top band (Pad/PadHome.swift sets it).
extension EnvironmentValues {
    @Entry var tlBarWidth: Double = TLLayout.barW
}

// MARK: - the track

/// The ground the bar's stretches lie on. While the day is being asked for it is all there is, and a soft band of light goes along it (it is still under Reduce Motion); nothing spins.
struct TLTrack: View {
    var loading = false
    var reduce = false
    @Environment(\.tlBarWidth) private var bw
    @State private var sweeping = false
    private let band = 96.0, period = 1.8, sweep = 1.4          // a pass takes 1.4 s, easing in and out, and the track rests for 0.4 s before the next: the band goes on past the end for the rest

    var body: some View {
        RoundedRectangle(cornerRadius: TLLayout.barRadius).fill(Color.white.opacity(0.035))
            .overlay(alignment: .leading) {
                if loading && !reduce {
                    LinearGradient(colors: [.clear, Color.white.opacity(0.07), .clear], startPoint: .leading, endPoint: .trailing)
                        .frame(width: band)
                        .offset(x: sweeping ? -band + (bw + band) * period / sweep : -band)
                        .onAppear { withAnimation(.easeInOut(duration: period).repeatForever(autoreverses: false)) { sweeping = true } }
                }
            }
            .clipShape(RoundedRectangle(cornerRadius: TLLayout.barRadius))
            .frame(width: bw, height: TLLayout.barH)
            .allowsHitTesting(false)
    }
}

// MARK: - the bar on screen

struct TLBarView: View {
    let scene: TLBarScene
    var bracket: ClosedRange<Double>?       // the block of the row he has open: framed
    var scrub: Double?                      // the minute under his finger
    var lens = false                        // a chip is pressed: the bar goes quiet and the things of that kind stand out in their colour
    var lensColor = RGB(196, 208, 255)
    var lensMarks: [TLMark] = []
    var reveal = true                       // false until the bar has grown in
    var reduce = false
    var breathe = true
    var sweeping = false                    // the brain is tidying the day: a band of light goes along the pieces (Sky/DayTimelineTidy.swift)
    var ghost: TLBarScene?                  // the drawing the bar had before a tidied day came in, over the new one until it has faded (`ghostGone`)
    var ghostGone = false
    var fair = false                        // every stretch is given room for its icon (`TLAxis.fair`): the phone's timeline
    var blink: TLBlink?                     // a time framed for a moment

    @Environment(\.tlBarWidth) private var bw
    private let bh = TLLayout.barH

    var body: some View {
        let axis = self.axis(for: scene)
        let rects = TLGeo.rects(scene.pieces, axis: axis, height: bh)
        ZStack(alignment: .topLeading) {
            ZStack(alignment: .topLeading) {
                TLTrack()
                BarCanvas(scene: scene, rects: rects).equatable()
                if sweeping { TLTidySweep(scene: scene, rects: rects).transition(.opacity) }
                planned(axis)
                TLBarLabels(scene: scene, axis: axis, reduce: reduce).equatable()
                if let g = ghost { ghostLayer(g) }
            }
            .animation(.easeOut(duration: 0.25), value: sweeping)
            .frame(width: bw, height: bh, alignment: .topLeading)       // its own size: the marker and the hours stand outside the bar and would stretch the track
            .mask(alignment: .leading) { GrowMask(shown: reduce || reveal) }          // grows in from the left, in the order of time, its leading edge soft
            .opacity(reduce && !reveal ? 0 : 1)
            .animation(TLMotion.barGrowth(reduce: reduce), value: reveal)
            .opacity(lens ? 0.25 : 1).animation(.easeOut(duration: 0.15), value: lens)
            lensLayer(axis)
            hours(axis).opacity(reveal ? 1 : 0).animation(TLMotion.arrival(after: 0.25, reveal: reveal, reduce: reduce), value: reveal)
            if let n = scene.now { nowMarker(axis.x(n)).opacity(reveal ? 1 : 0).animation(TLMotion.arrival(after: 0.7, reveal: reveal, reduce: reduce), value: reveal) }      // when the growth reaches it
            ZStack(alignment: .topLeading) {
                if let b = bracket { TLBarFrame(x0: axis.x(b.lowerBound), x1: axis.x(b.upperBound), color: TLPaint.warm.color(0.9)).transition(.opacity) }
            }
            .animation(.easeOut(duration: 0.2), value: bracket != nil)          // it comes and goes with a fade, and moves with the row that opens to the next block
            TLBlinkFrames(blink: blink, axis: axis)
            if let t = scrub { scrubMark(axis.x(t), t) }
        }
        .frame(width: bw, height: bh, alignment: .topLeading)
        .contentShape(Rectangle().inset(by: -10))
    }

    private func axis(for s: TLBarScene) -> TLAxis { fair ? .fair(s, width: bw) : .linear(s.span, width: bw) }

    /// the bar as it was, its pieces and its words, over the one that has come in: it fades out to show it (a cross-fade with no dip, which the two fading together would have)
    private func ghostLayer(_ g: TLBarScene) -> some View {
        let axis = self.axis(for: g)
        return ZStack(alignment: .topLeading) {
            BarCanvas(scene: g, rects: TLGeo.rects(g.pieces, axis: axis, height: bh)).equatable()
            TLBarLabels(scene: g, axis: axis, reduce: reduce).equatable()
        }
        .frame(width: bw, height: bh, alignment: .topLeading)
        .opacity(ghostGone ? 0 : 1)
        .allowsHitTesting(false)
    }

    // MARK: what is planned, the hours, a lens

    /// what is planned, on the part of the bar that has not happened: an event is a dashed box over its time, a reminder or a bus a tick. They are views and not paint, so that one he says
    /// is not his goes with a fade.
    private func planned(_ axis: TLAxis) -> some View {
        ZStack(alignment: .topLeading) {
            ForEach(scene.ahead) { a in
                TLAheadMark(a: a, x0: axis.x(a.t0), x1: axis.x(a.t1), height: bh).transition(.opacity)
            }
        }
        .frame(width: bw, height: bh, alignment: .topLeading)
        .clipShape(RoundedRectangle(cornerRadius: TLLayout.barRadius))
        .animation(TLMotion.fade(reduce: reduce), value: scene.ahead.map(\.id))
        .allowsHitTesting(false)
    }

    /// the hours under the bar: every three on the phone, every one on a bar wide enough to name them all (the iPad's); the last one ends where the bar ends. Where the axis gives a stretch
    /// of hours little room, an hour that would touch the one before it is left out.
    private var ticks: [Int] {
        guard bw >= 600 else { return scene.ticks }
        return Array(Int((scene.span.lowerBound / 60).rounded(.up))...24)
    }
    private func hours(_ axis: TLAxis) -> some View {
        var last = -Double.infinity
        let shown = ticks.compactMap { h -> (h: Int, x: Double)? in
            let x = axis.x(Double(h * 60)) - (h == 24 ? 12 : 6)
            guard x - last >= 16 else { return nil }
            last = x
            return (h, x)
        }
        return ForEach(shown, id: \.h) { t in
            Text(String(format: "%02d", t.h)).font(AT.mono(10)).foregroundStyle(RGB(196, 204, 238).color(0.38)).frame(width: 12)
                .offset(x: t.x, y: bh + 4)
        }
        .allowsHitTesting(false)
    }

    /// the things of the lens's kind in its colour over the quiet bar: a span as it is, a moment as a tick of 3 pt, the whole height
    private func lensLayer(_ axis: TLAxis) -> some View {
        ZStack(alignment: .topLeading) {
            ForEach(Array(lensMarks.enumerated()), id: \.offset) { _, m in
                let x0 = axis.x(m.t0), x1 = axis.x(m.t1), w = max(x1 - x0, 3)
                Rectangle().fill(lensColor.color(0.95)).frame(width: w, height: bh).offset(x: (x0 + x1) / 2 - w / 2)
            }
        }
        .frame(width: bw, height: bh, alignment: .topLeading)
        .clipShape(RoundedRectangle(cornerRadius: TLLayout.barRadius))
        .opacity(lens ? 1 : 0).animation(.easeOut(duration: 0.15), value: lens)
        .allowsHitTesting(false)
    }

    // MARK: now, and his finger

    /// 2 pt of warm white, 5 pt longer than the bar each way, with a glow that breathes over 2.4 s (`TLNowGlow`)
    private func nowMarker(_ x: Double) -> some View {
        TLNowGlow(height: bh + 10, breathe: breathe && !reduce)
            .frame(width: 2, height: bh + 10).offset(x: x - 1, y: -5)
            .allowsHitTesting(false)
    }

    /// a hairline over the minute and its time over the line, in a chip
    private func scrubMark(_ x: Double, _ t: Double) -> some View {
        ZStack(alignment: .topLeading) {
            Rectangle().fill(TLPaint.ink.color(0.7)).frame(width: 1, height: bh + 8).offset(x: x - 0.5, y: -4)
            Text(clock(t)).font(AT.mono(11)).foregroundStyle(TLPaint.ink.color).padding(.horizontal, 8).frame(height: 19)
                .background(Capsule().fill(Color(red: 28 / 255, green: 31 / 255, blue: 58 / 255)))
                .overlay(Capsule().stroke(Color.white.opacity(0.14), lineWidth: 0.5))
                .fixedSize().offset(x: clamp(x - 22, 0, bw - 44), y: -27)
        }
        .allowsHitTesting(false)
    }
}

// MARK: - the frames over it

/// A frame round a time on the bar: 1.5 pt with a radius of 8, 4 pt clear of the bar above and below, `pad` either side of what it frames, which is given `least` points at least.
struct TLBarFrame: View {
    let x0: Double, x1: Double
    let color: Color
    var glow = false
    var pad = 2.0
    var least = 6.0

    var body: some View {
        let w = max(least, x1 - x0) + 2 * pad
        RoundedRectangle(cornerRadius: 8).strokeBorder(color, lineWidth: 1.5)
            .frame(width: w, height: TLLayout.barH + 8)
            .shadow(color: TLPaint.warm.color(glow ? 0.25 : 0), radius: glow ? 7 : 0)
            .offset(x: (x0 + x1) / 2 - w / 2, y: -4)
            .allowsHitTesting(false)
    }
}

/// A time framed for a moment (`TLBlink`), with a warm glow: it comes in over the first 12% of its length, stays, and goes over the last 30%. Always in the bar, so that the first time it
/// is asked for is a change it plays.
private struct TLBlinkFrames: View {
    let blink: TLBlink?
    let axis: TLAxis

    var body: some View {
        let length = blink?.length ?? TLBlink.row
        ZStack(alignment: .topLeading) {
            ForEach(Array((blink?.spans ?? []).enumerated()), id: \.offset) { _, s in
                TLBarFrame(x0: axis.x(s.lowerBound), x1: axis.x(s.upperBound), color: TLPaint.warm.color(0.9), glow: true)
            }
        }
        .keyframeAnimator(initialValue: 0.0, trigger: blink?.n ?? 0) { content, shown in
            content.opacity(shown)
        } keyframes: { _ in
            KeyframeTrack {
                LinearKeyframe(1.0, duration: 0.12 * length)
                LinearKeyframe(1.0, duration: 0.58 * length)
                LinearKeyframe(0.0, duration: 0.3 * length)
            }
        }
        .allowsHitTesting(false)
    }
}

/// The part of the day the list shows, framed on the bar while he scrolls it and for a moment after (`TimelineState.scrolling`), never while a row is open where it stands. A view of its own,
/// over the bar: the list moves it on every frame of a scroll, and nothing else of the top is drawn again for it.
struct TLViewportFrame: View {
    let tl: TimelineState
    let axis: TLAxis

    var body: some View {
        let on = tl.viewportOn && tl.openRow == nil
        ZStack(alignment: .topLeading) {
            if let v = tl.viewport {
                TLBarFrame(x0: axis.x(v.lowerBound), x1: axis.x(v.upperBound), color: TLPaint.ink.color(0.5), pad: 3, least: 4)
            }
        }
        .frame(width: axis.width, height: TLLayout.barH, alignment: .topLeading)
        .opacity(on ? 1 : 0)
        .animation(.easeOut(duration: on ? 0.15 : 0.45), value: on)
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }
}

/// The words and icons the pieces wear. They are made from the scene alone, so as a view of their own that is drawn again only when the scene changes they are not measured again on every frame
/// of a finger moving over the bar.
private struct TLBarLabels: View, Equatable {
    let scene: TLBarScene
    let axis: TLAxis
    let reduce: Bool

    var body: some View {
        let bh = TLLayout.barH, bw = axis.width
        let placed = TLBarLabel.placed(scene, axis: axis)
        return ZStack(alignment: .topLeading) {
            ForEach(placed) { l in
                HStack(spacing: TLBarLabel.gap) {
                    if l.fit != .name, let icon = l.icon {
                        Image(systemName: icon).font(.system(size: 10.5, weight: .semibold)).frame(width: TLBarLabel.icon)
                    }
                    if l.fit != .icon, let name = l.name { Text(name).lineLimit(1) }
                }
                .font(.system(size: 11, weight: .semibold)).fixedSize()
                .foregroundStyle(l.dark ? TLPaint.darkInk.color(0.9) : TLPaint.ink.color(l.quiet ? 0.5 : 1))
                .frame(height: bh).offset(x: l.x)
                .transition(.opacity)
            }
        }
        .frame(width: bw, height: bh, alignment: .topLeading)
        .animation(TLMotion.fade(reduce: reduce), value: placed)          // a word goes with what it was on, as that fades
        .allowsHitTesting(false)
    }
}

/// The line for now: 2 pt of warm white with a glow that breathes over 2.4 s. The glow is a layer of its own, a shadow drawn once (and kept as it is drawn) whose opacity Core Animation lifts and
/// lowers from 0.3 to 0.8 of the shadow, on its own and with nothing asked of the app: not a shadow redrawn 30 times a second, nor an animation the app has to step. Paused it stands half lit.
private struct TLNowGlow: UIViewRepresentable {
    let height: Double
    let breathe: Bool

    func makeUIView(context: Context) -> GlowView { GlowView() }
    func updateUIView(_ v: GlowView, context: Context) { v.breathing = breathe }
    func sizeThatFits(_ proposal: ProposedViewSize, uiView: GlowView, context: Context) -> CGSize? { CGSize(width: 2, height: height) }

    final class GlowView: UIView {
        private let glow = CALayer(), line = CALayer()
        var breathing = false {
            didSet {
                guard breathing != oldValue else { return }
                if breathing {
                    let a = CABasicAnimation(keyPath: "opacity")
                    a.fromValue = 0.375; a.toValue = 1; a.duration = 1.2; a.autoreverses = true; a.repeatCount = .infinity
                    a.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
                    glow.add(a, forKey: "breathe")
                } else {
                    glow.removeAnimation(forKey: "breathe")
                    glow.opacity = 0.6875
                }
            }
        }

        override init(frame: CGRect) {
            super.init(frame: frame)
            let warm = UIColor(red: 1, green: 240 / 255, blue: 220 / 255, alpha: 1).cgColor
            for l in [glow, line] { l.backgroundColor = warm; l.cornerRadius = 1; layer.addSublayer(l) }
            glow.shadowColor = warm; glow.shadowOpacity = 0.8; glow.shadowRadius = 4; glow.shadowOffset = .zero
            glow.shouldRasterize = true; glow.rasterizationScale = UIScreen.main.scale
            glow.opacity = 0.6875
        }
        required init?(coder: NSCoder) { fatalError() }

        override func layoutSubviews() {
            super.layoutSubviews()
            glow.frame = bounds; line.frame = bounds
            glow.shadowPath = UIBezierPath(roundedRect: bounds, cornerRadius: 1).cgPath
        }
    }
}

/// What shows of the bar while it grows in: from the left, with a soft leading edge 28 pt wide that sweeps along it, so that the stretches appear in the order of time and none with a hard cut.
/// It is a fixed gradient that slides, so the sweep costs nothing to draw on every frame.
private struct GrowMask: View {
    let shown: Bool
    @Environment(\.tlBarWidth) private var w
    private let edge = 28.0

    var body: some View {
        LinearGradient(stops: [.init(color: .black, location: 0), .init(color: .black, location: w / (w + edge)), .init(color: .clear, location: 1)],
                       startPoint: .leading, endPoint: .trailing)
            .frame(width: w + edge, height: TLLayout.barH + 40)          // taller than the bar, so its edge is a line down the whole of it
            .offset(x: shown ? 0 : -(w + edge))
    }
}

/// one thing planned, on the bar: a dashed box 4 pt wide at least, inset half a point, or a tick 2 pt wide with a 4 pt dot on its head
private struct TLAheadMark: View {
    let a: TLAhead
    let x0: Double, x1: Double, height: Double

    var body: some View {
        switch a.shape {
        case .box:
            RoundedRectangle(cornerRadius: 3).stroke(TLPaint.ink.color(0.45), style: StrokeStyle(lineWidth: 1, dash: [3, 2.5]))
                .frame(width: max(x1 - x0, 4) - 1, height: height - 1).offset(x: x0 + 0.5, y: 0.5)
        case .tick:
            ZStack(alignment: .topLeading) {
                Rectangle().fill(TLPaint.ink.color(0.55)).frame(width: 2, height: height - 4).offset(x: x0 - 1, y: 2)
                Circle().fill(TLPaint.ink.color(0.95)).frame(width: 4, height: 4).offset(x: x0 - 2, y: 2)
            }
        }
    }
}

/// the pieces, the hatch on the trips, and the faint hatch of a stretch nothing speaks to (a blank: where nothing was recorded, or a stretch with no word for what he did)
private struct BarCanvas: View, Equatable {
    let scene: TLBarScene
    let rects: [CGRect]

    var body: some View {
        Canvas { ctx, size in
            ctx.clip(to: Path(roundedRect: CGRect(origin: .zero, size: size), cornerRadius: TLLayout.barRadius))
            for (p, r) in zip(scene.pieces, rects) {
                // the night is one plain piece like every other (yilun 10-20, as on the Mac): its stages are in the readout and drawn when its row opens
                if let l = TLPaint.look(p.fill) {
                    ctx.fill(Path(r), with: .color(l.rgb.color(l.alpha)))
                    if p.fill == .trip { hatch(ctx, r, TLPaint.ink.color(0.16)) }
                } else {
                    ctx.fill(Path(r), with: .color(Color.white.opacity(0.035)))
                    if p.fill == .blank { hatch(ctx, r, TLPaint.hole.color(0.13)) }
                }
            }
        }
        .allowsHitTesting(false)
    }

    /// fine 45-degree lines, 2 pt thick and 5 pt apart, in step across neighbouring pieces
    private func hatch(_ ctx: GraphicsContext, _ r: CGRect, _ color: Color) {
        var c = ctx
        c.clip(to: Path(r))
        let step = 5.0 * 2.0.squareRoot()
        var x = ((r.minX - r.height) / step).rounded(.down) * step
        while x < r.maxX {
            var line = Path()
            line.move(to: CGPoint(x: x, y: r.maxY)); line.addLine(to: CGPoint(x: x + r.height, y: r.minY))
            c.stroke(line, with: .color(color), lineWidth: 2)
            x += step
        }
    }
}
