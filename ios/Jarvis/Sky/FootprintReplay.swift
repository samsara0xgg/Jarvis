import CoreLocation
import Foundation

// The footprint's ▶ replay (brief, section 3) as a schedule: a pure function from seconds to the moment, the point on the drawn line, the way the camera faces and the beat showing.
// Pacing is by distance, so the ground goes by at one pace; every long stay and every stop is a beat; one camera height serves the whole replay.

struct FootprintReplay {
    enum Scope: Equatable { case day, outing(String) }
    /// what a beat says: a long stay with its length on the day, or a numbered stop with its minutes
    enum Beat: Equatable { case stay(id: String, minutes: Int), stop(id: String, number: Int, minutes: Int) }

    struct Frame {
        var ms: Int64
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
    private struct Slot { var t0: Double, t1: Double; var outing: String?; var part: Part }
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
        let layout = FootprintBarLayout(model: model), tracks = model.tracks, r = model.reply, dayEnd = min(r.end_ms, r.now_ms)
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
        func add(_ seconds: Double, _ outing: String?, _ part: Part) {
            guard seconds > 0 else { return }
            slots.append(Slot(t0: t, t1: t + seconds, outing: outing, part: part)); t += seconds
        }
        for it in items {
            switch it.kind {
            case let .bead(id):
                let prev = segments.prefix(seen).last?.run, next = segments.dropFirst(seen).first?.run
                let from = prev.flatMap { $0.track.coord(atDistance: $0.track.length) } ?? next?.track.coord(atDistance: 0)
                let to = next.flatMap { $0.track.coord(atDistance: 0) } ?? from
                guard let from, let to, let stay = r.stays.first(where: { $0.id == id }) else { continue }
                let fly = prev != nil && metres(from, to) > Self.flyMetres
                let minutes = max(0, Int((Double(min(stay.end_ms ?? dayEnd, dayEnd) - max(stay.start_ms, r.start_ms)) / 60_000).rounded()))
                add(Self.beatSeconds, nil, .beat(.stay(id: id, minutes: minutes), ms: it.ms0...it.ms1, from: from, to: to, heading: (fly || prev == nil ? next?.start : prev?.end) ?? 0, fly: fly))
            case let .segment(id):
                guard let run = runs[id] else { continue }
                let share = moving.shares[seen], length = run.track.length, runCount = it.pieces.filter { if case .run = $0.kind { true } else { false } }.count
                for p in it.pieces {
                    switch p.kind {
                    case let .run(d0, d1):
                        add(length > 0 ? share * (d1 - d0) / length : share / Double(runCount), id, .move(track: run.track, d0: d0, d1: d1, ms: p.ms0...p.ms1, ahead: run.ahead, hold: run.end))
                    case let .notch(stop, number):
                        let d = run.track.distance(atMs: p.ms0), c = run.track.coord(atDistance: d) ?? CLLocationCoordinate2D()
                        add(Self.beatSeconds, id, .beat(.stop(id: stop, number: number, minutes: Int((Double(p.ms1 - p.ms0) / 60_000).rounded())), ms: p.ms0...p.ms1, from: c, to: c, heading: run.track.heading(atDistance: d, ahead: run.ahead) ?? run.end, fly: false))
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
            return Frame(ms: min(max(track.ms(atDistance: d), ms.lowerBound), ms.upperBound), coord: track.coord(atDistance: d) ?? CLLocationCoordinate2D(), heading: track.heading(atDistance: d, ahead: ahead) ?? hold,
                         beat: nil, outingId: s.outing, flying: false)
        case let .beat(beat, ms, from, to, heading, fly):
            let e = u * u * (3 - 2 * u)         // eased at both ends
            let c = fly ? CLLocationCoordinate2D(latitude: from.latitude + (to.latitude - from.latitude) * e, longitude: from.longitude + (to.longitude - from.longitude) * e) : from
            return Frame(ms: ms.lowerBound + Int64((Double(ms.upperBound - ms.lowerBound) * u).rounded()), coord: c, heading: heading, beat: beat, outingId: s.outing, flying: fly)
        }
    }
}
