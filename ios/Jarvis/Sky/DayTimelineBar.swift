import SwiftUI

// The day's bar (Sky/DayTimeline.swift puts it under her sentence): where he was, as pieces of one bar with a hair of ground between them, the night, the places in their
// colours, the trips hatched, and past now the part that has not happened with what is planned on it. Each piece wears the richest word that fits it. What mattered is a lit
// star on its top edge, now is a warm line that breathes. The pieces, the labels and the paint are plain values (below), so what is drawn can be tested without a screen.

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

/// a one-off thing that happened (a payment, a picture, a talk), drawn as a small dot on its minute in the colour of its chip
struct TLDot: Equatable {
    let lens: TLLens
    let t: Double
}

struct TLBarScene: Equatable {
    var span: ClosedRange<Double>
    var now: Double?
    var pieces: [TLPiece]           // in time order, one after the other over the whole span
    var ahead: [TLAhead]
    var stars: [TLStar]
    var ticks: [Int]                // the hours under the bar
    var sun: TLSun?                 // when the sun rose and set, in the row of the hours
    var dots: [TLDot] = []          // the moments of the past

    static let empty = TLBarScene(span: 360...DAY, now: nil, pieces: [], ahead: [], stars: [], ticks: [])
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
    /// The one-off things of the past that have a lens (a payment he made, a picture, a talk: a moment, not a span), in the order of the day. What is still to come is drawn as it is planned (`ahead`).
    var dots: [TLDot] {
        let hi = now ?? span.upperBound
        return lensMarks.flatMap { lens, marks in marks.filter { $0.t1 - $0.t0 < 1 && $0.t0 >= span.lowerBound && $0.t0 <= hi }.map { TLDot(lens: lens, t: $0.t0) } }
            .sorted { ($0.t, TLLens.allCases.firstIndex(of: $0.lens) ?? 0) < ($1.t, TLLens.allCases.firstIndex(of: $1.lens) ?? 0) }
    }

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
        return TLBarScene(span: span, now: now, pieces: pieces, ahead: ahead, stars: stars, ticks: ticks, sun: sun, dots: dots)
    }
}

// MARK: - paint

enum TLPaint {
    static let ink = RGB(238, 240, 251)
    static let darkInk = RGB(11, 13, 26)                // the text on a light fill
    static let warm = RGB(255, 240, 220)                // now, and what mattered
    static let sunTick = RGB(255, 226, 186)             // the sun where its glyph has no room: a star's glow
    static let trip = RGB(196, 208, 255)
    static let sleep = RGB(112, 120, 205)
    static let ground = RGB(9, 10, 20)                  // what the bar sits on: the sky under the scrim

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
        if hasIcon, icon <= room { return .icon }
        return .none
    }

    /// the width of a word set 11 pt semibold, the bar's type
    static func measure(_ s: String) -> Double {
        ceil((s as NSString).size(withAttributes: [.font: UIFont.systemFont(ofSize: 11, weight: .semibold)]).width)
    }

    struct Placed: Equatable, Identifiable {
        let item: String                // the piece or box it is on (a stay the night cuts in two has a piece on each side of it, and a word on each that has room)
        var x: Double                   // where its icon or word begins
        var fit: Fit
        var name: String?, icon: String?
        var dark: Bool                  // dark ink on a light fill
        var id: String { "\(item)@\(Int(x))" }       // one place on the bar: the same item may stand there twice, never at the same x
    }

    /// what each piece (and each box of an event) wears at this width of bar
    static func placed(_ scene: TLBarScene, width: Double, measure: (String) -> Double = TLBarLabel.measure) -> [Placed] {
        var out: [Placed] = []
        for (p, r) in zip(scene.pieces, TLGeo.rects(scene.pieces, span: scene.span, width: width, height: 1)) {
            let f = fit(name: p.name, icon: p.icon != nil, width: r.width, measure: measure)
            if f != .none { out.append(Placed(item: p.id, x: r.minX + pad, fit: f, name: p.name, icon: p.icon, dark: TLPaint.textIsDark(p.fill))) }
        }
        for a in scene.ahead where a.shape == .box {
            let x0 = TLGeo.x(a.t0, scene.span, width), x1 = TLGeo.x(a.t1, scene.span, width)
            let f = fit(name: a.name, icon: a.icon != nil, width: x1 - x0, measure: measure)
            if f != .none { out.append(Placed(item: a.id, x: x0 + pad, fit: f, name: a.name, icon: a.icon, dark: false)) }
        }
        return out
    }
}

enum TLGeo {
    static func x(_ t: Double, _ span: ClosedRange<Double>, _ width: Double) -> Double {
        (clamp(t, span.lowerBound, span.upperBound) - span.lowerBound) / (span.upperBound - span.lowerBound) * width
    }
    /// Whether the sun's glyph (12 pt wide, centred under its minute `x`) can be drawn in the row of the hours: it overlaps no hour's label (12 pt wide) and stays on the bar. The hours
    /// always win; where the glyph does not fit the sun is a tick only. `hours` are the centres of the labels.
    static func sunGlyphFits(_ x: Double, hours: [Double], width: Double) -> Bool {
        x >= 6 && x <= width - 6 && hours.allSatisfy { abs($0 - x) >= 12 }
    }

    /// where each piece is drawn: a hair of ground (1 pt) between neighbours, and never narrower than 2 pt, so nothing recorded is lost to the gap
    static func rects(_ pieces: [TLPiece], span: ClosedRange<Double>, width: Double, height: Double) -> [CGRect] {
        pieces.enumerated().map { i, p in
            let x0 = x(p.t0, span, width), x1 = x(p.t1, span, width)
            let l = i == 0 ? x0 : x0 + 0.5, r = i == pieces.count - 1 ? x1 : x1 - 0.5
            let mid = (l + r) / 2, half = max(r - l, 2) / 2
            return CGRect(x: mid - half, y: 0, width: half * 2, height: height)
        }
    }
}

// MARK: - the track

/// The soft band of light that goes along the empty track while the day is being asked for: where its left edge is `time` seconds in, for a track `width` wide and a band `band` wide.
/// A pass takes 1.4 s, easing in and out, and the track rests for 0.4 s before the next.
enum TLShimmer {
    static let period = 1.8, sweep = 1.4

    static func x(at time: Double, width: Double, band: Double) -> Double {
        let k = min(1, max(0, time.truncatingRemainder(dividingBy: period) / sweep))
        let e = k < 0.5 ? 2 * k * k : 1 - pow(-2 * k + 2, 2) / 2
        return -band + e * (width + band)
    }
}

/// The ground the bar's stretches lie on. While the day is being asked for it is all there is, and a soft band of light goes along it (it is still under Reduce Motion); nothing spins.
struct TLTrack: View {
    var loading = false
    var reduce = false
    private let band = 96.0

    var body: some View {
        RoundedRectangle(cornerRadius: TLLayout.barRadius).fill(Color.white.opacity(0.035))
            .overlay(alignment: .leading) {
                if loading && !reduce {
                    TimelineView(.animation(minimumInterval: 1 / 30)) { tl in
                        LinearGradient(colors: [.clear, Color.white.opacity(0.07), .clear], startPoint: .leading, endPoint: .trailing)
                            .frame(width: band)
                            .offset(x: TLShimmer.x(at: tl.date.timeIntervalSinceReferenceDate, width: TLLayout.barW, band: band))
                    }
                }
            }
            .clipShape(RoundedRectangle(cornerRadius: TLLayout.barRadius))
            .frame(width: TLLayout.barW, height: TLLayout.barH)
            .allowsHitTesting(false)
    }
}

// MARK: - the bar on screen

struct TLBarView: View {
    let scene: TLBarScene
    var bracket: ClosedRange<Double>?       // the span of the row he has open
    var scrub: Double?                      // the minute under his finger
    var lens = false                        // a chip is pressed: the bar goes quiet and the things of that kind stand out in their colour
    var lensColor = RGB(196, 208, 255)
    var lensMarks: [TLMark] = []
    var reveal = true                       // false until the bar has grown in
    var reduce = false
    var breathe = true

    private let bw = TLLayout.barW, bh = TLLayout.barH

    var body: some View {
        let rects = TLGeo.rects(scene.pieces, span: scene.span, width: bw, height: bh)
        ZStack(alignment: .topLeading) {
            ZStack(alignment: .topLeading) {
                ZStack(alignment: .topLeading) {
                    TLTrack()
                    BarCanvas(scene: scene, rects: rects)
                    planned
                    dots
                    if let b = bracket { bracketView(b).transition(.opacity) }      // under the words, so a line never runs through a symbol
                    labels
                }
                .animation(.easeOut(duration: 0.12), value: bracket != nil)  // it comes and goes with a fade; moving from one span to the next is as quick as the finger
                .frame(width: bw, height: bh, alignment: .topLeading)       // its own size: the marker and the hours stand outside the bar and would stretch the track
                .mask(alignment: .leading) { GrowMask(shown: reduce || reveal) }          // grows in from the left, in the order of time, its leading edge soft
                .opacity(reduce && !reveal ? 0 : 1)
                .animation(TLMotion.barGrowth(reduce: reduce), value: reveal)
                stars.opacity(reveal ? 1 : 0).animation(TLMotion.arrival(after: 0.7, reveal: reveal, reduce: reduce), value: reveal)      // once the bar has grown to them
            }
            .opacity(lens ? 0.25 : 1).animation(.easeOut(duration: 0.15), value: lens)
            lensLayer
            lensBracket
            hours.opacity(reveal ? 1 : 0).animation(TLMotion.arrival(after: 0.25, reveal: reveal, reduce: reduce), value: reveal)
            sunMarks.opacity(reveal ? 1 : 0).animation(TLMotion.arrival(after: 0.25, reveal: reveal, reduce: reduce), value: reveal)
            if let n = scene.now { nowMarker(n).opacity(reveal ? 1 : 0).animation(TLMotion.arrival(after: 0.7, reveal: reveal, reduce: reduce), value: reveal) }      // when the growth reaches it
            if let t = scrub { scrubMark(t) }
        }
        .frame(width: bw, height: bh, alignment: .topLeading)
        .contentShape(Rectangle().inset(by: -10))
    }

    private func x(_ t: Double) -> Double { TLGeo.x(t, scene.span, bw) }

    // MARK: words and stars

    private var labels: some View {
        let placed = TLBarLabel.placed(scene, width: bw)
        return ZStack(alignment: .topLeading) {
            ForEach(placed) { l in
                HStack(spacing: TLBarLabel.gap) {
                    if l.fit != .name, let icon = l.icon {
                        Image(systemName: icon).font(.system(size: 10.5, weight: .semibold)).frame(width: TLBarLabel.icon)
                    }
                    if l.fit != .icon, let name = l.name { Text(name).lineLimit(1) }
                }
                .font(.system(size: 11, weight: .semibold)).fixedSize()
                .foregroundStyle(l.dark ? TLPaint.darkInk.color(0.9) : TLPaint.ink.color)
                .frame(height: bh).offset(x: l.x)
                .transition(.opacity)
            }
        }
        .frame(width: bw, height: bh, alignment: .topLeading)
        .animation(tlStarFade(reduce: reduce), value: placed)          // a word goes with what it was on, as that fades
        .allowsHitTesting(false)
    }

    /// what is planned, on the part of the bar that has not happened: an event is a dashed box over its time, a reminder or a bus a tick. They are views and not paint, so that one he says
    /// is not his goes with a fade.
    private var planned: some View {
        ZStack(alignment: .topLeading) {
            ForEach(scene.ahead) { a in
                TLAheadMark(a: a, x0: x(a.t0), x1: x(a.t1), height: bh).transition(.opacity)
            }
        }
        .frame(width: bw, height: bh, alignment: .topLeading)
        .clipShape(RoundedRectangle(cornerRadius: TLLayout.barRadius))
        .animation(tlStarFade(reduce: reduce), value: scene.ahead.map(\.id))
        .allowsHitTesting(false)
    }

    /// the one-off things of the past as dots on their minute, half over the bar's lower edge, each in the colour of its chip and ringed in the ground so that it is seen on any fill
    private var dots: some View {
        ZStack(alignment: .topLeading) {
            ForEach(Array(scene.dots.enumerated()), id: \.offset) { _, d in
                Circle().fill(d.lens.color.color).overlay(Circle().strokeBorder(TLPaint.ground.color, lineWidth: 1))
                    .frame(width: 6, height: 6).offset(x: x(d.t) - 3, y: bh - 3)
            }
        }
        .frame(width: bw, height: bh, alignment: .topLeading)
        .allowsHitTesting(false)
    }

    /// what mattered: his mark a filled star and her pick an outlined one, standing on the top edge so that it is seen over any fill; one comes and goes with a fade
    private var stars: some View {
        ForEach(scene.stars) { s in
            TLBarStar(hers: s.hers, reduce: reduce)
                .offset(x: x(s.t) - 3.5, y: -8)
                .transition(.opacity)
        }
        .animation(tlStarFade(reduce: reduce), value: scene.stars)
        .allowsHitTesting(false)
    }

    /// the hours under the bar, the last one ending where the bar ends
    private var hours: some View {
        ForEach(scene.ticks, id: \.self) { h in
            Text(String(format: "%02d", h)).font(AT.mono(10)).foregroundStyle(RGB(196, 204, 238).color(0.38)).frame(width: 12)
                .offset(x: x(Double(h * 60)) - (h == 24 ? 12 : 6), y: bh + 4)
        }
        .allowsHitTesting(false)
    }

    /// The sun rising and setting, in the row of the hours (no words): where its small glyph overlaps no hour's label, a hair of a tick under the bar at its minute and a sun on the horizon
    /// under it; where it would, only a warm tick 1 x 4 pt hanging under the bar. The hours always win.
    private var sunMarks: some View {
        let centres = scene.ticks.map { x(Double($0 * 60)) - ($0 == 24 ? 6 : 0) }
        let marks = [scene.sun?.rise.map { ($0, "sunrise.fill") }, scene.sun?.set.map { ($0, "sunset.fill") }].compactMap { $0 }
            .filter { $0.0 >= scene.span.lowerBound && $0.0 <= scene.span.upperBound }
        return ZStack(alignment: .topLeading) {
            ForEach(marks, id: \.1) { t, symbol in
                if TLGeo.sunGlyphFits(x(t), hours: centres, width: bw) {
                    Rectangle().fill(TLPaint.ink.color(0.32)).frame(width: 1, height: 3).offset(x: x(t) - 0.5, y: bh + 1)
                    Image(systemName: symbol).font(.system(size: 9)).foregroundStyle(RGB(196, 204, 238).color(0.55)).frame(width: 12, height: 12).offset(x: x(t) - 6, y: bh + 4)
                } else {
                    Rectangle().fill(TLPaint.sunTick.color(0.9)).frame(width: 1, height: 4).offset(x: x(t) - 0.5, y: bh)
                }
            }
        }
        .allowsHitTesting(false)
    }

    /// the things of the lens's kind in its colour over the quiet bar: a span as it is, a moment as a tick of 3 pt, the whole height
    private var lensLayer: some View {
        ZStack(alignment: .topLeading) {
            ForEach(Array(lensMarks.enumerated()), id: \.offset) { _, m in
                let x0 = x(m.t0), x1 = x(m.t1), w = max(x1 - x0, 3)
                Rectangle().fill(lensColor.color(0.95)).frame(width: w, height: bh).offset(x: (x0 + x1) / 2 - w / 2)
            }
        }
        .frame(width: bw, height: bh, alignment: .topLeading)
        .clipShape(RoundedRectangle(cornerRadius: TLLayout.barRadius))
        .opacity(lens ? 1 : 0).animation(.easeOut(duration: 0.15), value: lens)
        .allowsHitTesting(false)
    }

    // MARK: the row he has open, now, and his finger

    /// with a lens on, the bar under the words is quiet and so is its bracket: this one stands over the lens's spans, whole
    private var lensBracket: some View {
        ZStack(alignment: .topLeading) {
            if lens, let b = bracket { bracketView(b).transition(.opacity) }
        }
        .frame(width: bw, height: bh, alignment: .topLeading)
        .animation(.easeOut(duration: 0.12), value: bracket != nil)
    }

    /// the span of the open row: its fill lifts and a 1.5 pt line goes round it (a moment is given 5 pt to be seen)
    private func bracketView(_ b: ClosedRange<Double>) -> some View {
        let x0 = x(b.lowerBound), x1 = x(b.upperBound), w = max(x1 - x0, 5)
        return RoundedRectangle(cornerRadius: 3).fill(Color.white.opacity(0.12))
            .overlay(RoundedRectangle(cornerRadius: 3).inset(by: 0.75).stroke(TLPaint.ink.color, lineWidth: 1.5))
            .frame(width: w, height: bh).offset(x: (x0 + x1) / 2 - w / 2)
            .allowsHitTesting(false)
    }

    /// 2 pt of warm white, 5 pt longer than the bar each way, with a glow that breathes over 2.4 s; its time above it, kept inside the bar's width
    private func nowMarker(_ n: Double) -> some View {
        let label = clock(n), lw = 30.0
        return ZStack(alignment: .topLeading) {
            TimelineView(.animation(minimumInterval: 1 / 30, paused: !breathe || reduce)) { tl in
                let k = breathe && !reduce ? 0.5 + 0.5 * sin(tl.date.timeIntervalSinceReferenceDate * 2 * .pi / 2.4) : 0.5
                RoundedRectangle(cornerRadius: 1).fill(TLPaint.warm.color).frame(width: 2, height: bh + 10)
                    .shadow(color: TLPaint.warm.color(0.3 + 0.5 * k), radius: 2.5 + 1.5 * k)
            }
            .frame(width: 2, height: bh + 10).offset(x: x(n) - 1, y: -5)
            Text(label).font(AT.mono(10)).foregroundStyle(TLPaint.warm.color).frame(width: lw)
                .offset(x: clamp(x(n) - lw / 2, 0, bw - lw), y: -19)
                .opacity(scrub == nil ? 1 : 0).animation(.easeOut(duration: 0.12), value: scrub == nil)
        }
        .allowsHitTesting(false)
    }

    /// a hairline over the minute and its time over the line, in a chip
    private func scrubMark(_ t: Double) -> some View {
        ZStack(alignment: .topLeading) {
            Rectangle().fill(TLPaint.ink.color(0.7)).frame(width: 1, height: bh + 8).offset(x: x(t) - 0.5, y: -4)
            Text(clock(t)).font(AT.mono(11)).foregroundStyle(TLPaint.ink.color).padding(.horizontal, 8).frame(height: 19)
                .background(Capsule().fill(Color(red: 28 / 255, green: 31 / 255, blue: 58 / 255)))
                .overlay(Capsule().stroke(Color.white.opacity(0.14), lineWidth: 0.5))
                .fixedSize().offset(x: clamp(x(t) - 22, 0, bw - 44), y: -27)
        }
        .allowsHitTesting(false)
    }
}

/// What shows of the bar while it grows in: from the left, with a soft leading edge 28 pt wide that sweeps along it, so that the stretches appear in the order of time and none with a hard cut.
/// It is a fixed gradient that slides, so the sweep costs nothing to draw on every frame.
private struct GrowMask: View {
    let shown: Bool
    private let edge = 28.0

    var body: some View {
        let w = TLLayout.barW
        LinearGradient(stops: [.init(color: .black, location: 0), .init(color: .black, location: w / (w + edge)), .init(color: .clear, location: 1)],
                       startPoint: .leading, endPoint: .trailing)
            .frame(width: w + edge, height: TLLayout.barH + 40)          // taller than the bar: the stars and the line for now stand outside it
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

/// the pieces and the hatch on the trips
private struct BarCanvas: View {
    let scene: TLBarScene
    let rects: [CGRect]

    var body: some View {
        Canvas { ctx, size in
            ctx.clip(to: Path(roundedRect: CGRect(origin: .zero, size: size), cornerRadius: TLLayout.barRadius))
            for (p, r) in zip(scene.pieces, rects) {
                // the night is one plain piece like every other (yilun 10-20, as on the Mac): its stages are in the readout and drawn when its row opens
                if let l = TLPaint.look(p.fill) {
                    ctx.fill(Path(r), with: .color(l.rgb.color(l.alpha)))
                    if p.fill == .trip { hatch(ctx, r) }
                } else {
                    ctx.fill(Path(r), with: .color(Color.white.opacity(p.fill == .blank ? 0.06 : 0.035)))
                }
            }
        }
        .allowsHitTesting(false)
    }

    /// fine 45-degree lines, 2 pt thick and 5 pt apart, in step across neighbouring trips
    private func hatch(_ ctx: GraphicsContext, _ r: CGRect) {
        var c = ctx
        c.clip(to: Path(r))
        let step = 5.0 * 2.0.squareRoot()
        var x = ((r.minX - r.height) / step).rounded(.down) * step
        while x < r.maxX {
            var line = Path()
            line.move(to: CGPoint(x: x, y: r.maxY)); line.addLine(to: CGPoint(x: x + r.height, y: r.minY))
            c.stroke(line, with: .color(TLPaint.ink.color(0.16)), lineWidth: 2)
            x += step
        }
    }
}
