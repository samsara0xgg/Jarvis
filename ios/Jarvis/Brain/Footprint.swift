import Foundation

// The phone's side of `GET /inherent/footprint?date=YYYY-MM-DD` (ADR 0323, docs/spec.html): the day's long stays and the outings between them, times in UTC epoch milliseconds.
// Read the way `DayReply` is: one element the phone cannot read costs that element, a field it does not know is nil, a missing required field is a reply that cannot be read.

private extension KeyedDecodingContainer {
    /// the value when it can be read, else nil: missing, null or another shape alike
    func soft<T: Decodable>(_ k: Key) -> T? { try? decodeIfPresent(T.self, forKey: k) }
    /// a list read element by element; empty when it is missing or not a list
    func list<T: Decodable>(_ k: Key) -> [T] { ((try? decodeIfPresent([Lossy<T>].self, forKey: k)) ?? nil)?.compactMap(\.value) ?? [] }
}

struct FootprintKeptPlace: Decodable, Equatable, Identifiable {
    let id: String, name: String
    let role: String?           // home, school or other
}

/// A stay: one of the day's long ones, or a stop inside an outing. `end_ms` nil is still there.
struct FootprintStay: Decodable, Equatable, Identifiable {
    let id: String
    let start_ms: Int64
    let end_ms: Int64?
    let lat: Double, lng: Double
    let place: String?
    let kept_place: FootprintKeptPlace?
}
extension FootprintStay {
    init(from d: Decoder) throws {
        let c = try d.container(keyedBy: K.self)
        id = try c.decode(String.self, forKey: .id)
        start_ms = try c.decode(Int64.self, forKey: .start_ms)
        end_ms = try c.decodeIfPresent(Int64.self, forKey: .end_ms)             // present and not a time costs the stay
        lat = try c.decode(Double.self, forKey: .lat)
        lng = try c.decode(Double.self, forKey: .lng)
        place = c.soft(.place)
        kept_place = c.soft(.kept_place)
    }
    private enum K: String, CodingKey { case id, start_ms, end_ms, lat, lng, place, kept_place }
}

/// No fixes between two moments: drawn dotted and straight.
struct FootprintGap: Decodable, Equatable {
    let start_ms: Int64, end_ms: Int64
}

struct FootprintBeat: Decodable, Equatable {
    let at_ms: Int64
    let bpm: Double
    init(at_ms: Int64, bpm: Double) { self.at_ms = at_ms; self.bpm = bpm }
    /// `[at_ms, bpm]`
    init(from d: Decoder) throws {
        var c = try d.unkeyedContainer()
        at_ms = try c.decode(Int64.self)
        bpm = try c.decode(Double.self)
    }
}

struct FootprintHeart: Decodable, Equatable {
    let avg_bpm: Double, max_bpm: Double
    let home_avg_bpm: Double?
    let samples: [FootprintBeat]
}
extension FootprintHeart {
    init(from d: Decoder) throws {
        let c = try d.container(keyedBy: K.self)
        avg_bpm = try c.decode(Double.self, forKey: .avg_bpm)
        max_bpm = try c.decode(Double.self, forKey: .max_bpm)
        home_avg_bpm = c.soft(.home_avg_bpm)
        samples = c.list(.samples)
    }
    private enum K: String, CodingKey { case avg_bpm, max_bpm, home_avg_bpm, samples }
}

struct FootprintPoint: Decodable, Equatable {
    let at_ms: Int64
    let lat: Double, lng: Double
    let speed_mps: Double?
    init(at_ms: Int64, lat: Double, lng: Double, speed_mps: Double?) { self.at_ms = at_ms; self.lat = lat; self.lng = lng; self.speed_mps = speed_mps }
    /// `[at_ms, lat, lng, speed_mps]`, the speed null when the brain could not work it out
    init(from d: Decoder) throws {
        var c = try d.unkeyedContainer()
        at_ms = try c.decode(Int64.self)
        lat = try c.decode(Double.self)
        lng = try c.decode(Double.self)
        speed_mps = try c.isAtEnd || c.decodeNil() ? nil : try? c.decode(Double.self)
    }
}

struct FootprintOuting: Decodable, Equatable, Identifiable {
    let id: String                  // `outing-<start_ms>`
    let start_ms: Int64
    let end_ms: Int64?              // nil: still out
    let from_stay: String?, to_stay: String?
    let activity: String?           // walking, running, cycling, automotive
    let distance_m: Double
    let moving_ms: Int64
    let max_speed_mps: Double?
    let stops: [FootprintStay]
    let gaps: [FootprintGap]
    let heart_rate: FootprintHeart?
    let points: [FootprintPoint]
}
extension FootprintOuting {
    init(from d: Decoder) throws {
        let c = try d.container(keyedBy: K.self)
        id = try c.decode(String.self, forKey: .id)
        start_ms = try c.decode(Int64.self, forKey: .start_ms)
        end_ms = try c.decodeIfPresent(Int64.self, forKey: .end_ms)
        distance_m = try c.decode(Double.self, forKey: .distance_m)
        moving_ms = try c.decode(Int64.self, forKey: .moving_ms)
        from_stay = c.soft(.from_stay)
        to_stay = c.soft(.to_stay)
        activity = c.soft(.activity)
        max_speed_mps = c.soft(.max_speed_mps)
        stops = c.list(.stops)
        gaps = c.list(.gaps)
        heart_rate = c.soft(.heart_rate)
        points = c.list(.points)
    }
    private enum K: String, CodingKey { case id, start_ms, end_ms, from_stay, to_stay, activity, distance_m, moving_ms, max_speed_mps, stops, gaps, heart_rate, points }
}

struct FootprintReply: Decodable, Equatable {
    let date: String
    let tz: String
    let start_ms: Int64, end_ms: Int64, now_ms: Int64
    let stays: [FootprintStay]
    let outings: [FootprintOuting]
}
extension FootprintReply {
    init(from d: Decoder) throws {
        let c = try d.container(keyedBy: K.self)
        date = try c.decode(String.self, forKey: .date)
        tz = try c.decode(String.self, forKey: .tz)
        start_ms = try c.decode(Int64.self, forKey: .start_ms)
        end_ms = try c.decode(Int64.self, forKey: .end_ms)
        now_ms = try c.decode(Int64.self, forKey: .now_ms)
        stays = try c.decode([Lossy<FootprintStay>].self, forKey: .stays).compactMap(\.value)
        outings = try c.decode([Lossy<FootprintOuting>].self, forKey: .outings).compactMap(\.value)
    }
    private enum K: String, CodingKey { case date, tz, start_ms, end_ms, now_ms, stays, outings }
}

extension BrainClient {
    /// `GET /inherent/footprint?date=YYYY-MM-DD`, under this phone's token. The reply is nil when this brain has no such route (an older one).
    static func footprint(_ date: String, bases: [URL], token: String) async throws -> (reply: FootprintReply?, base: URL, ms: Int) {
        let r = try await send("GET", "/inherent/footprint", query: [URLQueryItem(name: "date", value: date)], also: [404], bases: bases, token: token, unauthorized: .revoked)
        if r.status == 404 {
            if BrainRoute.missing(r.data) { return (nil, r.base, r.ms) }
            throw BrainError.refused                                              // the route's own 404
        }
        guard let reply = try? JSONDecoder().decode(FootprintReply.self, from: r.data) else { throw BrainError.garbled }
        return (reply, r.base, r.ms)
    }
}
