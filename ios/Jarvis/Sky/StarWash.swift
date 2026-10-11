import UIKit
import CoreGraphics

// Where he was: each stay is a very faint, narrow band of colour along his lane for its time span, like the haze of the sky, with the place's name set inside his lane.
// The stays (and their colours and names) are built once when the days load (`RealDay.washes`); this only paints them.

extension StarEngine {
    /// how far a wash feathers in and out along the dial, in minutes (at most 0.45 of the stay): 90 minutes is over 20 degrees of the dial, so no end shows as an edge.
    /// A stay that has not ended (he is still there) stays at full strength up to now and is cut at now over only a few minutes: nothing is drawn into the future.
    private static let washFeather = 90.0, washFeatherOpen = 10.0, washSlices = 24.0
    /// the wash's radial profile: a gaussian band on his lane, about 26 pt wide to where it is gone, nothing at both of its ends, as a gradient from the pole outwards (one per colour)
    /// the names go between her lane and his
    private static let washNameR = 118.0
    private static let washHalf = 16.0, washSigma = 6.5, washPeak = 4.6
    private static let washR0 = R_YOU - washHalf, washR1 = R_YOU + washHalf
    /// the band's cross-section as (radius, colour) stops from washR0 to washR1: gaussian on his lane, nothing at both edges (shared by the flat and the near drawing)
    private static func washStops(_ col: RGB) -> [(Double, CGColor)] {
        func gauss(_ r: Double) -> Double { exp(-pow((r - R_YOU) / washSigma, 2) / 2) }
        let floorV = max(gauss(washR0), gauss(washR1))
        return (0...16).map { i in
            let r = washR0 + (washR1 - washR0) * Double(i) / 16
            return (r, col.cg(i == 0 || i == 16 ? 0 : max(0, (gauss(r) - floorV) / (1 - floorV))))
        }
    }
    private static var washGradients: [RGB: CGGradient] = [:], washLinear: [RGB: CGGradient] = [:]
    /// as a gradient from the pole outwards (one per colour)
    private static func washGradient(_ col: RGB) -> CGGradient {
        if let g = washGradients[col] { return g }
        let stops = [(CGFloat(0), col.cg(0))] + washStops(col).map { (CGFloat($0.0 / washR1), $0.1) }
        let g = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(), colors: stops.map { $0.1 } as CFArray, locations: stops.map { $0.0 })!
        washGradients[col] = g
        return g
    }
    /// the same cross-section across the band's width, 0 at its inner edge to 1 at its outer (the near view draws the band as strips of this)
    private static func washAcross(_ col: RGB) -> CGGradient {
        if let g = washLinear[col] { return g }
        let g = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(), colors: washStops(col).map { $0.1 } as CFArray, locations: washStops(col).map { CGFloat(($0.0 - washR0) / (washR1 - washR0)) })!
        washLinear[col] = g
        return g
    }

    /// the part of a stay that is on show: nil when none of it is
    private func washSpan(_ w: RealDay.Wash) -> (end: Double, a0: Double, a1: Double)? {
        let sp = span(), lo = max(F - sp, TRACE0), hi = min(F + sp, Trev), end = w.t1 ?? Trev
        let a0 = max(w.t0, lo), a1 = min(end, hi)
        return a1 > a0 ? (end, a0, a1) : nil
    }

    /// how far into its stay's feathering the wash is at t: 0 at both ends, 1 in the middle
    private static func washProfile(_ w: RealDay.Wash, _ end: Double, _ fIn: Double, _ fOut: Double, _ t: Double) -> Double {
        smooth(0, 1, (t - w.t0) / fIn) * smooth(0, 1, (end - t) / fOut)
    }

    /// The wash of each stay, like the haze of the sky: a thin glow in the place's hue along his lane that fades to nothing on both sides, and along the dial in at the start and out
    /// at the end of the stay. It is cut into slices that each take a share of the strength, which the additive blend makes seamless. Close up it is the same band seen through the camera (`placeWashNear`).
    func placeWash(_ c: CGContext) {
        let washes = RealDay.shared.washes
        if washes.isEmpty { return }
        if cam != nil { placeWashNear(c, washes); return }
        let step = 32 * max(1, z), crowd = 1 / max(1, z * 0.7)       // zoomed out, several days lie on top of each other: each is fainter
        c.setBlendMode(.plusLighter); c.setAlpha(1)
        for w in washes {
            guard let (end, a0, a1) = washSpan(w) else { continue }
            let dur = end - w.t0, fIn = min(Self.washFeather, dur * 0.45), fOut = min(w.t1 == nil ? Self.washFeatherOpen : Self.washFeather, dur * 0.45)
            let profile = { (t: Double) in Self.washProfile(w, end, fIn, fOut, t) }
            var cuts: [Double] = [a0, a1]
            let n = Int(Self.washSlices)
            for j in 1..<n { cuts.append(w.t0 + fIn * Double(j) / Self.washSlices); cuts.append(end - fOut * Double(j) / Self.washSlices) }
            var t = a0 + step; while t < a1 { cuts.append(t); t += abs(t - F) > 0.55 * DAY * z / 2 ? step / 4 : step }      // finer where the view's own fade is (no steps at the far ends)
            cuts = cuts.filter { $0 >= a0 && $0 <= a1 }.sorted()
            for (s, e) in zip(cuts, cuts.dropFirst()) where e - s > 0.01 {
                let al = w.a * Self.washPeak * profile((s + e) / 2) * visT((s + e) / 2) * crowd
                if al < 0.002 { continue }
                c.saveGState()
                c.beginPath()
                c.addArc(center: CGPoint(x: CX, y: CY), radius: Self.washR1, startAngle: ang(s), endAngle: ang(e), clockwise: false)
                c.addArc(center: CGPoint(x: CX, y: CY), radius: Self.washR0, startAngle: ang(e), endAngle: ang(s), clockwise: true)
                c.closePath(); c.clip()
                c.setAlpha(al)
                c.drawRadialGradient(Self.washGradient(w.col), startCenter: CGPoint(x: CX, y: CY), startRadius: 0, endCenter: CGPoint(x: CX, y: CY), endRadius: Self.washR1, options: [])
                c.restoreGState()
            }
        }
        c.setBlendMode(.normal)
    }

    /// The same band, seen close up through the camera: the same strength, cross-section and feathering as above, as short strips along his lane that each take the strength at their
    /// middle. A strip is clipped to its four corners and filled across its width from the shared cross-section, so the band widens and thins with the perspective by itself and has no edge.
    /// Where the view is turning from flat to close, the camera is nearly overhead, so this meets the flat drawing.
    private func placeWashNear(_ c: CGContext, _ washes: [RealDay.Wash]) {
        let crowd = 1 / max(1, z * 0.7), r0 = Self.washR0, r1 = Self.washR1
        let screen = CGRect(x: -40, y: MAP_Y - 40, width: W + 80, height: MAP_H + 80)
        c.setBlendMode(.plusLighter); c.setAlpha(1)
        for w in washes {
            guard let (end, a0, a1) = washSpan(w) else { continue }
            let dur = end - w.t0, fIn = min(Self.washFeather, dur * 0.45), fOut = min(w.t1 == nil ? Self.washFeatherOpen : Self.washFeather, dur * 0.45)
            let grad = Self.washAcross(w.col)
            var t = a0
            while t < a1 {
                guard P(R_YOU, ang(t)) else { t += 2; continue }
                var dt = clamp(4 / (max(PS, 0.3) * k * R_YOU), 0.5, 6)       // about 4 pt of the band per strip
                if end - t < fOut + 6 { dt = min(dt, max(0.4, fOut / 10)) }       // finer where the stay ends
                let t2 = min(t + dt, a1), tm = (t + t2) / 2
                defer { t = t2 }
                let al = w.a * Self.washPeak * Self.washProfile(w, end, fIn, fOut, tm) * visT(tm) * crowd
                if al < 0.002 { continue }
                var q: [CGPoint] = []
                for (tt, r) in [(t, r0), (t2, r0), (t2, r1), (t, r1)] {
                    guard P(r, ang(tt)) else { break }
                    q.append(CGPoint(x: PX, y: PY))
                }
                if q.count < 4 || !screen.contains(q[0]) && !screen.contains(q[2]) && !screen.contains(q[1]) && !screen.contains(q[3]) { continue }
                c.saveGState()
                c.beginPath(); c.move(to: q[0]); q.dropFirst().forEach { c.addLine(to: $0) }; c.closePath(); c.clip()
                c.setAlpha(al)
                c.drawLinearGradient(grad, start: CGPoint(x: (q[0].x + q[1].x) / 2, y: (q[0].y + q[1].y) / 2), end: CGPoint(x: (q[2].x + q[3].x) / 2, y: (q[2].y + q[3].y) / 2), options: [])
                c.restoreGState()
            }
        }
        c.setBlendMode(.normal)
    }

    /// The name of every stay of three quarters of an hour or more, small and dim, along the dial inside his lane (close up it stands over the ring like the hours).
    /// It is cut to what fits the stay's stretch of the ring.
    func placeNames(_ c: CGContext) {
        let washes = RealDay.shared.washes
        if washes.isEmpty { return }
        let rr = Self.washNameR, calm = calmK
        for w in washes {
            guard let name = w.name, let (end, a0, a1) = washSpan(w), end - w.t0 >= PlaceTint.nameMinutes else { continue }
            let mid = (w.t0 + end) / 2, tm = mid >= a0 && mid <= a1 ? mid : (a0 + a1) / 2
            var v = visT(tm) * calm * (w.kept ? 0.5 : 0.3)
            var n = 9.0, sc = 1.0
            if cam != nil {
                guard P(rr, ang(tm)) else { continue }
                sc = PS; n = clamp((7 + 3.2 * PS.squareRoot()).rounded(), 9, 14); v *= fog()
            }
            if v < 0.03 { continue }
            let text = RealDay.shared.fit(name, (((a1 - a0) * k * rr + 4) * sc) / n)
            if text == "…" || (text != name && text.count < 3) { continue }
            let s = spr.word(text, .mono(400, n), .ink, n)
            if cam == nil { tangent(c, s, tm, rr, v, 1, upright: true); continue }
            let hw = (s.w - 6) / 2, box = (PX - hw, PY - 0.62 * n, PX + hw, PY + 0.62 * n)
            if PX < 4 || PX > W - 4 || PY < MAP_Y + 8 || PY > MAP_Y + MAP_H - 24 { continue }
            if numeralBoxes.contains(where: { box.0 < $0.2 && box.2 > $0.0 && box.1 < $0.3 && box.3 > $0.1 }) { continue }
            numeralBoxes.append(box)
            stand(c, s, PX, PY, v)
        }
        c.setAlpha(1)
    }
}
