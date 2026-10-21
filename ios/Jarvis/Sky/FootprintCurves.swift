import SwiftUI

// The two curves of an outing's page (ADR 0323): how fast he went and his heart rate, 380 pt wide on the outing's own time axis, the speed line coloured by its value on the ramp so the
// curve is its own legend. `FootprintCurves` is the pure part (where a time falls, where a value falls, which stretches the line breaks at); the two views draw it, and a drag on either
// reports one held time. Colours are `FootprintPaint` and `TLPaint`; none is written here.

extension FootprintPaint {
    static let distanceIcon = RGB(135, 163, 255)     // the small icon before the distance, as the board has it
}

struct FootprintCurves: Equatable {
    /// a value at a time: speed in km/h or heart rate in bpm
    struct Pt: Equatable { var ms: Int64, v: Double }
    /// the line does not cross a stretch with no fixes: the dotted bridge stands for it
    struct Bridge: Equatable { var from: Pt, to: Pt }
    /// a stop: a band over both ends of its time, its token at the middle
    struct Band: Equatable { var number: Int, startMs: Int64, endMs: Int64 }

    static let width = 380.0, speedH = 64.0, heartH = 30.0

    let startMs: Int64, endMs: Int64
    let runs: [[Pt]], bridges: [Bridge], bands: [Band]
    /// km/h at the top of the speed curve
    let top: Double
    let beats: [Pt]
    let homeBpm: Double?
    private let bpmLo: Double, bpmHi: Double

    init(_ o: FootprintOuting, endMs: Int64) {
        startMs = o.start_ms
        self.endMs = max(endMs, o.start_ms + 1)
        let end = self.endMs
        let bands = o.stops.sorted { $0.start_ms < $1.start_ms }.enumerated().map { Band(number: $0 + 1, startMs: $1.start_ms, endMs: min($1.end_ms ?? end, end)) }
        self.bands = bands

        // a point with no speed takes the one before it; a stop stands at zero from its first moment to its last (the points inside it are dropped, or the line would slope across it)
        var last = o.points.compactMap(\.speed_mps).first ?? 0
        var pts = o.points.map { p -> Pt in last = p.speed_mps ?? last; return Pt(ms: p.at_ms, v: last * 3.6) }
        pts.removeAll { p in bands.contains { $0.startMs <= p.ms && p.ms <= $0.endMs } }
        pts += bands.flatMap { [Pt(ms: $0.startMs, v: 0), Pt(ms: $0.endMs, v: 0)] }
        pts.sort { $0.ms < $1.ms }
        var runs: [[Pt]] = [], bridges: [Bridge] = []
        for p in pts {
            if let a = runs.last?.last, o.gaps.contains(where: { a.ms <= $0.start_ms && $0.end_ms <= p.ms }) {
                bridges.append(Bridge(from: a, to: p)); runs.append([p])
            } else if runs.isEmpty { runs = [[p]] } else { runs[runs.count - 1].append(p) }
        }
        self.runs = runs; self.bridges = bridges
        top = max(12, (pts.map(\.v).max() ?? 0) * 1.185)

        beats = (o.heart_rate?.samples ?? []).map { Pt(ms: $0.at_ms, v: $0.bpm) }
        homeBpm = o.heart_rate?.home_avg_bpm
        let all = beats.map(\.v) + [homeBpm].compactMap { $0 }
        bpmLo = (all.min() ?? 60).rounded(.down) - 4
        bpmHi = (all.max() ?? 100).rounded(.up) + 4
    }

    var hasHeart: Bool { beats.count > 1 }

    // MARK: where things fall

    /// x of a time on the 380 pt axis, clamped to the outing
    func x(_ ms: Int64) -> Double { Self.width * clamp(Double(ms - startMs) / Double(endMs - startMs), 0, 1) }
    /// the time under x, clamped to the outing: what a finger on a curve holds
    func ms(x: Double) -> Int64 { startMs + Int64((clamp(x / Self.width, 0, 1) * Double(endMs - startMs)).rounded()) }
    func y(kmh: Double) -> Double { Self.speedH * (1 - clamp(kmh / top, 0, 1)) }
    func y(bpm: Double) -> Double { 4 + (bpmHi - clamp(bpm, bpmLo, bpmHi)) / (bpmHi - bpmLo) * (Self.heartH - 8) }

    /// the faint lines at 40 and 80 km/h; on a slow outing, every 10
    var gridKmh: [Double] { Array(stride(from: top >= 60 ? 40.0 : 10.0, to: top * 0.95, by: top >= 60 ? 40 : 10)) }

    /// The ramp along the curve's height (0 at the foot): the speed line is stroked with it, so a point's colour is its value's.
    func rampStops() -> [(loc: Double, rgb: RGB)] {
        FootprintPaint.ramp.filter { $0.kmh < top }.map { ($0.kmh / top, $0.rgb) } + [(1, FootprintPaint.speed(kmh: top))]
    }

    /// Ticks every 20 minutes of the wall clock, the first at the next ten past the start (13:07 gives 13:10, 13:30, 13:50).
    func ticks(_ clock: TLClock) -> [Int64] {
        let m1 = clock.minute(ms: endMs)
        return stride(from: (clock.minute(ms: startMs) / 10).rounded(.up) * 10, through: m1, by: 20).map { clock.ms(minute: $0) }
    }

    // MARK: at a time

    /// His speed in km/h on the curve at a time; nil across a gap or outside the outing
    func kmh(at ms: Int64) -> Double? { runs.lazy.compactMap { Self.value($0, at: ms) }.first }
    func bpm(at ms: Int64) -> Double? { Self.value(beats, at: ms) }

    private static func value(_ xs: [Pt], at t: Int64) -> Double? {
        guard let f = xs.first, let l = xs.last, f.ms <= t, t <= l.ms else { return nil }
        guard let i = xs.firstIndex(where: { $0.ms >= t }), i > 0 else { return f.v }
        let a = xs[i - 1], b = xs[i]
        return b.ms == a.ms ? b.v : a.v + (b.v - a.v) * Double(t - a.ms) / Double(b.ms - a.ms)
    }

    /// The colours along a leg of the outing, for the rail between two rows: the speed at `count` even moments from `from` to `to` (still reads as zero).
    static func legColors(from: Int64, to: Int64, count: Int = 9, speedAt: (Int64) -> Double?) -> [RGB] {
        (0..<count).map { i in
            let t = from + Int64((Double(to - from) * Double(i) / Double(max(count - 1, 1))).rounded())
            return FootprintPaint.speed(mps: speedAt(t) ?? 0)
        }
    }
}

// MARK: speed

struct FootprintSpeedCurve: View {
    let c: FootprintCurves
    let scrub: Int64?
    let set: (Int64?) -> Void

    var body: some View {
        let w = FootprintCurves.width, h = FootprintCurves.speedH
        Canvas { ctx, _ in
            let ramp = GraphicsContext.Shading.linearGradient(
                Gradient(stops: c.rampStops().map { .init(color: $0.rgb.color, location: $0.loc) }), startPoint: CGPoint(x: 0, y: h), endPoint: CGPoint(x: 0, y: 0))
            for g in c.gridKmh {
                let y = c.y(kmh: g)
                ctx.stroke(Path { $0.move(to: CGPoint(x: 0, y: y)); $0.addLine(to: CGPoint(x: w, y: y)) }, with: .color(FootprintPaint.gap.color(0.08)), lineWidth: 1)
                ctx.draw(Text("\(Int(g))").font(.system(size: 12.5).monospacedDigit()).foregroundColor(SetColor.faint), at: CGPoint(x: w + 4, y: y), anchor: .leading)
            }
            for b in c.bands {
                let x0 = c.x(b.startMs)
                ctx.fill(Path(CGRect(x: x0, y: 0, width: max(c.x(b.endMs) - x0, 1), height: h)), with: .color(TLPaint.ink.color(0.035)))
            }
            for run in c.runs where run.count > 1 {
                let line = Path { p in
                    p.move(to: CGPoint(x: c.x(run[0].ms), y: c.y(kmh: run[0].v)))
                    for q in run.dropFirst() { p.addLine(to: CGPoint(x: c.x(q.ms), y: c.y(kmh: q.v))) }
                }
                var area = line
                area.addLine(to: CGPoint(x: c.x(run[run.count - 1].ms), y: h)); area.addLine(to: CGPoint(x: c.x(run[0].ms), y: h)); area.closeSubpath()
                ctx.opacity = 0.22; ctx.fill(area, with: ramp); ctx.opacity = 1
                ctx.stroke(line, with: ramp, style: StrokeStyle(lineWidth: 2, lineCap: .round, lineJoin: .round))
            }
            for b in c.bridges {
                let dots = Path { $0.move(to: CGPoint(x: c.x(b.from.ms), y: c.y(kmh: b.from.v))); $0.addLine(to: CGPoint(x: c.x(b.to.ms), y: c.y(kmh: b.to.v))) }
                ctx.stroke(dots, with: .color(FootprintPaint.gap.color(FootprintPaint.gapAlpha)), style: StrokeStyle(lineWidth: 1.6, lineCap: .round, dash: [0.1, 4]))
            }
            ctx.stroke(Path { $0.move(to: CGPoint(x: 0, y: h - 0.5)); $0.addLine(to: CGPoint(x: w, y: h - 0.5)) }, with: .color(FootprintPaint.gap.color(0.14)), lineWidth: 1)
            for b in c.bands {                  // on top of the line, so a stop's number is never under it
                let at = CGPoint(x: (c.x(b.startMs) + c.x(b.endMs)) / 2, y: 9), ring = CGRect(x: at.x - 6.5, y: at.y - 6.5, width: 13, height: 13)
                ctx.fill(Path(ellipseIn: ring), with: .color(FootprintPaint.token.color))
                ctx.stroke(Path(ellipseIn: ring), with: .color(TLPaint.ink.color(0.8)), lineWidth: 1)
                ctx.draw(Text("\(b.number)").font(.system(size: 9.5, weight: .medium).monospacedDigit()).foregroundColor(TLPaint.ink.color), at: at)
            }
            if let t = scrub {
                let x = c.x(t)
                ctx.stroke(Path { $0.move(to: CGPoint(x: x, y: 0)); $0.addLine(to: CGPoint(x: x, y: h)) }, with: .color(TLPaint.ink.color(0.7)), lineWidth: 1)
                if let v = c.kmh(at: t) {
                    let r = CGRect(x: x - 4, y: c.y(kmh: v) - 4, width: 8, height: 8)
                    ctx.fill(Path(ellipseIn: r), with: .color(FootprintPaint.speed(kmh: v).color))
                    ctx.stroke(Path(ellipseIn: r), with: .color(TLPaint.ink.color), lineWidth: 1.5)
                }
            }
        }
        .frame(width: w + 30, height: h)
        .scrubbing(c, set)
    }
}

// MARK: heart rate

struct FootprintHeartCurve: View {
    let c: FootprintCurves
    let scrub: Int64?
    let set: (Int64?) -> Void

    var body: some View {
        let w = FootprintCurves.width, h = FootprintCurves.heartH
        Canvas { ctx, _ in
            if let home = c.homeBpm {
                let y = c.y(bpm: home)
                ctx.stroke(Path { $0.move(to: CGPoint(x: 0, y: y)); $0.addLine(to: CGPoint(x: w, y: y)) }, with: .color(FootprintPaint.gap.color(0.3)), style: StrokeStyle(lineWidth: 1, dash: [2, 3]))
            }
            let line = Path { p in
                p.move(to: CGPoint(x: c.x(c.beats[0].ms), y: c.y(bpm: c.beats[0].v)))
                for q in c.beats.dropFirst() { p.addLine(to: CGPoint(x: c.x(q.ms), y: c.y(bpm: q.v))) }
            }
            ctx.stroke(line, with: .color(FootprintPaint.heart.color), style: StrokeStyle(lineWidth: 1.8, lineCap: .round, lineJoin: .round))
            if let t = scrub {
                let x = c.x(t)
                ctx.stroke(Path { $0.move(to: CGPoint(x: x, y: 0)); $0.addLine(to: CGPoint(x: x, y: h)) }, with: .color(TLPaint.ink.color(0.7)), lineWidth: 1)
                if let v = c.bpm(at: t) {
                    let r = CGRect(x: x - 3.5, y: c.y(bpm: v) - 3.5, width: 7, height: 7)
                    ctx.fill(Path(ellipseIn: r), with: .color(FootprintPaint.heart.color))
                    ctx.stroke(Path(ellipseIn: r), with: .color(TLPaint.ink.color), lineWidth: 1.5)
                }
            }
        }
        .frame(width: w + 30, height: h)
        .scrubbing(c, set)
    }
}

private extension View {
    /// One time held by a finger on the curve: it follows the finger at once, with no animation, and goes when the finger does.
    func scrubbing(_ c: FootprintCurves, _ set: @escaping (Int64?) -> Void) -> some View {
        contentShape(Rectangle())
            .gesture(DragGesture(minimumDistance: 0)
                .onChanged { set(c.ms(x: $0.location.x)) }
                .onEnded { _ in set(nil) })
    }
}
