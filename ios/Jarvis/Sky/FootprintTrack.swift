import CoreLocation
import Foundation

// One outing's drawn line (what `FootprintModel.route(_:)` gives: the join from the stay he left, the fixes, the straight gap stretches, the join to the stay he reached), indexed once
// so that a scrub or a replay finds a place or a time by binary search. Distances are metres along the line; the time map holds the distance still through each stop, so a dot driven by
// time never leaves the line.

extension Array {
    /// The first index whose element satisfies `p` (count when none), for an array where the ones that do come after the ones that do not.
    func partitionPoint(_ p: (Element) -> Bool) -> Int {
        var lo = 0, hi = count
        while lo < hi {
            let m = (lo + hi) / 2
            if p(self[m]) { hi = m } else { lo = m + 1 }
        }
        return lo
    }
}

/// Splits `total` over `lengths` in proportion, none under `floor` (those that would be take the floor, the rest share what remains); the second value is `total` per unit of length for the
/// ones above the floor, nil when there is none. Under `floor` for each, the shares are equal.
func footprintAllot(_ lengths: [Double], total: Double, floor: Double) -> (shares: [Double], perUnit: Double?) {
    let n = Double(lengths.count)
    guard n > 0 else { return ([], nil) }
    var fixed = Set<Int>()
    while true {
        let free = lengths.indices.filter { !fixed.contains($0) }, rest = total - floor * Double(fixed.count), sum = free.reduce(0) { $0 + lengths[$1] }
        guard !free.isEmpty, sum > 0, rest > 0 else { return (Array(repeating: total / n, count: lengths.count), nil) }
        let s = rest / sum, low = free.filter { lengths[$0] * s < floor }
        if low.isEmpty { return (lengths.indices.map { fixed.contains($0) ? floor : lengths[$0] * s }, s) }
        fixed.formUnion(low)
    }
}

struct FootprintTrack {
    private let coords: [CLLocationCoordinate2D]
    private let cum: [Double]                       // metres from the start to each vertex
    private let knots: [(ms: Int64, d: Double)]     // time to distance, both rising; a stop is two knots at one distance
    let startMs: Int64, endMs: Int64

    var length: Double { cum.last ?? 0 }

    init(model: FootprintModel, outing o: FootprintOuting) {
        let r = model.route(o)
        func same(_ a: CLLocationCoordinate2D, _ b: CLLocationCoordinate2D) -> Bool { a.latitude == b.latitude && a.longitude == b.longitude }
        var v: [(c: CLLocationCoordinate2D, ms: Int64)] = []
        if let first = r.points.first, let last = r.points.last {
            if let g = r.gaps.first(where: { same($0.to, first.coord) && !same($0.from, first.coord) }) { v.append((g.from, g.startMs)) }
            v += r.points.map { ($0.coord, $0.ms) }
            if let g = r.gaps.last(where: { same($0.from, last.coord) && !same($0.to, last.coord) }) { v.append((g.to, g.endMs)) }
        } else if let g = r.gaps.first {
            v = [(g.from, g.startMs), (g.to, g.endMs)]
        }
        var at = Int64.min
        v = v.map { at = max(at, $0.ms); return ($0.c, at) }

        coords = v.map(\.c)
        var d = [0.0]
        for i in v.indices.dropFirst() { d.append(d[i - 1] + Self.metres(v[i - 1].c, v[i].c).distance) }
        cum = v.isEmpty ? [] : d

        var knots = zip(v.map(\.ms), cum).map { (ms: $0, d: $1) }
        for h in Self.holds(o) where !knots.isEmpty {
            let held = Self.interpolate(knots, h.ms.lowerBound)
            knots = knots.filter { $0.ms < h.ms.lowerBound || $0.ms > h.ms.upperBound }
            knots += [(h.ms.lowerBound, held), (h.ms.upperBound, held)]
            knots.sort { $0.ms < $1.ms }                  // stable: the stop's own pair keeps its order
        }
        self.knots = knots
        startMs = knots.first?.ms ?? o.start_ms
        endMs = knots.last?.ms ?? o.end_ms ?? o.start_ms
    }

    /// The stops of an outing as the line holds them: numbered from 1 in time, each from its start to its end (a stop still going ends where it begins), none starting before the one before ends.
    static func holds(_ o: FootprintOuting) -> [(stop: FootprintStay, number: Int, ms: ClosedRange<Int64>)] {
        var end = Int64.min
        return o.stops.sorted { $0.start_ms < $1.start_ms }.enumerated().map { n, s in
            let a = max(s.start_ms, end), b = max(s.end_ms ?? s.start_ms, a)
            end = b
            return (s, n + 1, a...b)
        }
    }

    // MARK: lookups

    func coord(atDistance d: Double) -> CLLocationCoordinate2D? {
        guard let first = coords.first else { return nil }
        let d = min(max(d, 0), length), i = cum.partitionPoint { $0 >= d }
        guard i > 0, i < coords.count else { return i == 0 ? first : coords[coords.count - 1] }
        let a = coords[i - 1], b = coords[i], span = cum[i] - cum[i - 1], k = span > 0 ? (d - cum[i - 1]) / span : 1
        return CLLocationCoordinate2D(latitude: a.latitude + (b.latitude - a.latitude) * k, longitude: a.longitude + (b.longitude - a.longitude) * k)
    }

    /// How far along the line he was at a time (held through a stop; before the start and after the end, the ends).
    func distance(atMs t: Int64) -> Double { Self.interpolate(knots, t) }

    /// The time at a distance: the earliest, so a stop's distance is the moment he arrived.
    func ms(atDistance d: Double) -> Int64 {
        guard let lo = knots.first, let hi = knots.last else { return startMs }
        let i = knots.partitionPoint { $0.d >= d }
        guard i > 0, i < knots.count else { return i == 0 ? lo.ms : hi.ms }
        let a = knots[i - 1], b = knots[i], k = (d - a.d) / (b.d - a.d)
        return a.ms + Int64((Double(b.ms - a.ms) * k).rounded())
    }

    /// The way the line goes from a distance: the bearing in degrees clockwise from north to the point `ahead` metres further along it (the end of the line at most). Nil when that is under a metre away.
    func heading(atDistance d: Double, ahead: Double) -> Double? {
        guard let a = coord(atDistance: d), let b = coord(atDistance: d + ahead) else { return nil }
        let m = Self.metres(a, b)
        guard m.distance >= 1 else { return nil }
        return (atan2(m.east, m.north) * 180 / .pi + 360).truncatingRemainder(dividingBy: 360)
    }

    // MARK: pieces

    static func metres(_ a: CLLocationCoordinate2D, _ b: CLLocationCoordinate2D) -> (east: Double, north: Double, distance: Double) {
        let east = (b.longitude - a.longitude) * 111_320 * cos((a.latitude + b.latitude) / 2 * .pi / 180), north = (b.latitude - a.latitude) * 111_320
        return (east, north, hypot(east, north))
    }

    /// the distance at a time between knots ordered by time
    private static func interpolate(_ k: [(ms: Int64, d: Double)], _ t: Int64) -> Double {
        guard let first = k.first, let last = k.last else { return 0 }
        let i = k.partitionPoint { $0.ms > t }
        guard i > 0, i < k.count else { return i == 0 ? first.d : last.d }
        let a = k[i - 1], b = k[i]
        return a.d + (b.d - a.d) * Double(t - a.ms) / Double(b.ms - a.ms)
    }
}
