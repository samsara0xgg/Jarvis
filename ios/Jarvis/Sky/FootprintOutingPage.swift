import SwiftUI
import MapKit

// One outing of the footprint day (ADR 0323), the page A1 slides in over the sky from the outing's row: the map framed to it, its four numbers, the speed and heart-rate curves and the list of
// its stops. A finger on either curve holds one time, and the map's dot, a line on both curves and the capsule by the dot follow it; with none held they stand at his fastest fix. Laid out in the app's 440 pt design space; her corner
// (`TLSeat.corner`, where she sits while these pages are open) stays clear, so the way back stands beside her and the title after it. The page paints no ground of its own: the sky is under it.

private let ink = SetColor.ink
private let muted = SetColor.muted

struct FootprintOutingPage: View {
    let model: FootprintModel
    let outing: FootprintOuting
    var phone = true                    // the phone's strip at the top and the map to the top edge; the iPad's sheet keeps the old head and its map under it
    let back: () -> Void
    @State private var moment: FootprintMoment?          // under a finger on a curve or played by the replay
    @State private var player = FootprintPlayer()

    private var scrub: Int64? { moment?.ms }
    private let track: FootprintTrack?
    private let parts: FootprintOutingParts             // the map's marks, the numbers, the curves and the stops, as the trip's row opened draws them too
    private var end: Int64 { parts.end }
    private var curves: FootprintCurves { parts.curves }
    private var route: FootprintMapRoute { parts.route }
    private var fastestMs: Int64? { parts.fastestMs }

    init(model: FootprintModel, outing: FootprintOuting, phone: Bool = true, back: @escaping () -> Void) {
        self.model = model; self.outing = outing; self.phone = phone; self.back = back
        track = model.tracks[outing.id]
        parts = FootprintOutingParts(model: model, outing: outing)
    }

    #if DEBUG
    /// the scene's: the page with a time already held, to see the curves and the map under a finger
    init(model: FootprintModel, outing: FootprintOuting, held: Int64) {
        self.init(model: model, outing: outing, back: {})
        _moment = State(initialValue: track.flatMap { FootprintMoment.on(outing, track: $0, atMs: held) })
    }
    #endif

    var body: some View {
        ZStack(alignment: .topLeading) {
            mapView.padding(.top, phone ? 0 : 76)
            if model.replay(.outing(outing.id)) != nil {
                FootprintReplayButton(player: player, play: play).offset(x: W - 30 - 36, y: 372)          // clear of the map's fade
            }
            FootprintBeatCaption(beat: moment?.beat, model: model).frame(width: W).offset(y: 298)
            if phone { strip } else { head }
            VStack(alignment: .leading, spacing: 0) {
                parts.figures.frame(width: FootprintCurves.width, alignment: .leading).padding(.leading, 30)
                parts.curveBlock(scrub: scrub) { held($0) }.padding(.leading, 30)
                stops
            }
            .padding(.top, 459)
        }
        .frame(width: W, height: H, alignment: .topLeading)
        .task { FootprintStreets.shared.ask(outing.stops.filter { FootprintDayContent.name($0) == nil }.map(\.coord)) }          // the streets of the stops nobody named
        .onDisappear { player.stop() }
        #if DEBUG
        .onAppear {
            FootprintHooks.curve = { x in held(x.map { curves.ms(x: $0) }) }
            FootprintHooks.outingPlay = play
        }
        #endif
    }

    // MARK: held and played

    /// A finger on a curve holds a time of the outing, or lifts: the cursor, the dot and the camera follow it on the line through the replay's schedule, eased, so that a little of the
    /// finger that is a long way on the ground glides there; a replay stands paused where the finger leaves it.
    private func held(_ ms: Int64?) {
        guard let ms else {
            let rest = player.phase == .rest
            player.lift()
            if rest { moment = nil }
            return
        }
        player.touch()          // a finger on a curve pauses the replay
        if let replay = model.replay(.outing(outing.id)) {
            connect()
            player.point(at: replay.seconds(atMs: ms), of: replay)
        } else if let track, let m = FootprintMoment.on(outing, track: track, atMs: ms) {
            moment = m
        }
    }

    private func play() {
        guard let replay = model.replay(.outing(outing.id)) else { return }
        connect()
        player.play(replay)
    }

    private func connect() {
        let model = model
        player.emit = { f, r, playing in moment = playing ? FootprintMoment(f, height: r.height) : FootprintMoment(finger: f, model: model) }
        player.ended = { moment = nil }
    }

    // MARK: head

    /// the phone's top (跨端UI标准 §2): the glass ‹ at the left and the two-line title after it, clear of her at the right
    private var strip: some View {
        ZStack(alignment: .topLeading) {
            TopStrip {
                TopGlassButton(symbol: "chevron.left", label: Words.Footprint.back, action: back)
            } middle: {
                EmptyView()
            } right: {
                EmptyView()
            }
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.system(size: Look.title, weight: .semibold)).foregroundStyle(Look.ink).lineLimit(1)
                Text(dateLine).font(Look.figures(Look.note)).foregroundStyle(Look.ink2).lineLimit(1)
            }
            .frame(width: W - 72 - 80, height: HomeTop.button, alignment: .leading)          // 80 pt kept at the right for her
            .offset(x: HomeTop.side + HomeTop.button + 12, y: HomeTop.buttonY)
        }
    }

    private var head: some View {
        ZStack(alignment: .topLeading) {
            Button(action: back) {
                Image(systemName: "chevron.left").font(.system(size: 17, weight: .semibold)).foregroundStyle(ink)
                    .frame(width: 44, height: 44).contentShape(Rectangle())
            }
            .buttonStyle(PressScale())
            .accessibilityLabel(Words.Footprint.back)
            .offset(x: 66, y: 57)        // 4 pt clear of her disc in the iPad sheet's corner
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
        let day = Words.date(Date(timeIntervalSince1970: Double(outing.start_ms) / 1000), in: model.clock.cal)
        return day + " · " + time(outing.start_ms) + (outing.end_ms == nil ? "" : "–" + time(end))
    }

    private func time(_ ms: Int64) -> String { parts.time(ms) }

    // MARK: map

    private var mapView: some View {
        let top = phone ? 76.0 : 0.0          // on the phone the map runs 76 pt higher, to the top edge, and keeps its route where it stood
        return FootprintMapView(routes: [route], places: parts.places, tokens: parts.tokens, dot: dot, follow: moment?.follow, lineWidth: 4.6,
                         fitPadding: UIEdgeInsets(top: 44 + top, left: 30, bottom: 44, right: 115), fitKey: outing.id, touched: { player.touch() })
            .frame(width: W, height: 371 + top)
            .mask(LinearGradient(stops: [.init(color: phone ? .black : .clear, location: 0), .init(color: .black, location: phone ? 0 : 62 / 371), .init(color: .black, location: (340 + top) / (371 + top)), .init(color: .clear, location: 1)],
                                 startPoint: .top, endPoint: .bottom))
    }

    /// the held time on the map: where he was, in the colour of his speed, with the time and the speed in its capsule; with no finger on a curve, the same at his fastest fix
    private var dot: FootprintMapDot? {
        guard let t = scrub ?? fastestMs, let c = moment?.coord ?? model.position(atMs: t) else { return nil }
        let v = model.speedMps(atMs: t)
        let label = time(t) + (v.map { "  " + Words.Footprint.held(speed: "\(Int(($0 * 3.6).rounded()))") } ?? "")
        return FootprintMapDot(coord: c, ms: t, rgb: FootprintPaint.speed(mps: v ?? 0), label: label, held: scrub != nil)
    }

    // MARK: stops

    private var stops: some View {
        ScrollView(showsIndicators: false) {
            VStack(alignment: .leading, spacing: 10) {
                ForEach(parts.steps) { StepRow(step: $0, time: time($0.t0), model: model) }
            }
            .padding(.horizontal, 30).padding(.top, 23).padding(.bottom, 130)
        }
        .scrollBounceBehavior(.basedOnSize)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
    }
}

/// What a trip shows wherever it is shown: the outing page above and a trip's row opened (Sky/RowStage.swift) draw the same marks on the map, numbers, curves and stops.
@MainActor struct FootprintOutingParts {
    let model: FootprintModel
    let outing: FootprintOuting
    let end: Int64
    let curves: FootprintCurves
    let route: FootprintMapRoute
    let firstFixMs: Int64?, lastFixMs: Int64?

    init(model: FootprintModel, outing: FootprintOuting) {
        self.model = model; self.outing = outing
        let end = outing.end_ms ?? max(model.reply.now_ms, outing.points.last?.at_ms ?? outing.start_ms)
        self.end = end
        curves = FootprintCurves(outing, endMs: end)
        route = model.route(outing)
        firstFixMs = route.points.first?.ms; lastFixMs = route.points.last?.ms
    }

    var steps: [Step] { Step.list(model, outing, end: end, firstFixMs: firstFixMs, lastFixMs: lastFixMs) }          // read when drawn: a stop's street may come after the page
    func time(_ ms: Int64) -> String { clock(model.clock.minute(ms: ms)) }
    /// where the cursor stands while no finger is on a curve: his fastest fix
    var fastestMs: Int64? { outing.fastest?.at_ms }
    private func stay(_ id: String?) -> FootprintStay? { id.flatMap { id in model.reply.stays.first { $0.id == id } } }

    // MARK: map

    var places: [FootprintMapPlace] {
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

    var tokens: [FootprintMapToken] {
        outing.stops.sorted { $0.start_ms < $1.start_ms }.enumerated().map {
            FootprintMapToken(id: $1.id, coord: CLLocationCoordinate2D(latitude: $1.lat, longitude: $1.lng), number: $0 + 1, ms: $1.start_ms)
        }
    }

    // MARK: numbers

    /// the four numbers (distance, how long, fastest, heart rate), as wide as they are given
    var figures: some View {
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
        .accessibilityElement(children: .contain)
    }

    /// "21.7 km" style: a number and its unit, split at the first space
    private static func split(_ s: String) -> (String, String?) {
        let p = s.split(separator: " ", maxSplits: 1).map(String.init)
        return (p[0], p.count > 1 ? p[1] : nil)
    }

    // MARK: curves

    /// the speed curve and, where his watch was on, the heart rate's, 380 pt wide; `held` is a finger on either
    func curveBlock(scrub: Int64?, held: @escaping (Int64?) -> Void) -> some View {
        let w = Words.Footprint.self
        return VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text(w.speedAxis); Spacer(minLength: 8); Text(w.colorsMatchMap)
            }
            .font(.system(size: 12.5)).foregroundStyle(muted).lineLimit(1).frame(width: FootprintCurves.width, height: 16).padding(.top, 10)
            FootprintSpeedCurve(c: curves, scrub: scrub ?? fastestMs) { held($0) }.padding(.top, 6)
            ticks.padding(.top, 4)
            if curves.hasHeart, let heart = outing.heart_rate {
                HStack {
                    Text(w.heartRate); Spacer(minLength: 8)
                    Text(w.heartAverage(avg: "\(Int(heart.avg_bpm.rounded()))", home: heart.home_avg_bpm.map { "\(Int($0.rounded()))" }))
                }
                .font(.system(size: 12.5).monospacedDigit()).foregroundStyle(muted).lineLimit(1).frame(width: FootprintCurves.width, height: 16).padding(.top, 12)
                FootprintHeartCurve(c: curves, scrub: scrub ?? fastestMs) { held($0) }.padding(.top, 6)
            }
        }
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

}

// MARK: figures

struct Figure: View {
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

struct Step: Identifiable {
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
    @MainActor static func list(_ model: FootprintModel, _ o: FootprintOuting, end: Int64, firstFixMs: Int64?, lastFixMs: Int64?) -> [Step] {
        func stay(_ id: String?) -> FootprintStay? { id.flatMap { id in model.reply.stays.first { $0.id == id } } }
        func name(_ s: FootprintStay?) -> String? { s.flatMap { $0.kept_place?.name ?? $0.place } }
        func stopName(_ s: FootprintStay) -> String? { name(s) ?? FootprintStreets.shared.street(at: s.coord) }
        let from = stay(o.from_stay), to = stay(o.to_stay)
        var mid: [(Int64, Int64, Kind, FootprintStay?)] = o.stops.sorted { $0.start_ms < $1.start_ms }.enumerated().map { ($1.start_ms, min($1.end_ms ?? end, end), .stop($0 + 1), $1) }
        mid += o.gaps.map { ($0.start_ms, $0.end_ms, .gap, nil) }
        mid.sort { $0.0 < $1.0 }
        var out = [Step(id: 0, kind: .depart(home: from?.kept_place?.role == "home"), t0: o.start_ms, t1: o.start_ms, place: name(from), tint: tintOf(from))]
        for m in mid { out.append(Step(id: out.count, kind: m.2, t0: m.0, t1: m.1, place: m.3.flatMap(stopName), tint: tintOf(m.3))) }
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

struct StepRow: View {
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
