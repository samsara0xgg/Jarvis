import CoreLocation
import Foundation

// The footprint's ▶ replay (brief, section 3) as a schedule: a pure function from seconds to the moment, the point on the drawn line, the way the camera faces and the beat showing.
// Pacing is by distance, so the ground goes by at one pace; every long stay and every stop is a beat; one camera height serves the whole replay.

struct FootprintReplay {
    enum Scope: Hashable { case day, outing(String) }
    /// what a beat says: a long stay with its whole length, or a numbered stop with its minutes
    enum Beat: Equatable { case stay(id: String, minutes: Int), stop(id: String, number: Int, minutes: Int) }

    struct Frame {
        var ms: Int64
        /// where the playhead stands on the held bar (the trip layout)
        var x: Double
        var coord: CLLocationCoordinate2D
        /// degrees clockwise from north; kept while he is still
        var heading: Double
        var beat: Beat?
        var outingId: String?
        /// a beat in which the camera flies from one place to the next: the coordinate is not on a line then
        var flying: Bool
    }

    static let beatSeconds = 1.0, minOutingSeconds = 3.0, flyMetres = 20.0
    static let daySeconds = 30.0, outingSeconds = 18.0
    /// the ground goes by at most half a screen height a second: height = ground speed / 0.5, within these
    static let heights = 400.0...3000.0, screensPerSecond = 0.5

    let scope: Scope
    /// seconds
    let duration: Double
    /// metres, one value for the whole replay
    let height: Double

    private enum Part {
        case move(track: FootprintTrack, d0: Double, d1: Double, ms: ClosedRange<Int64>, ahead: Double, hold: Double)
        case beat(Beat, ms: ClosedRange<Int64>, from: CLLocationCoordinate2D, to: CLLocationCoordinate2D, heading: Double, fly: Bool)
    }
    /// a stretch of the replay with where it lies on the trip bar and in time, so that a finger's x or a curve's time can be turned back into a second
    private struct Slot { var t0: Double, t1: Double; var x0: Double, x1: Double; var ms: ClosedRange<Int64>; var outing: String?; var part: Part }
    private let slots: [Slot]

    // MARK: per activity

    /// the camera's fixed height over an outing while a finger scrubs it
    static func scrubHeight(_ o: FootprintOuting) -> Double {
        switch o.activity { case "walking", "running", nil: 500; case "cycling": 800; default: 1200 }
    }
    /// how far ahead along the line the camera looks
    static func lookAhead(_ o: FootprintOuting) -> Double { o.activity == "walking" || o.activity == "running" || o.activity == nil ? 30 : 120 }

    // MARK: schedule

    /// Nil when nothing in the scope has a line to follow.
    init?(model: FootprintModel, scope: Scope) {
        self.scope = scope
        let layout = model.barLayout, tracks = model.tracks, r = model.reply
        func line(_ id: String) -> FootprintTrack? { tracks[id].flatMap { $0.coord(atDistance: 0) == nil ? nil : $0 } }
        let items = layout.items.filter {
            switch ($0.kind, scope) {
            case let (.segment(id), .day): line(id) != nil
            case let (.segment(id), .outing(want)): id == want && line(id) != nil
            case (.bead, .day): true
            case (.bead, .outing): false
            }
        }
        // each outing: its line, how far ahead it looks, and the way it faces at its start and at its end
        struct Run { var id: String, track: FootprintTrack, ahead: Double, start: Double, end: Double }
        var runs: [String: Run] = [:], last: Double?
        for it in items { if case let .segment(id) = it.kind, let o = r.outings.first(where: { $0.id == id }), let t = line(id) {
            let ahead = Self.lookAhead(o), start = t.heading(atDistance: 0, ahead: ahead) ?? last ?? 0, end = t.heading(atDistance: max(0, t.length - 1.01), ahead: ahead) ?? start
            runs[id] = Run(id: id, track: t, ahead: ahead, start: start, end: end); last = end
        } }
        guard !runs.isEmpty else { return nil }
        let segments = items.compactMap { it -> (item: FootprintBarLayout.Item, run: Run)? in
            if case let .segment(id) = it.kind, let run = runs[id] { return (it, run) } else { return nil }
        }

        // the time: beats a second each, the rest of the whole by the length of each line with a floor, the ground never faster than the camera can show
        let beats = items.reduce(0) { n, it in
            if case .bead = it.kind { n + 1 } else { n + it.notches.count }
        }
        let day = scope == .day, lengths = segments.map(\.run.track.length)
        var moving = footprintAllot(lengths, total: max((day ? Self.daySeconds : Self.outingSeconds) - Double(beats) * Self.beatSeconds, day ? 15 : 10, Self.minOutingSeconds * Double(lengths.count)), floor: Self.minOutingSeconds)
        var speed = moving.perUnit.map { 1 / $0 } ?? lengths.reduce(0, +) / max(moving.shares.reduce(0, +), .leastNonzeroMagnitude)
        let fastest = Self.heights.upperBound * Self.screensPerSecond
        if speed > fastest {
            moving = (lengths.map { max($0 / fastest, Self.minOutingSeconds) }, 1 / fastest)
            speed = fastest
        }
        height = min(max(speed / Self.screensPerSecond, Self.heights.lowerBound), Self.heights.upperBound)

        // the slots
        func metres(_ a: CLLocationCoordinate2D, _ b: CLLocationCoordinate2D) -> Double { FootprintTrack.metres(a, b).distance }
        var slots: [Slot] = [], t = 0.0, seen = 0
        func add(_ seconds: Double, _ outing: String?, x: ClosedRange<Double>, ms: ClosedRange<Int64>, _ part: Part) {
            guard seconds > 0 else { return }
            slots.append(Slot(t0: t, t1: t + seconds, x0: x.lowerBound, x1: x.upperBound, ms: ms, outing: outing, part: part)); t += seconds
        }
        for it in items {
            switch it.kind {
            case let .bead(id):
                let prev = segments.prefix(seen).last?.run, next = segments.dropFirst(seen).first?.run
                let from = prev.flatMap { $0.track.coord(atDistance: $0.track.length) } ?? next?.track.coord(atDistance: 0)
                let to = next.flatMap { $0.track.coord(atDistance: 0) } ?? from
                guard let from, let to, let stay = r.stays.first(where: { $0.id == id }) else { continue }
                let fly = prev != nil && metres(from, to) > Self.flyMetres
                add(Self.beatSeconds, nil, x: it.x0...it.x1, ms: it.ms0...it.ms1, .beat(.stay(id: id, minutes: stay.minutes(now: r.now_ms)), ms: it.ms0...it.ms1, from: from, to: to, heading: (fly || prev == nil ? next?.start : prev?.end) ?? 0, fly: fly))
            case let .segment(id):
                guard let run = runs[id] else { continue }
                let share = moving.shares[seen], length = run.track.length, runCount = it.pieces.filter { if case .run = $0.kind { true } else { false } }.count
                for p in it.pieces {
                    switch p.kind {
                    case let .run(d0, d1):
                        add(length > 0 ? share * (d1 - d0) / length : share / Double(runCount), id, x: p.x0...p.x1, ms: p.ms0...p.ms1, .move(track: run.track, d0: d0, d1: d1, ms: p.ms0...p.ms1, ahead: run.ahead, hold: run.end))
                    case let .notch(stop, number):
                        let d = run.track.distance(atMs: p.ms0), c = run.track.coord(atDistance: d) ?? CLLocationCoordinate2D()
                        add(Self.beatSeconds, id, x: p.x0...p.x1, ms: p.ms0...p.ms1, .beat(.stop(id: stop, number: number, minutes: Int((Double(p.ms1 - p.ms0) / 60_000).rounded())), ms: p.ms0...p.ms1, from: c, to: c, heading: run.track.heading(atDistance: d, ahead: run.ahead) ?? run.end, fly: false))
                    }
                }
                seen += 1
            }
        }
        guard !slots.isEmpty else { return nil }
        self.slots = slots
        duration = t
    }

    // MARK: frames

    func frame(at seconds: Double) -> Frame {
        let t = min(max(seconds, 0), duration), s = slots[max(slots.partitionPoint { $0.t0 > t } - 1, 0)]
        let u = s.t1 > s.t0 ? min(max((t - s.t0) / (s.t1 - s.t0), 0), 1) : 1
        switch s.part {
        case let .move(track, d0, d1, ms, ahead, hold):
            let d = d0 + (d1 - d0) * u
            return Frame(ms: min(max(track.ms(atDistance: d), ms.lowerBound), ms.upperBound), x: s.x0 + (s.x1 - s.x0) * u, coord: track.coord(atDistance: d) ?? CLLocationCoordinate2D(), heading: track.heading(atDistance: d, ahead: ahead) ?? hold,
                         beat: nil, outingId: s.outing, flying: false)
        case let .beat(beat, ms, from, to, heading, fly):
            let e = u * u * (3 - 2 * u)         // eased at both ends
            let c = fly ? CLLocationCoordinate2D(latitude: from.latitude + (to.latitude - from.latitude) * e, longitude: from.longitude + (to.longitude - from.longitude) * e) : from
            return Frame(ms: ms.lowerBound + Int64((Double(ms.upperBound - ms.lowerBound) * u).rounded()), x: s.x0 + (s.x1 - s.x0) * u, coord: c, heading: heading, beat: beat, outingId: s.outing, flying: fly)
        }
    }

    // MARK: and back

    /// The second at which the playhead stands at `x` on the trip bar (the inverse of `Frame.x`); over the spacing between two items the start of the next one.
    func seconds(atX x: Double) -> Double {
        let s = slots[min(slots.partitionPoint { $0.x1 >= x }, slots.count - 1)]
        return s.t0 + (s.x1 > s.x0 ? min(max((x - s.x0) / (s.x1 - s.x0), 0), 1) : 0) * (s.t1 - s.t0)
    }

    /// The second at which the replay is at a time of the day, for a finger on the outing's curves: along a line by distance, so that it agrees with `frame(at:)`.
    func seconds(atMs t: Int64) -> Double {
        let s = slots[min(slots.partitionPoint { $0.ms.upperBound >= t }, slots.count - 1)]
        var u = s.ms.upperBound > s.ms.lowerBound ? Double(t - s.ms.lowerBound) / Double(s.ms.upperBound - s.ms.lowerBound) : 0
        if case let .move(track, d0, d1, _, _, _) = s.part, d1 > d0 { u = (track.distance(atMs: t) - d0) / (d1 - d0) }
        return s.t0 + min(max(u, 0), 1) * (s.t1 - s.t0)
    }
}
