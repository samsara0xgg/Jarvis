import CoreGraphics
import CoreLocation
import Foundation

// What the footprint page knows about a moment of the day, from the brain's footprint (Brain/Footprint.swift) and the day beside it (`DayReply`, for the calendar events).
// Plain values only, no screen: where he was at a time, how fast, which stay or outing holds it, and how an outing is named. The words are the views'.

/// How an outing is called, before it is worded: his calendar event it overlaps most, where it went (a place he keeps, else the address), a trip still going, or none of these.
enum FootprintTitle: Equatable {
    case event(String), to(String), onTheWay, outing
}

struct FootprintModel {
    let reply: FootprintReply
    let day: DayReply?
    let clock: TLClock

    init(reply: FootprintReply, day: DayReply?) {
        self.reply = reply
        self.day = day
        clock = TLClock(tz: reply.tz, startMs: reply.start_ms)
    }

    /// The disc of a long stay: its area is the time spent, 29 pt for 13 h 7 min.
    static func discRadius(minutes: Double) -> CGFloat { CGFloat(29 * (max(0, minutes) / 787).squareRoot()) }

    /// the stops no name was given for: where the phone's geocoder is asked for a street
    var unnamedStops: [FootprintStay] { reply.outings.flatMap(\.stops).filter { FootprintDayContent.name($0) == nil } }

    var totalDistanceM: Double { reply.outings.reduce(0) { $0 + $1.distance_m } }
    var topSpeedMps: Double? { reply.outings.compactMap(\.max_speed_mps).max() }

    /// The long stay he is in now: today's open one. Nil on any other day, and when he is out.
    var nowStay: FootprintStay? {
        let now = reply.now_ms
        guard now >= reply.start_ms, now < reply.end_ms else { return nil }
        return reply.stays.first { $0.start_ms <= now && $0.end_ms == nil }
    }

    // MARK: at a time

    /// The long stay or the stop that holds the time (a stay without an end is still going).
    func stay(atMs t: Int64) -> FootprintStay? {
        func holds(_ s: FootprintStay) -> Bool { s.start_ms <= t && t < (s.end_ms ?? .max) }
        return reply.stays.first(where: holds) ?? reply.outings.lazy.flatMap(\.stops).first(where: holds)
    }

    func outing(atMs t: Int64) -> FootprintOuting? {
        reply.outings.first { $0.start_ms <= t && t < ($0.end_ms ?? .max) }
    }

    /// Where he was: the centre of the stay or stop, else between the nearest known positions by time (the outing's points, and the
    /// centres of its stays and stops at their ends), straight across a gap and across the first and last few hundred metres, which have no fixes.
    func position(atMs t: Int64) -> CLLocationCoordinate2D? {
        if let s = stay(atMs: t) { return s.coord }
        guard let o = outing(atMs: t), case let (a, b, k)? = Self.between(anchors(o), t, \.ms) else { return nil }
        return CLLocationCoordinate2D(latitude: lerp(a.coord.latitude, b.coord.latitude, k), longitude: lerp(a.coord.longitude, b.coord.longitude, k))
    }

    /// The outing as the map draws it: its points, each gap from the last fix before it to the first after it, and the joins from the
    /// stay it left to its first fix and from its last fix to the stay it reached (pin to pin when it has no fixes), drawn like a gap.
    func route(_ o: FootprintOuting) -> FootprintMapRoute {
        let pts = o.points.map { FootprintMapPoint(coord: CLLocationCoordinate2D(latitude: $0.lat, longitude: $0.lng), ms: $0.at_ms, speedMps: $0.speed_mps) }
        // a stop inside a gap splits it in two with the same fixes either side: one line
        var gaps: [FootprintMapGap] = []
        for g in o.gaps {
            guard let a = pts.last(where: { $0.ms <= g.start_ms }), let b = pts.first(where: { $0.ms >= g.end_ms }) else { continue }
            let line = FootprintMapGap(from: a.coord, to: b.coord, startMs: a.ms, endMs: b.ms)
            if !gaps.contains(line) { gaps.append(line) }
        }
        let from = longStay(o.from_stay), to = longStay(o.to_stay)
        switch (pts.first, pts.last) {
        case let (first?, last?):
            if let f = from { gaps.insert(FootprintMapGap(from: f.coord, to: first.coord, startMs: o.start_ms, endMs: first.ms), at: 0) }
            if let s = to { gaps.append(FootprintMapGap(from: last.coord, to: s.coord, startMs: last.ms, endMs: o.end_ms ?? last.ms)) }
        default:
            if let f = from, let s = to { gaps.append(FootprintMapGap(from: f.coord, to: s.coord, startMs: o.start_ms, endMs: o.end_ms ?? o.start_ms)) }
        }
        return FootprintMapRoute(id: o.id, points: pts, gaps: gaps)
    }

    private func longStay(_ id: String?) -> FootprintStay? { id.flatMap { id in reply.stays.first { $0.id == id } } }

    /// The known positions of an outing by time: the stay it left at its start, its points and each stop at both ends, the stay it reached at its end
    /// (after the last point, which may be a minute past that stay's start).
    private func anchors(_ o: FootprintOuting) -> [(ms: Int64, coord: CLLocationCoordinate2D)] {
        var mid = o.points.map { (ms: $0.at_ms, coord: CLLocationCoordinate2D(latitude: $0.lat, longitude: $0.lng)) }
        for s in o.stops { mid += [(s.start_ms, s.coord), (s.end_ms ?? s.start_ms, s.coord)] }
        mid.sort { $0.ms < $1.ms }
        return (longStay(o.from_stay).map { [(o.start_ms, $0.coord)] } ?? []) + mid + (longStay(o.to_stay).map { [(o.end_ms ?? $0.start_ms, $0.coord)] } ?? [])
    }

    /// How fast he moved, nil when still (in a stay or a stop), in a gap, or when the brain could not tell.
    func speedMps(atMs t: Int64) -> Double? {
        guard stay(atMs: t) == nil, let o = outing(atMs: t), !o.gaps.contains(where: { $0.start_ms < t && t < $0.end_ms }),
              case let (a, b, k)? = Self.between(o.points, t, \.at_ms) else { return nil }
        switch (a.speed_mps, b.speed_mps) {
        case let (x?, y?): return lerp(x, y, k)
        case let (x?, nil): return x
        case let (nil, y?): return y
        default: return nil
        }
    }

    /// The way he was going, in degrees clockwise from north: from where he was a few seconds before to where he was a few seconds after. Nil in a stay or a stop, and when he moved
    /// less than 4 m in that time, as a bearing from so little is noise.
    func heading(atMs t: Int64) -> Double? {
        guard stay(atMs: t) == nil, let a = position(atMs: t - Self.headingSpanMs), let b = position(atMs: t + Self.headingSpanMs) else { return nil }
        let east = (b.longitude - a.longitude) * 111_320 * cos((a.latitude + b.latitude) / 2 * .pi / 180), north = (b.latitude - a.latitude) * 111_320
        guard hypot(east, north) >= 4 else { return nil }
        return (atan2(east, north) * 180 / .pi + 360).truncatingRemainder(dividingBy: 360)
    }
    private static let headingSpanMs: Int64 = 5_000

    /// What the map's first-person camera takes at a time: where he was, the way he was going and how fast (still when it is not known); nil when he was nowhere.
    func follow(atMs t: Int64) -> FootprintMapFollow? {
        position(atMs: t).map { FootprintMapFollow(coord: $0, heading: heading(atMs: t), speedMps: speedMps(atMs: t) ?? 0) }
    }

    /// His heart rate on an outing at a time, between the watch's readings; nil outside them or when the outing has none.
    func heartBpm(atMs t: Int64) -> Double? {
        guard let s = outing(atMs: t)?.heart_rate?.samples, let lo = s.first?.at_ms, let hi = s.last?.at_ms, lo <= t, t <= hi, case let (a, b, k)? = Self.between(s, t, \.at_ms) else { return nil }
        return lerp(a.bpm, b.bpm, k)
    }

    func title(_ o: FootprintOuting) -> FootprintTitle {
        let end = o.end_ms ?? max(reply.now_ms, o.start_ms)
        func overlap(_ i: DayItem) -> Int64 { min(i.end_ms ?? end, end) - max(i.start_ms, o.start_ms) }
        let events = (day?.items ?? []).filter { $0.kind == "event" && overlap($0) > 0 && !($0.title ?? "").isEmpty }
        if let e = events.max(by: { overlap($0) < overlap($1) }), let t = e.title { return .event(t) }
        if let id = o.to_stay, let s = reply.stays.first(where: { $0.id == id }), let name = s.kept_place?.name ?? s.place { return .to(name) }
        return o.end_ms == nil ? .onTheWay : .outing
    }

    /// The two neighbours of time `t` in a list ordered by time and how far between them it is (0...1); the end one twice before the first and after the last.
    // ponytail: a linear scan, a binary search if a scrub over thousands of points stutters
    private static func between<T>(_ xs: [T], _ t: Int64, _ at: KeyPath<T, Int64>) -> (T, T, Double)? {
        guard let first = xs.first, let last = xs.last else { return nil }
        if t <= first[keyPath: at] { return (first, first, 0) }
        guard let i = xs.firstIndex(where: { $0[keyPath: at] > t }) else { return (last, last, 0) }
        let a = xs[i - 1], b = xs[i]
        return (a, b, Double(t - a[keyPath: at]) / Double(b[keyPath: at] - a[keyPath: at]))
    }
}

extension FootprintOuting {
    /// the fix he was fastest at; nil when no fix has a speed
    var fastest: FootprintPoint? { points.filter { $0.speed_mps != nil }.max { ($0.speed_mps ?? 0) < ($1.speed_mps ?? 0) } }
}

extension FootprintStay {
    var coord: CLLocationCoordinate2D { CLLocationCoordinate2D(latitude: lat, longitude: lng) }
}
