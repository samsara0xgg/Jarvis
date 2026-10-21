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

    /// Where he was: the centre of the stay or stop, else along the outing's points by time (straight across a gap, which has none).
    func position(atMs t: Int64) -> CLLocationCoordinate2D? {
        if let s = stay(atMs: t) { return CLLocationCoordinate2D(latitude: s.lat, longitude: s.lng) }
        guard let o = outing(atMs: t), case let (a, b, k)? = Self.between(o.points, t, \.at_ms) else { return nil }
        return CLLocationCoordinate2D(latitude: lerp(a.lat, b.lat, k), longitude: lerp(a.lng, b.lng, k))
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
