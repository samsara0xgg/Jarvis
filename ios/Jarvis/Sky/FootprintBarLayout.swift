import Foundation

// The held day bar's trip layout (replay brief, section 1): in day order each long stay is a bead, each outing a segment as wide as its drawn line is long (never under a floor), each stop in
// it a notch. Inside a segment x is distance along the line; across a notch it is the stop's minutes, across a bead the stay's time. The bar at rest is the home timeline's and not this.
// Width left once beads, notches and spacing are served goes to the segments; a day so full that the floors cannot all be kept is shared equally and, if still over, squeezed to fit.

struct FootprintBarLayout {
    static let bead = 24.0, notch = 6.0, floor = 28.0, spacing = 4.0

    /// A stretch of a segment: a run of the line (distances in metres along it) or a notch (a stop's minutes). Times are contiguous from piece to piece.
    struct Piece {
        enum Kind { case run(d0: Double, d1: Double), notch(stopId: String, number: Int) }
        let kind: Kind
        let x0: Double, x1: Double
        let ms0: Int64, ms1: Int64
    }

    struct Item {
        enum Kind: Equatable { case bead(stayId: String), segment(outingId: String) }
        let kind: Kind
        let x0: Double, x1: Double
        let ms0: Int64, ms1: Int64
        /// a segment's runs and notches in order; empty for a bead
        let pieces: [Piece]
        let track: FootprintTrack?

        var notches: [Piece] { pieces.filter { if case .notch = $0.kind { true } else { false } } }
    }

    let width: Double
    let items: [Item]

    init(model: FootprintModel, width: Double = TLLayout.barW) {
        self.width = width
        let r = model.reply, tracks = model.tracks, dayEnd = min(r.end_ms, r.now_ms)
        enum Entry { case stay(FootprintStay), outing(FootprintOuting, FootprintTrack) }
        let all = r.stays.map { (ms: $0.start_ms, e: Entry.stay($0)) } + r.outings.compactMap { o in tracks[o.id].map { (ms: o.start_ms, e: Entry.outing(o, $0)) } }
        let entries = all.enumerated().sorted { ($0.element.ms, $0.offset) < ($1.element.ms, $1.offset) }.map(\.element.e)

        // the time each entry covers, end to end: a stay from where the one before ends to where the outing after starts
        var spans: [(Int64, Int64)] = []
        for (i, e) in entries.enumerated() {
            let prev = spans.last?.1
            switch e {
            case let .outing(_, t):
                let lo = max(t.startMs, prev ?? t.startMs)
                spans.append((lo, max(t.endMs, lo)))
            case let .stay(s):
                let lo = prev ?? max(s.start_ms, r.start_ms)
                var hi = min(s.end_ms ?? dayEnd, dayEnd)
                if i + 1 < entries.count, case let .outing(_, t) = entries[i + 1] { hi = t.startMs }
                spans.append((lo, max(hi, lo)))
            }
        }

        // the width: what beads, notches and spacing leave is the segments' to share by line length
        var beads = 0, notches = 0, lengths: [Double] = []
        for e in entries {
            switch e {
            case .stay: beads += 1
            case let .outing(o, t): notches += FootprintTrack.holds(o).count; lengths.append(t.length)
            }
        }
        let spacing = Self.spacing * Double(max(entries.count - 1, 0))
        let rest = max(width - Self.bead * Double(beads) - Self.notch * Double(notches) - spacing, Double(lengths.count))
        var moving = footprintAllot(lengths, total: rest, floor: Self.floor).shares.makeIterator()
        let widths = entries.map { e -> Double in
            switch e {
            case .stay: Self.bead
            case let .outing(o, _): (moving.next() ?? 0) + Self.notch * Double(FootprintTrack.holds(o).count)
            }
        }
        // ponytail: squeezes everything alike when the floors do not fit; a day with that many outings wants a scrolling bar instead
        let k = lengths.isEmpty ? 1 : width / (widths.reduce(0, +) + spacing)

        var x = 0.0, built: [Item] = []
        for (i, e) in entries.enumerated() {
            let x0 = x, x1 = x + widths[i] * k, (lo, hi) = spans[i]
            switch e {
            case let .stay(s):
                built.append(Item(kind: .bead(stayId: s.id), x0: x0, x1: x1, ms0: lo, ms1: hi, pieces: [], track: nil))
            case let .outing(o, t):
                built.append(Item(kind: .segment(outingId: o.id), x0: x0, x1: x1, ms0: lo, ms1: hi, pieces: Self.pieces(o, t, x0: x0, x1: x1, k: k, ms: lo...hi), track: t))
            }
            x = x1 + Self.spacing * k
        }
        items = built
    }

    /// the runs and notches of a segment `x0...x1` wide (notches `notch * k` each) over the time `ms`
    private static func pieces(_ o: FootprintOuting, _ t: FootprintTrack, x0: Double, x1: Double, k: Double, ms: ClosedRange<Int64>) -> [Piece] {
        let holds = FootprintTrack.holds(o), edges = [0] + holds.map { t.distance(atMs: $0.ms.lowerBound) } + [t.length], runs = holds.count + 1
        let notchW = notch * k, free = max(x1 - x0 - notchW * Double(holds.count), 0)
        func at(_ v: Int64) -> Int64 { min(max(v, ms.lowerBound), ms.upperBound) }
        var out: [Piece] = [], x = x0
        for j in 0..<runs {
            let w = (t.length > 0 ? (edges[j + 1] - edges[j]) / t.length : 1 / Double(runs)) * free
            out.append(Piece(kind: .run(d0: edges[j], d1: edges[j + 1]), x0: x, x1: x + w, ms0: j == 0 ? ms.lowerBound : at(holds[j - 1].ms.upperBound), ms1: j == holds.count ? ms.upperBound : at(holds[j].ms.lowerBound)))
            x += w
            if j < holds.count {
                out.append(Piece(kind: .notch(stopId: holds[j].stop.id, number: holds[j].number), x0: x, x1: x + notchW, ms0: at(holds[j].ms.lowerBound), ms1: at(holds[j].ms.upperBound)))
                x += notchW
            }
        }
        return out
    }

    // MARK: where

    /// The item whose x range holds `x`, the nearer one in the spacing between two, the first and last beyond the ends.
    func item(atX x: Double) -> Item {
        let i = max(items.partitionPoint { $0.x0 > x } - 1, 0)
        return i + 1 < items.count && x > items[i].x1 && items[i + 1].x0 - x < x - items[i].x1 ? items[i + 1] : items[i]
    }

    /// The moment at x: along a run by distance on the line (the track's time for it), across a notch by the stop's minutes, across a bead by the stay's time; flat over the spacing.
    func ms(atX x: Double) -> Int64 {
        guard let first = items.first, let last = items.last else { return 0 }
        if x <= first.x0 { return first.ms0 }
        if x >= last.x1 { return last.ms1 }
        let it = items[items.partitionPoint { $0.x0 > x } - 1]
        if x > it.x1 { return it.ms1 }
        guard let track = it.track, !it.pieces.isEmpty else { return Self.lerp(it.ms0, it.ms1, (x - it.x0) / (it.x1 - it.x0)) }
        let p = it.pieces[max(it.pieces.partitionPoint { $0.x0 > x } - 1, 0)], u = p.x1 > p.x0 ? (x - p.x0) / (p.x1 - p.x0) : 0
        guard case let .run(d0, d1) = p.kind else { return Self.lerp(p.ms0, p.ms1, u) }
        return min(max(track.ms(atDistance: d0 + (d1 - d0) * u), p.ms0), p.ms1)
    }

    /// The x of a moment: the inverse of `ms(atX:)` where that is one to one (a halt that is not a stop takes no width, so its minutes all stand at one x).
    func x(atMs t: Int64) -> Double {
        guard let first = items.first, let last = items.last else { return 0 }
        if t <= first.ms0 { return first.x0 }
        if t >= last.ms1 { return last.x1 }
        let it = items[max(items.partitionPoint { $0.ms0 > t } - 1, 0)]
        guard let track = it.track, !it.pieces.isEmpty else { return Self.lerp(it.x0, it.x1, it.ms1 > it.ms0 ? Double(t - it.ms0) / Double(it.ms1 - it.ms0) : 0) }
        let p = it.pieces[max(it.pieces.partitionPoint { $0.ms0 > t } - 1, 0)]
        guard case let .run(d0, d1) = p.kind else { return Self.lerp(p.x0, p.x1, p.ms1 > p.ms0 ? Double(t - p.ms0) / Double(p.ms1 - p.ms0) : 0) }
        return Self.lerp(p.x0, p.x1, d1 > d0 ? (min(max(track.distance(atMs: t), d0), d1) - d0) / (d1 - d0) : 0)
    }

    private static func lerp(_ a: Int64, _ b: Int64, _ u: Double) -> Int64 { a + Int64((Double(b - a) * min(max(u, 0), 1)).rounded()) }
    private static func lerp(_ a: Double, _ b: Double, _ u: Double) -> Double { a + (b - a) * min(max(u, 0), 1) }
}
