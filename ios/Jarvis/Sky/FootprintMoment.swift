import CoreLocation
import Foundation

// What a finger on the held bar, a finger on an outing's curve or the replay holds: a moment with where the camera goes for it. The finger's moment is read by distance along the line
// (replay brief, sections 1 and 2): the dot glides along what the map draws even where the line takes no time (the join into the next stay), and the readouts use the time that goes with it.

struct FootprintMoment {
    var ms: Int64
    var coord: CLLocationCoordinate2D
    /// degrees clockwise from north; nil while he is still (a bead, a notch), when the camera keeps the one it has
    var heading: Double?
    /// metres, fixed for the whole outing
    var height: Double
    var response = TLMotion.mapResponse
    var outingId: String?
    /// where the playhead stands on the held bar
    var x: Double?
    var beat: FootprintReplay.Beat?
    /// a replay beat in which the camera flies from one place to the next: the coordinate is not on a line then
    var flying = false

    var follow: FootprintMapFollow { FootprintMapFollow(coord: coord, heading: heading, height: height, response: response) }

    /// a frame of the replay
    init(_ f: FootprintReplay.Frame, height: Double) {
        ms = f.ms; coord = f.coord; heading = f.heading; self.height = height; response = TLMotion.replayResponse; outingId = f.outingId; x = f.x; beat = f.beat; flying = f.flying
    }

    init(ms: Int64, coord: CLLocationCoordinate2D, heading: Double?, height: Double, outingId: String?, x: Double?) {
        self.ms = ms; self.coord = coord; self.heading = heading; self.height = height; self.outingId = outingId; self.x = x
    }

    /// Where he was on an outing at a time of it, as a finger on its curves holds it: on the drawn line by the distance that time has reached, facing along the line, and still (no heading) in a stop.
    static func on(_ o: FootprintOuting, track: FootprintTrack, atMs t: Int64) -> FootprintMoment? {
        let d = track.distance(atMs: t)
        guard let c = track.coord(atDistance: d) else { return nil }
        let still = FootprintTrack.holds(o).contains { $0.ms.contains(t) }
        return FootprintMoment(ms: t, coord: c, heading: still ? nil : track.heading(atDistance: d, ahead: FootprintReplay.lookAhead(o)), height: FootprintReplay.scrubHeight(o), outingId: o.id, x: nil)
    }
}

extension FootprintBarLayout {
    var hasSegments: Bool { items.contains { if case .segment = $0.kind { true } else { false } } }

    /// the stretch of the bar at `x` (an item, and in a segment its run or notch): a finger ticks when it goes from one to another
    func stretch(atX x: Double) -> String {
        let it = item(atX: x), at = min(max(x, it.x0), it.x1)
        switch it.kind {
        case let .bead(id): return id
        case let .segment(id): return "\(id)/\(it.pieces.partitionPoint { $0.x0 > at } - 1)"
        }
    }

    /// The moment under a finger at `x` on the held bar: in a segment the place on the line by distance (the heading from the line a little ahead, none in a notch), in a bead the stay's own place;
    /// the height is that of the outing the finger is on, or in a bead the nearer one. Nil when the day has no line.
    func moment(atX x: Double, model: FootprintModel) -> FootprintMoment? {
        guard !items.isEmpty else { return nil }
        let it = item(atX: x), at = min(max(x, it.x0), it.x1), ms = self.ms(atX: at)
        func outing(_ item: Item) -> FootprintOuting? {
            if case let .segment(id) = item.kind { model.reply.outings.first { $0.id == id } } else { nil }
        }
        switch it.kind {
        case let .bead(id):
            let i = items.firstIndex { $0.x0 == it.x0 } ?? 0
            let before = items[..<i].last { outing($0) != nil }, after = items[(i + 1)...].first { outing($0) != nil }
            let near = [before, after].compactMap { $0 }.min { abs($0.x1 - at) + abs($0.x0 - at) < abs($1.x1 - at) + abs($1.x0 - at) }
            guard let coord = model.reply.stays.first(where: { $0.id == id })?.coord ?? model.position(atMs: ms) else { return nil }
            return FootprintMoment(ms: ms, coord: coord, heading: nil, height: near.flatMap(outing).map(FootprintReplay.scrubHeight) ?? 500, outingId: nil, x: at)
        case .segment:
            guard let o = outing(it), let track = it.track, !it.pieces.isEmpty else { return nil }
            let p = it.pieces[max(it.pieces.partitionPoint { $0.x0 > at } - 1, 0)]
            var d: Double, still = false
            switch p.kind {
            case let .run(d0, d1): d = d0 + (d1 - d0) * (p.x1 > p.x0 ? min(max((at - p.x0) / (p.x1 - p.x0), 0), 1) : 0)
            case .notch: d = track.distance(atMs: p.ms0); still = true
            }
            guard let coord = track.coord(atDistance: d) else { return nil }
            return FootprintMoment(ms: ms, coord: coord, heading: still ? nil : track.heading(atDistance: d, ahead: FootprintReplay.lookAhead(o)), height: FootprintReplay.scrubHeight(o), outingId: o.id, x: at)
        }
    }
}
