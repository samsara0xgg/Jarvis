import SwiftUI
import MapKit

// One outing of the footprint day (ADR 0323), the page A1 slides in over the sky from the outing's row: the map framed to it, its four numbers, the speed and heart-rate curves and the list of
// its stops. A finger on either curve holds one time, and the map's dot, a line on both curves and the capsule by the dot follow it; with none held they stand at his fastest fix. Laid out in the app's 440 pt design space; her corner
// (`TLLayout.her`) stays clear, so the way back stands beside her and the title after it. The page paints no ground of its own: the sky is under it.

private let ink = SetColor.ink
private let muted = SetColor.muted

struct FootprintOutingPage: View {
    let model: FootprintModel
    let outing: FootprintOuting
    let back: () -> Void
    @State private var scrub: Int64?

    private let end: Int64
    private let curves: FootprintCurves
    private let route: FootprintMapRoute
    private let steps: [Step]

    init(model: FootprintModel, outing: FootprintOuting, back: @escaping () -> Void) {
        self.model = model; self.outing = outing; self.back = back
        let end = outing.end_ms ?? max(model.reply.now_ms, outing.points.last?.at_ms ?? outing.start_ms)
        self.end = end
        curves = FootprintCurves(outing, endMs: end)
        route = model.route(outing)
        let fixes = route.points
        steps = Step.list(model, outing, end: end, firstFixMs: fixes.first?.ms, lastFixMs: fixes.last?.ms)
    }

    #if DEBUG
    /// the scene's: the page with a time already held, to see the curves and the map under a finger
    init(model: FootprintModel, outing: FootprintOuting, held: Int64) {
        self.init(model: model, outing: outing, back: {})
        _scrub = State(initialValue: held)
    }
    #endif

    var body: some View {
        ZStack(alignment: .topLeading) {
            mapView.padding(.top, 76)
            head
            VStack(alignment: .leading, spacing: 0) {
                figures
                curveBlock
                stops
            }
            .padding(.top, 459)
        }
        .frame(width: W, height: H, alignment: .topLeading)
    }

    // MARK: head

    private var head: some View {
        ZStack(alignment: .topLeading) {
            Button(action: back) {
                Image(systemName: "chevron.left").font(.system(size: 17, weight: .semibold)).foregroundStyle(ink)
                    .frame(width: 44, height: 44).contentShape(Rectangle())
            }
            .buttonStyle(PressScale())
            .accessibilityLabel(Words.Footprint.back)
            .offset(x: TLLayout.her.x + 19, y: 57)        // 4 pt clear of her disc
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.system(size: 17, weight: .semibold)).foregroundStyle(ink).lineLimit(1)
                Text(dateLine).font(.system(size: 12.5).monospacedDigit()).foregroundStyle(muted).lineLimit(1)
            }
            .offset(x: 110, y: 59)
            .frame(maxWidth: W - 110 - 30, alignment: .leading)
        }
    }

    private var title: String {
        switch model.title(outing) {
        case .event(let name): name
        case .to(let place): Words.Footprint.outingTo(place)
        case .onTheWay: Words.Footprint.onTheWay
        case .outing: Words.Footprint.outing
        }
    }

    private var dateLine: String {
        let day = Words.Footprint.dateLine(Date(timeIntervalSince1970: Double(outing.start_ms) / 1000), in: model.clock.cal)
        return day + " · " + time(outing.start_ms) + (outing.end_ms == nil ? "" : "–" + time(end))
    }

    private func time(_ ms: Int64) -> String { clock(model.clock.minute(ms: ms)) }

    // MARK: map

    private var mapView: some View {
        FootprintMapView(routes: [route], places: places, tokens: tokens, dot: dot, follow: scrub.flatMap { model.follow(atMs: $0) }, lineWidth: 4.6,
                         fitPadding: UIEdgeInsets(top: 44, left: 30, bottom: 44, right: 115), fitKey: outing.id)
            .frame(width: W, height: 371)
            .mask(LinearGradient(stops: [.init(color: .clear, location: 0), .init(color: .black, location: 62 / 371), .init(color: .black, location: 340 / 371), .init(color: .clear, location: 1)],
                                 startPoint: .top, endPoint: .bottom))
    }

    private func stay(_ id: String?) -> FootprintStay? { id.flatMap { id in model.reply.stays.first { $0.id == id } } }

    private var places: [FootprintMapPlace] {
        let first = route.points.first?.coord, last = route.points.last?.coord
        let from = stay(outing.from_stay), to = stay(outing.to_stay)
        func at(_ s: FootprintStay?, _ fallback: CLLocationCoordinate2D?) -> CLLocationCoordinate2D? {
            s.map { CLLocationCoordinate2D(latitude: $0.lat, longitude: $0.lng) } ?? fallback
        }
        var out: [FootprintMapPlace] = []
        if let c = at(from, first) {
            let home = from?.kept_place?.role == "home"
            out.append(FootprintMapPlace(id: "from", coord: c, discRadius: 0, tint: Step.tintOf(from), pin: home ? .home : .place, label: Words.Footprint.left(time(outing.start_ms))))
        }
        if let c = at(to, last) {
            out.append(FootprintMapPlace(id: "to", coord: c, discRadius: 0, tint: outing.end_ms == nil ? TLPaint.warm : Step.tintOf(to), pin: .end,
                                         label: outing.end_ms.map { Words.Footprint.arrived(time($0)) }))
        }
        return out
    }

    private var tokens: [FootprintMapToken] {
        outing.stops.sorted { $0.start_ms < $1.start_ms }.enumerated().map {
            FootprintMapToken(id: $1.id, coord: CLLocationCoordinate2D(latitude: $1.lat, longitude: $1.lng), number: $0 + 1, ms: $1.start_ms)
        }
    }

    /// where the cursor stands while no finger is on a curve: his fastest fix
    private var fastestMs: Int64? { outing.fastest?.at_ms }

    /// the held time on the map: where he was, in the colour of his speed, with the time and the speed in its capsule; with no finger on a curve, the same at his fastest fix
    private var dot: FootprintMapDot? {
        guard let t = scrub ?? fastestMs, let c = model.position(atMs: t) else { return nil }
        let v = model.speedMps(atMs: t)
        let label = time(t) + (v.map { "  " + Words.Footprint.held(speed: "\(Int(($0 * 3.6).rounded()))") } ?? "")
        return FootprintMapDot(coord: c, ms: t, rgb: FootprintPaint.speed(mps: v ?? 0), label: label, held: scrub != nil)
    }

    // MARK: numbers

    private var figures: some View {
        let w = Words.Footprint.self
        let dist = tlDistance(outing.distance_m).map(Self.split) ?? ("–", nil)
        let fast = outing.max_speed_mps.map { (w.held(speed: "\(Int(($0 * 3.6).rounded()))")) }.map(Self.split) ?? ("–", nil)
        let heart = outing.heart_rate.map { ("\(Int($0.avg_bpm.rounded()))", Optional(w.average)) } ?? ("–", nil)
        return HStack(alignment: .top, spacing: 8) {
            Figure(symbol: "point.topleft.down.to.point.bottomright.curvepath", tint: FootprintPaint.distanceIcon, label: w.distance, value: dist.0, unit: dist.1)
            Figure(symbol: "clock", tint: TLPaint.trip, label: w.duration, value: tlDur(Int((Double(end - outing.start_ms) / 60000).rounded())), unit: nil)
            Figure(symbol: "speedometer", tint: RGB.done, label: w.fastest, value: fast.0, unit: fast.1)
            Figure(symbol: "heart.fill", tint: FootprintPaint.heart, label: w.heartRate, value: heart.0, unit: heart.1)
        }
        .frame(width: FootprintCurves.width, alignment: .leading)
        .padding(.leading, 30)
        .accessibilityElement(children: .contain)
    }

    /// "21.7 km" style: a number and its unit, split at the first space
    private static func split(_ s: String) -> (String, String?) {
        let p = s.split(separator: " ", maxSplits: 1).map(String.init)
        return (p[0], p.count > 1 ? p[1] : nil)
    }

    // MARK: curves

    private var curveBlock: some View {
        let w = Words.Footprint.self
        return VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text(w.speedAxis); Spacer(minLength: 8); Text(w.colorsMatchMap)
            }
            .font(.system(size: 12.5)).foregroundStyle(muted).lineLimit(1).frame(width: FootprintCurves.width, height: 16).padding(.top, 10)
            FootprintSpeedCurve(c: curves, scrub: scrub ?? fastestMs) { scrub = $0 }.padding(.top, 6)
            ticks.padding(.top, 4)
            if curves.hasHeart, let heart = outing.heart_rate {
                HStack {
                    Text(w.heartRate); Spacer(minLength: 8)
                    Text(w.heartAverage(avg: "\(Int(heart.avg_bpm.rounded()))", home: heart.home_avg_bpm.map { "\(Int($0.rounded()))" }))
                }
                .font(.system(size: 12.5).monospacedDigit()).foregroundStyle(muted).lineLimit(1).frame(width: FootprintCurves.width, height: 16).padding(.top, 12)
                FootprintHeartCurve(c: curves, scrub: scrub ?? fastestMs) { scrub = $0 }.padding(.top, 6)
            }
        }
        .padding(.leading, 30)
    }

    private var ticks: some View {
        ZStack(alignment: .topLeading) {
            ForEach(curves.ticks(model.clock), id: \.self) { ms in
                let x = curves.x(ms)
                if x >= 12, x <= FootprintCurves.width - 12 {
                    Text(time(ms)).font(.system(size: 12.5).monospacedDigit()).foregroundStyle(SetColor.faint).frame(width: 50).offset(x: x - 25)
                }
            }
        }
        .frame(width: FootprintCurves.width, height: 15, alignment: .topLeading)
        .accessibilityHidden(true)
    }

    // MARK: stops

    private var stops: some View {
        ScrollView(showsIndicators: false) {
            VStack(alignment: .leading, spacing: 10) {
                ForEach(steps) { StepRow(step: $0, time: time($0.t0), model: model) }
            }
            .padding(.horizontal, 30).padding(.top, 23).padding(.bottom, 130)
        }
        .scrollBounceBehavior(.basedOnSize)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
    }
}

// MARK: figures

private struct Figure: View {
    let symbol: String, tint: RGB, label: String, value: String, unit: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 5) {
                Image(systemName: symbol).font(.system(size: 11)).foregroundStyle(tint.color).frame(width: 12)
                Text(label).font(.system(size: 12.5)).foregroundStyle(muted).lineLimit(1)
            }
            HStack(alignment: .firstTextBaseline, spacing: 3) {
                Text(value).font(.system(size: 17, weight: .semibold).monospacedDigit()).foregroundStyle(ink).lineLimit(1).minimumScaleFactor(0.7)
                if let unit { Text(unit).font(.system(size: 12.5)).foregroundStyle(muted).lineLimit(1) }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
    }
}

// MARK: the list of stops

private struct Step: Identifiable {
    enum Kind { case depart(home: Bool), stop(Int), gap, arrive }
    let id: Int
    let kind: Kind
    let t0: Int64, t1: Int64
    let place: String?
    let tint: RGB
    /// the rail to the next row: the speeds of that leg, or dotted into a stretch with no fixes
    var rail: [RGB] = []
    var dotted = false

    var minutes: Int { Int((Double(t1 - t0) / 60000).rounded()) }

    static func tintOf(_ s: FootprintStay?) -> RGB { s?.kept_place.map { PlaceTint.line(role: $0.role, id: $0.id) } ?? PlaceTint.lineNeutral }

    /// departure, each stop and each stretch with no fixes by time, and the arrival unless he is still out
    static func list(_ model: FootprintModel, _ o: FootprintOuting, end: Int64, firstFixMs: Int64?, lastFixMs: Int64?) -> [Step] {
        func stay(_ id: String?) -> FootprintStay? { id.flatMap { id in model.reply.stays.first { $0.id == id } } }
        func name(_ s: FootprintStay?) -> String? { s.flatMap { $0.kept_place?.name ?? $0.place } }
        let from = stay(o.from_stay), to = stay(o.to_stay)
        var mid: [(Int64, Int64, Kind, FootprintStay?)] = o.stops.sorted { $0.start_ms < $1.start_ms }.enumerated().map { ($1.start_ms, min($1.end_ms ?? end, end), .stop($0 + 1), $1) }
        mid += o.gaps.map { ($0.start_ms, $0.end_ms, .gap, nil) }
        mid.sort { $0.0 < $1.0 }
        var out = [Step(id: 0, kind: .depart(home: from?.kept_place?.role == "home"), t0: o.start_ms, t1: o.start_ms, place: name(from), tint: tintOf(from))]
        for m in mid { out.append(Step(id: out.count, kind: m.2, t0: m.0, t1: m.1, place: name(m.3), tint: tintOf(m.3))) }
        if o.end_ms != nil { out.append(Step(id: out.count, kind: .arrive, t0: end, t1: end, place: name(to), tint: o.to_stay == nil ? TLPaint.warm : tintOf(to))) }
        for i in out.indices.dropLast() {
            out[i].rail = FootprintCurves.legColors(from: out[i].t1, to: out[i + 1].t0) { model.speedMps(atMs: $0) }
            // dotted like a gap: into a stretch with no fixes, before the first fix and after the last
            if case .gap = out[i + 1].kind { out[i].dotted = true }
            if let f = firstFixMs, out[i + 1].t0 <= f { out[i].dotted = true }
            if let l = lastFixMs, out[i].t1 >= l { out[i].dotted = true }
        }
        return out
    }
}

private struct StepRow: View {
    let step: Step
    let time: String
    let model: FootprintModel

    var body: some View {
        HStack(spacing: 0) {
            Text(time).font(.system(size: 12.5).monospacedDigit()).foregroundStyle(muted).frame(width: 51, alignment: .leading)
            glyph.frame(width: 18, height: 18)
            Text(title).font(.system(size: gap ? 12.5 : 15)).foregroundStyle(gap ? muted : ink).lineLimit(1).padding(.leading, 15)
            Spacer(minLength: 8)
            if let right { Text(right).font(.system(size: 12.5)).foregroundStyle(muted).lineLimit(1) }
        }
        .frame(height: 18)
        .overlay(alignment: .topLeading) { rail.offset(x: 59, y: 19) }
        .accessibilityElement(children: .combine)
        .accessibilityLabel([time, title, right].compactMap { $0 }.joined(separator: ", "))
    }

    private var gap: Bool { if case .gap = step.kind { true } else { false } }

    private var title: String {
        let w = Words.Footprint.self
        switch step.kind {
        case .gap: return w.unrecorded(tlDur(step.minutes))
        case .stop: return step.place ?? w.stopFor(tlDur(step.minutes))
        case .depart, .arrive: return step.place ?? ""
        }
    }

    private var right: String? {
        let w = Words.Footprint.self
        switch step.kind {
        case .depart: return w.departed
        case .stop: return step.place == nil ? nil : w.stopFor(tlDur(step.minutes))
        case .gap: return nil
        case .arrive: return w.arrivedAt
        }
    }

    @ViewBuilder private var glyph: some View {
        switch step.kind {
        case .depart(let home):
            if home {
                Circle().fill(FootprintPaint.home.color)
                    .overlay(Image(systemName: "house.fill").font(.system(size: 9, weight: .bold)).foregroundStyle(TLPaint.darkInk.color))
            } else {
                Circle().fill(step.tint.color).frame(width: 12, height: 12)
            }
        case .stop(let n):
            Circle().fill(FootprintPaint.token.color)
                .overlay(Circle().strokeBorder(TLPaint.ink.color(0.85), lineWidth: 1.25))
                .overlay(Text("\(n)").font(.system(size: 9.5, weight: .medium).monospacedDigit()).foregroundStyle(ink))
        case .gap:
            Rectangle().fill(.clear).overlay(alignment: .top) { dots(height: 18) }
        case .arrive:
            Circle().fill(step.tint.color).frame(width: 12, height: 12)
        }
    }

    @ViewBuilder private var rail: some View {
        if step.dotted {
            dots(height: 8)
        } else if !step.rail.isEmpty {
            RoundedRectangle(cornerRadius: 1)
                .fill(LinearGradient(colors: step.rail.map(\.color), startPoint: .top, endPoint: .bottom))
                .frame(width: 2, height: 8)
        }
    }

    /// a rail of dots, for a stretch with no fixes
    private func dots(height: CGFloat) -> some View {
        Path { $0.move(to: CGPoint(x: 1, y: 0)); $0.addLine(to: CGPoint(x: 1, y: height)) }
            .stroke(FootprintPaint.gap.color(FootprintPaint.gapAlpha), style: StrokeStyle(lineWidth: 2, lineCap: .round, dash: [0.1, 4]))
            .frame(width: 2, height: height)
    }
}
