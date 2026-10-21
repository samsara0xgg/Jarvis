#if DEBUG
import Foundation

/// A made-up footprint for today, as if the brain had served it, and the `/inherent/day` it goes with (same stay ids and times): home overnight, a lesson in a driving school's car
/// (a calendar event, two stops, a no-fix stretch with a stop in it; its fixes begin 500 m out of home and end 400 m short of the school, as the phone wakes GPS late), the school,
/// a walk home, and the evening at home at the timeline fixture's 21:50. Made-up data only: invented places on a plain far from his life, the route and the speeds generated here
/// from fixed numbers, not recorded.
enum FootprintFixture {
    static func day(_ cal: Calendar) -> DayReply { decode(Plan(cal).day) }
    static func footprint(_ cal: Calendar) -> FootprintReply { decode(Plan(cal).footprint) }

    private static func decode<T: Decodable>(_ o: [String: Any]) -> T { try! JSONDecoder().decode(T.self, from: try! JSONSerialization.data(withJSONObject: o)) }

    /// One position a second, in metres east and north of home.
    private typealias Track = [(x: Double, y: Double)]
    /// A fix kept for the reply: second of its track, position, and the mean speed over the 10 s around it
    private struct Fix { var s: Int; var x: Double, y: Double; var v: Double }

    private struct Plan {
        static let origin = (lat: -43.7560, lng: 172.0230)        // a made-up house on the Canterbury plains
        let clock: TLClock
        let date: String
        let drive: Track, walk: Track
        let driveFixes: [Fix], walkFixes: [Fix]
        static let arrivedEarly = 40                                                         // seconds the school stay starts before the drive's last fix
        let driveAt: Int64, walkAt: Int64
        static let driveSeconds = 4200
        static let stopMinutes: [(Double, Double)] = [(9, 15), (29, 34)]                   // of the drive
        static let gapSeconds = (1620, 2220)                                                 // 27 to 37 minutes of the drive, the second stop in it
        /// the gap without the stops in it, as the brain lists it
        static var gapPieces: [(Int, Int)] {
            stopMinutes.reduce([gapSeconds]) { gaps, m in
                gaps.flatMap { [($0.0, min($0.1, Int(m.0 * 60))), (max($0.0, Int(m.1 * 60)), $0.1)] }.filter { $0.0 < $0.1 }
            }
        }

        init(_ cal: Calendar) {
            let day0 = cal.startOfDay(for: Cal.now())
            let clock = TLClock(tz: cal.timeZone.identifier, startMs: Int64(day0.timeIntervalSince1970 * 1000))
            self.clock = clock
            let c = cal.dateComponents([.year, .month, .day], from: day0)
            date = String(format: "%04d-%02d-%02d", c.year!, c.month!, c.day!)
            driveAt = clock.ms(minute: 13 * 60 + 7)
            walkAt = clock.ms(minute: 17 * 60)
            drive = Route.drive()
            walk = Plan.walk(from: drive[Plan.driveSeconds])
            driveFixes = Plan.fixes(drive, within: Plan.reach(drive, out: 500, short: 400), gap: Plan.gapSeconds)
            walkFixes = Plan.fixes(walk, within: 0...walk.count - 1, gap: nil)
        }

        // MARK: the day

        func ms(_ at: Int64, _ s: Int) -> Int64 { at + Int64(s) * 1000 }
        var driveEnd: Int64 { ms(driveAt, driveFixes[driveFixes.count - 1].s - Plan.arrivedEarly) }
        var walkEnd: Int64 { ms(walkAt, walk.count - 1) }
        var homeBack: [String: Any] { stay("stay-6", walkEnd, nil, walk[walk.count - 1], "示例路 12 号", home) }
        let home: [String: Any] = ["id": "p-home", "name": "家", "role": "home"]
        let school: [String: Any] = ["id": "p-school", "name": "驾校", "role": "school"]

        func ll(_ p: (x: Double, y: Double)) -> (lat: Double, lng: Double) {
            (Plan.origin.lat + p.y / 111_320, Plan.origin.lng + p.x / (111_320 * cos(Plan.origin.lat * .pi / 180)))
        }
        func stay(_ id: String, _ from: Int64, _ to: Int64?, _ p: (x: Double, y: Double), _ place: String?, _ kept: [String: Any]?) -> [String: Any] {
            ["id": id, "start_ms": from, "end_ms": to.map { $0 as Any } ?? NSNull(), "lat": ll(p).lat, "lng": ll(p).lng, "place": place.map { $0 as Any } ?? NSNull(), "kept_place": kept.map { $0 as Any } ?? NSNull()]
        }
        /// the two stops of the drive: id, seconds, place
        var stops: [[String: Any]] {
            zip(Plan.stopMinutes, [("stay-2", nil), ("stay-3", "超市")] as [(String, String?)]).map { m, id in
                let a = Int(m.0 * 60), b = Int(m.1 * 60)
                return stay(id.0, ms(driveAt, a), ms(driveAt, b), drive[a], id.1, nil)
            }
        }

        var footprint: [String: Any] {
            let fixes = driveFixes, seconds = fixes[fixes.count - 1].s, beats = Array(stride(from: 0, through: seconds, by: 15))
            let bpm = beats.map { Plan.heart($0) }
            let gaps = Plan.gapPieces.map { ["start_ms": ms(driveAt, $0.0), "end_ms": ms(driveAt, $0.1)] }
            func outing(_ at: Int64, _ f: [Fix], _ track: Track, _ from: String, _ to: String, _ activity: String, _ s: [[String: Any]], _ gaps: [[String: Any]], _ heart: Any, _ moving: Int) -> [String: Any] {
                ["id": "outing-\(at)", "start_ms": at, "end_ms": ms(at, f[f.count - 1].s), "from_stay": from, "to_stay": to, "activity": activity, "distance_m": distance(f, track),
                 "moving_ms": moving, "max_speed_mps": f.map(\.v).max()!, "stops": s, "gaps": gaps, "heart_rate": heart,
                 "points": f.map { [ms(at, $0.s), ll(($0.x, $0.y)).lat, ll(($0.x, $0.y)).lng, $0.v] as [Any] }]
            }
            let heart: [String: Any] = ["avg_bpm": (bpm.reduce(0, +) / Double(bpm.count)).rounded(), "max_bpm": bpm.max()!, "home_avg_bpm": 68.0, "samples": zip(beats, bpm).map { [ms(driveAt, $0), $1] as [Any] }]
            let moving = (seconds - Plan.stopMinutes.reduce(0) { $0 + Int(($1.1 - $1.0) * 60) }) * 1000
            return [
                "date": date, "tz": clock.cal.timeZone.identifier, "start_ms": clock.ms(minute: 0), "end_ms": clock.ms(minute: 1440), "now_ms": clock.ms(minute: TimelineFixture.now),
                "stays": [stay("stay-1", clock.ms(minute: -180), driveAt, drive[0], "示例路 12 号", home), stay("stay-5", driveEnd, walkAt, drive[Plan.driveSeconds], "驾校路 3 号", school), homeBack],
                "outings": [outing(driveAt, fixes, drive, "stay-1", "stay-5", "automotive", stops, gaps, heart, moving),
                            outing(walkAt, walkFixes, walk, "stay-5", "stay-6", "walking", [], [], NSNull(), (walk.count - 1) * 1000)],
            ]
        }

        /// the same day as `/inherent/day` serves it: the stays, his lesson on the calendar, and the moves between them
        var day: [String: Any] {
            func item(_ id: String, _ kind: String, _ from: Int64, _ to: Int64?, _ more: [String: Any] = [:]) -> [String: Any] {
                let o: [String: Any] = ["id": id, "lane": "you", "kind": kind, "state": to == nil ? "now" : "done", "start_ms": from, "end_ms": to.map { $0 as Any } ?? NSNull()]
                return o.merging(more) { $1 }
            }
            func place(_ s: [String: Any]) -> [String: Any] { ["place": s["place"] as Any, "kept_place": s["kept_place"] as Any, "lat": s["lat"] as Any, "lng": s["lng"] as Any] }
            func stayItem(_ s: [String: Any]) -> [String: Any] { item(s["id"] as! String, "stay", s["start_ms"] as! Int64, s["end_ms"] as? Int64, place(s)) }
            let long = (footprint["stays"] as! [[String: Any]]), mid = stops
            var items = [stayItem(long[0])]
            var from = driveAt
            for (i, s) in mid.enumerated() {
                items.append(item("move-\(i + 1)", "move", from, s["start_ms"] as? Int64, ["activity": "automotive"])); items.append(stayItem(s)); from = s["end_ms"] as! Int64
            }
            items.append(item("move-4", "move", from, driveEnd, ["activity": "automotive"]))
            items.append(stayItem(long[1]))
            items.append(item("move-5", "move", walkAt, walkEnd, ["activity": "walking"]))
            items.append(stayItem(long[2]))
            items.append(item("event-1", "event", clock.ms(minute: 13 * 60), clock.ms(minute: 14 * 60 + 30), ["title": "练车"]))
            return ["date": date, "tz": clock.cal.timeZone.identifier, "start_ms": clock.ms(minute: 0), "end_ms": clock.ms(minute: 1440), "now_ms": clock.ms(minute: TimelineFixture.now), "items": items, "missing": [String]()]
        }

        // MARK: the numbers

        /// the length of the line from where the track begins through the fixes (straight across a gap) to where it ends
        func distance(_ f: [Fix], _ track: Track) -> Double {
            let line = [track[0]] + f.map { (x: $0.x, y: $0.y) } + [track[track.count - 1]]
            return zip(line, line.dropFirst()).reduce(0.0) { $0 + hypot($1.1.x - $1.0.x, $1.1.y - $1.0.y) }
        }

        /// the seconds of a track from the first it is `out` m from its start to the last it is `short` m from its end
        static func reach(_ t: Track, out: Double, short: Double) -> ClosedRange<Int> {
            let a = t.firstIndex { hypot($0.x - t[0].x, $0.y - t[0].y) >= out }!, b = t.lastIndex { hypot($0.x - t[t.count - 1].x, $0.y - t[t.count - 1].y) >= short }!
            return a...b
        }

        /// 88 beats a minute on average, rising and falling with the road
        static func heart(_ s: Int) -> Double { (88 + 8 * sin(Double(s) / 175) + 4 * sin(Double(s) / 41 + 1)).rounded() }

        /// a walk of a little over a kilometre back home, one bend in it, at about 1.3 m/s with a stride that is not even
        static func walk(from start: (x: Double, y: Double)) -> Track {
            let bend = (x: start.x / 2 + 70, y: start.y / 2 + 70), a = hypot(bend.x - start.x, bend.y - start.y), b = hypot(bend.x, bend.y), total = a + b
            let seconds = Int((total / 1.3).rounded(.up))
            return (0...seconds).map { t in
                let u = Double(t) / Double(seconds), d = total * (u + 0.015 * sin(6 * .pi * u))
                return d <= a ? (start.x + (bend.x - start.x) * d / a, start.y + (bend.y - start.y) * d / a) : (bend.x * (total - d) / b, bend.y * (total - d) / b)
            }
        }

        /// the fixes of a track within some seconds of it, every 10 s, the inside of the gap left out, each side thinned to the points the line needs within 5 m
        static func fixes(_ pos: Track, within r: ClosedRange<Int>, gap: (Int, Int)?) -> [Fix] {
            let n = pos.count - 1
            func speed(_ s: Int) -> Double { let a = max(0, s - 5), b = min(n, s + 5); return hypot(pos[b].x - pos[a].x, pos[b].y - pos[a].y) / Double(b - a) }
            var raw = stride(from: (r.lowerBound + 9) / 10 * 10, through: r.upperBound, by: 10).map { Fix(s: $0, x: pos[$0].x, y: pos[$0].y, v: speed($0)) }
            if raw.last!.s != r.upperBound { raw.append(Fix(s: r.upperBound, x: pos[r.upperBound].x, y: pos[r.upperBound].y, v: speed(r.upperBound))) }
            let sides = gap.map { g in [raw.filter { $0.s <= g.0 }, raw.filter { $0.s >= g.1 }] } ?? [raw]
            return sides.flatMap { f -> [Fix] in
                var out = [f[0]], a = 0, j = 2
                while j < f.count {
                    let near = (a + 1..<j).allSatisfy { k in
                        let u = Double(f[k].s - f[a].s) / Double(f[j].s - f[a].s)
                        return hypot(f[k].x - (f[a].x + (f[j].x - f[a].x) * u), f[k].y - (f[a].y + (f[j].y - f[a].y) * u)) <= 5
                    }
                    if !near { out.append(f[j - 1]); a = j - 1 }
                    j += 1
                }
                return out + [f[f.count - 1]]
            }
        }
    }

    /// The lesson's drive: 70 minutes of speeds eased between fixed marks, standing still for the two stops, along a looping line stretched to the distance those speeds cover.
    private enum Route {
        /// (minute of the drive, km/h)
        static let keys: [(Double, Double)] = [
            (0, 0), (1.5, 12), (4, 17), (7, 15), (8.6, 6), (9, 0), (15, 0), (16.5, 12), (19, 21), (23, 23), (27, 18), (28.6, 7), (29, 0),
            (34, 0), (35.5, 19), (37, 33), (39, 46), (41.5, 66), (42.7, 80.5), (44, 62), (46, 36), (48, 17), (50, 16),
            (53, 19), (57, 21), (61, 15), (64, 17), (67, 11), (69, 6), (70, 0),
        ]
        /// the line's corners in km from home; it ends by the driving school, about 0.9 km from where it began
        static let corners: [(Double, Double)] = [(0, 0), (0, 1.6), (2.2, 2.4), (2.6, 4.6), (5.4, 5.2), (7.4, 3.2), (8.6, 6.0), (11.6, 6.6), (12.4, 3.6), (10.4, 0.8), (7.0, 0.4), (3.8, -1.2), (0.9, -0.9)]

        static func kmh(_ minute: Double) -> Double {
            guard let i = keys.firstIndex(where: { $0.0 >= minute }), i > 0 else { return 0 }
            let a = keys[i - 1], b = keys[i], k = (minute - a.0) / (b.0 - a.0), v = a.1 + (b.1 - a.1) * k * k * (3 - 2 * k)
            return max(0, v + 1.6 * sin(minute * 2.9) * sin(minute * 0.7 + 1) * min(1, v / 12))      // a little unevenness, none when standing
        }

        static func drive() -> Track {
            var along = [0.0]
            for s in 1...Plan.driveSeconds { along.append(along[s - 1] + (kmh(Double(s - 1) / 60) + kmh(Double(s) / 60)) / 2 / 3.6) }
            var p = corners, len = [0.0]
            for _ in 0..<3 {                // round the corners
                var q = [p[0]]
                for i in 0..<p.count - 1 {
                    q.append((0.75 * p[i].0 + 0.25 * p[i + 1].0, 0.75 * p[i].1 + 0.25 * p[i + 1].1))
                    q.append((0.25 * p[i].0 + 0.75 * p[i + 1].0, 0.25 * p[i].1 + 0.75 * p[i + 1].1))
                }
                p = q + [p[p.count - 1]]
            }
            for i in 1..<p.count { len.append(len[i - 1] + hypot(p[i].0 - p[i - 1].0, p[i].1 - p[i - 1].1)) }
            let c = along[Plan.driveSeconds] / len[len.count - 1], total = along[Plan.driveSeconds]
            var j = 1
            return along.map { a in
                let d = min(a / c, total / c)
                while j < p.count - 1 && len[j] < d { j += 1 }
                let f = (d - len[j - 1]) / (len[j] - len[j - 1])
                return ((p[j - 1].0 + (p[j].0 - p[j - 1].0) * f) * c, (p[j - 1].1 + (p[j].1 - p[j - 1].1) * f) * c)
            }
        }
    }
}
#endif
