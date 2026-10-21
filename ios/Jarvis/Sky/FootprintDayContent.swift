import SwiftUI
import CoreLocation

// What the footprint's one-day screen draws that comes from the brain's footprint: the map's routes, places, stops and now, the list's rows, the two chips of its own and the sentence under
// the head. Plain values made from a `FootprintModel`, so the order of the rows and the words can be tested without a screen (Sky/FootprintDayPage.swift draws them).

struct FootprintDayContent: Equatable {
    var routes: [FootprintMapRoute] = []
    var places: [FootprintMapPlace] = []
    var tokens: [FootprintMapToken] = []
    var now: FootprintMapNow?
    var entries: [TLEntry] = []
    var chips: [TLPlainChip] = []
    var summary = ""

    /// how many slices an outing's speed ribbon is cut into
    static let ribbonSlices = 64

    // MARK: words

    @MainActor static func titleText(_ t: FootprintTitle) -> String {
        switch t {
        case .event(let name): name
        case .to(let place): Words.Footprint.outingTo(place)
        case .onTheWay: Words.Footprint.onTheWay
        case .outing: Words.Footprint.outing
        }
    }

    /// the name a stay goes by: the place he keeps, else what the phone called it
    static func name(_ s: FootprintStay) -> String? {
        [s.kept_place?.name, s.place].lazy.compactMap { $0?.trimmingCharacters(in: .whitespacesAndNewlines) }.first { !$0.isEmpty }
    }

    @MainActor static func speedText(_ mps: Double) -> String { Words.Footprint.held(speed: String(Int((mps * 3.6).rounded()))) }

    // MARK: making it

    /// `past`: a day before today, whose sentence says no more of now
    @MainActor static func make(_ m: FootprintModel, past: Bool) -> FootprintDayContent {
        var c = FootprintDayContent()
        let r = m.reply
        let nowMinute: Double? = r.now_ms >= r.start_ms && r.now_ms < r.end_ms ? m.clock.minute(ms: r.now_ms) : nil
        func minute(_ ms: Int64) -> Double { m.clock.minute(ms: ms) }
        func coord(_ lat: Double, _ lng: Double) -> CLLocationCoordinate2D { CLLocationCoordinate2D(latitude: lat, longitude: lng) }

        // the map: a route for each outing, its stops numbered
        for o in r.outings {
            c.routes.append(m.route(o))
            for (n, s) in o.stops.sorted(by: { $0.start_ms < $1.start_ms }).enumerated() {
                c.tokens.append(FootprintMapToken(id: s.id, coord: coord(s.lat, s.lng), number: n + 1, ms: s.start_ms))
            }
        }

        // the long stays: a place he keeps is one disc and pin however often he came back to it
        var order: [String] = [], groups: [String: [FootprintStay]] = [:]
        for s in r.stays {
            let key = s.kept_place?.id ?? s.id
            if groups[key] == nil { order.append(key) }
            groups[key, default: []].append(s)
        }
        let nowStay = m.nowStay
        func spent(_ s: FootprintStay) -> Double {
            let t0 = max(0, minute(s.start_ms)), t1 = min(DAY, s.end_ms.map(minute) ?? nowMinute ?? DAY)
            return max(0, t1 - t0)
        }
        for key in order {
            guard let list = groups[key], let first = list.first else { continue }
            let total = list.reduce(0) { $0 + spent($1) }
            let kept = first.kept_place
            let here = nowStay.map { n in list.contains { $0.id == n.id } } ?? false          // the now mark says how long he has been here, so the pin keeps its word to itself
            c.places.append(FootprintMapPlace(
                id: key, coord: coord(first.lat, first.lng), discRadius: FootprintModel.discRadius(minutes: total),
                tint: kept.map { PlaceTint.line(role: $0.role, id: $0.id) } ?? PlaceTint.lineNeutral, pin: kept?.role == "home" ? .home : .place,
                label: here ? nil : name(first).map { $0 + " " + tlDur(tlInt(total)) }))
        }
        if let s = nowStay {
            c.now = FootprintMapNow(coord: coord(s.lat, s.lng), label: Words.Footprint.nowFor(tlDur(tlInt(Double(r.now_ms - s.start_ms) / 60_000))))
        } else if nowMinute != nil, let o = r.outings.last, o.end_ms == nil, let p = o.points.last {
            c.now = FootprintMapNow(coord: coord(p.lat, p.lng), label: Words.Footprint.nowFor(tlDur(tlInt(Double(r.now_ms - o.start_ms) / 60_000))))
        }

        // the list: each long stay and each outing in the order of the day, an outing followed by its stops and the stretches with no fixes
        enum Item { case stay(FootprintStay), outing(FootprintOuting)
            var start: Int64 { switch self { case .stay(let s): s.start_ms; case .outing(let o): o.start_ms } } }
        let items = (r.stays.map(Item.stay) + r.outings.map(Item.outing)).sorted { $0.start < $1.start }
        var rows: [TLEntry] = []
        for item in items {
            switch item {
            case .stay(let s):
                let t0 = max(0, minute(s.start_ms)), t1 = min(DAY, s.end_ms.map(minute) ?? nowMinute ?? DAY)
                var e = TLEntry(id: s.id, kind: .stay, t0: t0, title: name(s) ?? Words.FootprintDay.unnamedPlace)
                e.t1 = t1; e.going = s.end_ms == nil
                e.color = s.kept_place.map { PlaceTint.line(role: $0.role, id: $0.id) } ?? PlaceTint.lineNeutral
                e.kept = s.kept_place != nil
                e.right = clock(t0) + "\u{2013}" + (s.end_ms == nil ? "" : tlEnd(t1)) + " \u{00B7} " + tlDur(tlInt(t1 - t0))
                rows.append(e)
            case .outing(let o):
                rows.append(contentsOf: outingRows(o, m, nowMinute: nowMinute))
            }
        }
        // the rails: dashed from a place down through its outing to the next place
        for k in rows.indices {
            let before = k > 0 ? rows[k - 1] : nil, after = k + 1 < rows.count ? rows[k + 1] : nil
            if rows[k].kind == .stay {
                rows[k].railIn = before.map { $0.kind != .stay } ?? false
                rows[k].railOut = after.map { $0.kind != .stay } ?? false
                if rows[k].railIn || rows[k].railOut { rows[k].rail = .trip }
            }
        }
        if let n = nowMinute {
            var line = TLEntry(id: "now", kind: .now, t0: n, title: clock(n))
            line.time = clock(n)
            rows.insert(line, at: rows.firstIndex { $0.t0 > n } ?? rows.count)
        }
        c.entries = rows

        // the two numbers of this page
        if let d = tlDistance(m.totalDistanceM) {
            c.chips.append(TLPlainChip(symbol: "point.topleft.down.to.point.bottomright.curvepath", color: RGB.work, word: Words.Footprint.distance, value: d))
        }
        if let v = m.topSpeedMps {
            c.chips.append(TLPlainChip(symbol: "speedometer", color: FootprintPaint.speed(mps: v), word: Words.Footprint.fastest, value: speedText(v)))
        }
        c.summary = summary(m, past: past)
        return c
    }

    /// the row of an outing, then its stops (numbered) and the stretches with no fixes, in time
    @MainActor private static func outingRows(_ o: FootprintOuting, _ m: FootprintModel, nowMinute: Double?) -> [TLEntry] {
        func minute(_ ms: Int64) -> Double { m.clock.minute(ms: ms) }
        let t0 = minute(o.start_ms), t1 = o.end_ms.map(minute) ?? nowMinute ?? t0
        var e = TLEntry(id: o.id, kind: .trip, t0: t0, title: titleText(m.title(o)))
        e.time = clock(t0); e.t1 = t1; e.going = o.end_ms == nil
        e.symbol = DayTimelineModel.moves[o.activity ?? ""]?.1 ?? "arrow.right"
        e.detail = Words.Footprint.rowSub(distance: tlDistance(max(1, o.distance_m)) ?? "", speed: speedText(o.max_speed_mps ?? 0), stops: o.stops.count)
        e.right = tlDur(tlInt(t1 - t0))
        e.tapOnly = true
        e.rail = .trip; e.railIn = true; e.railOut = true
        e.ribbon = ribbon(o, m)
        var subs: [(start: Int64, row: TLEntry)] = []
        for (n, s) in o.stops.sorted(by: { $0.start_ms < $1.start_ms }).enumerated() {
            let s0 = minute(s.start_ms), s1 = s.end_ms.map(minute) ?? nowMinute ?? s0
            let stopped = Words.Footprint.stopFor(tlDur(tlInt(s1 - s0)))
            let place = name(s)
            var row = TLEntry(id: s.id, kind: .pause, t0: s0, title: place ?? stopped)
            row.time = clock(s0); row.t1 = s1; row.token = n + 1; row.right = place == nil ? "" : stopped
            row.rail = .trip; row.railIn = true; row.railOut = true
            subs.append((s.start_ms, row))
        }
        for g in o.gaps {
            let g0 = minute(g.start_ms), g1 = minute(g.end_ms)
            var row = TLEntry(id: "gap-\(g.start_ms)", kind: .pause, t0: g0, title: Words.Footprint.unrecorded(tlDur(tlInt(g1 - g0))))
            row.time = clock(g0); row.t1 = g1; row.unknown = true
            row.rail = .gap; row.railIn = true; row.railOut = true
            subs.append((g.start_ms, row))
        }
        return [e] + subs.sorted { $0.start < $1.start }.map(\.row)
    }

    /// the speed along an outing in slices of equal time: the colour of the ramp, none where he stood still or nothing was recorded
    private static func ribbon(_ o: FootprintOuting, _ m: FootprintModel) -> [RGB?] {
        let end = o.end_ms ?? max(m.reply.now_ms, o.start_ms + 1), span = Double(end - o.start_ms)
        return (0..<ribbonSlices).map { i in
            let ms = o.start_ms + Int64(span * (Double(i) + 0.5) / Double(ribbonSlices))
            return m.speedMps(atMs: ms).map { FootprintPaint.speed(mps: $0) }
        }
    }

    // MARK: the sentence under the head

    @MainActor private static func summary(_ m: FootprintModel, past: Bool) -> String {
        let r = m.reply
        guard let only = r.outings.first else { return Words.Footprint.summaryNone(past: past) }
        let distance = tlDistance(r.outings.count == 1 ? only.distance_m : m.totalDistanceM) ?? ""
        var s: String
        if r.outings.count == 1 {
            let title = titleText(m.title(only))
            s = only.max_speed_mps.map { Words.Footprint.summaryOne(title: title, distance: distance, speed: speedText($0)) } ?? Words.FootprintDay.summaryOneBare(title: title, distance: distance)
        } else {
            s = Words.Footprint.summaryMany(count: r.outings.count, distance: distance, past: past)
        }
        if !past, let here = m.nowStay.flatMap(name) { s = Words.FootprintDay.then(s, Words.Footprint.nowAt(place: here)) }
        return s
    }

    // MARK: a finger on the bar

    /// the row to mark for the minute under a finger: a stop or a stretch with no fixes inside an outing before the outing, that before the place
    static func hot(at minute: Double, in rows: [TLEntry]) -> String? {
        let covering = rows.filter { $0.kind != .now && $0.t0 <= minute && minute < max($0.t1 ?? $0.t0, $0.t0 + 0.001) }
        return (covering.first { $0.kind == .pause } ?? covering.first { $0.kind == .trip } ?? covering.first)?.id
    }

    /// The line under the stretch's name while the bar is held: the time, where he was, how fast and his heart; of these only the ones the footprint knows. When it knows none
    /// of the three, what else was going on (the timeline's own line).
    @MainActor static func heldLine(_ m: FootprintModel, minute: Double, elsewhere: String) -> String {
        let ms = m.clock.ms(minute: minute)
        guard ms <= min(m.reply.now_ms, m.reply.end_ms) else { return elsewhere }
        var parts: [String] = []
        if let s = m.stay(atMs: ms), let n = name(s) { parts.append(n) }
        if let v = m.speedMps(atMs: ms) { parts.append(speedText(v)) }
        if let b = m.heartBpm(atMs: ms) { parts.append(Words.Footprint.held(bpm: Int(b.rounded()))) }
        return parts.isEmpty ? elsewhere : ([clock(minute)] + parts).joined(separator: " \u{00B7} ")
    }

    /// The readout of the minute under a finger: on an outing (a stop of it too) its first line is the whole outing, `练车 · 13:07–14:17 · 1 时 10 分`, whichever stretch of the bar the finger is
    /// on; in a long stay, or where he was nowhere, it is the timeline's own.
    @MainActor static func heldReadout(_ m: FootprintModel, minute: Double, _ r: TLReadout) -> TLReadout {
        guard let o = m.outing(atMs: m.clock.ms(minute: minute)) else { return r }
        let t0 = m.clock.minute(ms: o.start_ms), t1 = m.clock.minute(ms: o.end_ms ?? max(m.reply.now_ms, o.start_ms))
        var out = r
        out.head = [titleText(m.title(o)), clock(t0) + "\u{2013}" + (o.end_ms == nil ? "" : tlEnd(t1)), tlDur(tlInt(t1 - t0))].joined(separator: " \u{00B7} ")
        return out
    }

    /// the scrub dot at a minute: where he was, in the colour of how fast; still is the start of the ramp
    @MainActor static func dot(_ m: FootprintModel, minute: Double) -> FootprintMapDot? {
        let ms = m.clock.ms(minute: minute)
        guard ms <= min(m.reply.now_ms, m.reply.end_ms), let at = m.position(atMs: ms) else { return nil }
        let v = m.speedMps(atMs: ms) ?? 0
        return FootprintMapDot(coord: at, ms: ms, rgb: FootprintPaint.speed(mps: v), label: speedText(v))
    }
}
