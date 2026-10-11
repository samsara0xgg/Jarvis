import UIKit
import CoreGraphics

// Where he was: each stay is a very faint wedge of colour behind the ring for its time span, like the haze of the sky, with the place's name set inside his lane.
// The stays (and their colours and names) are built once when the days load (`RealDay.washes`); this only paints them.

extension StarEngine {
    /// how far a wash feathers in and out along the dial, in minutes (at most 0.45 of the stay; an open end, still going now, a shorter way)
    private static let washFeather = 60.0, washFeatherOpen = 30.0
    /// the wash's radial profile: a gaussian that peaks on his lane and is nothing at both of its ends, as a gradient from the pole outwards (one per colour)
    /// the names go between her lane and his
    private static let washNameR = 118.0
    private static let washR0 = R_HER + 10, washR1 = R_SC + 2
    private static var washGradients: [RGB: CGGradient] = [:]
    private static func washGradient(_ col: RGB) -> CGGradient {
        if let g = washGradients[col] { return g }
        let lo = washR0, hi = washR1, sigma = 19.0
        func gauss(_ r: Double) -> Double { exp(-pow((r - R_YOU) / sigma, 2) / 2) }
        let floorV = max(gauss(lo), gauss(hi))
        var stops: [(CGFloat, CGColor)] = [(0, col.cg(0))]
        for i in 0...12 {
            let r = lo + (hi - lo) * Double(i) / 12
            stops.append((CGFloat(r / hi), col.cg(i == 0 || i == 12 ? 0 : max(0, (gauss(r) - floorV) / (1 - floorV)))))
        }
        let g = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(), colors: stops.map { $0.1 } as CFArray, locations: stops.map { $0.0 })!
        washGradients[col] = g
        return g
    }

    /// the part of a stay that is on show: nil when none of it is
    private func washSpan(_ w: RealDay.Wash) -> (end: Double, a0: Double, a1: Double)? {
        let sp = span(), lo = max(F - sp, TRACE0), hi = min(F + sp, Trev), end = w.t1 ?? Trev
        let a0 = max(w.t0, lo), a1 = min(end, hi)
        return a1 > a0 ? (end, a0, a1) : nil
    }

    /// The wash of each stay, like the haze of the sky: his lane's hue in a soft radial glow that fades to nothing on both sides, and along the dial in at the start and out
    /// at the end of the stay. It is cut into slices that each take a share of the strength, which the additive blend makes seamless. Close up there is no wash, only the names.
    func placeWash(_ c: CGContext) {
        let washes = RealDay.shared.washes
        if washes.isEmpty || cam != nil { return }
        let step = 60 * max(1, z), crowd = 1 / max(1, z * 0.7)       // zoomed out, several days lie on top of each other: each is fainter
        c.setBlendMode(.plusLighter); c.setAlpha(1)
        for w in washes {
            guard let (end, a0, a1) = washSpan(w) else { continue }
            let dur = end - w.t0, fIn = min(Self.washFeather, dur * 0.45), fOut = min(w.t1 == nil ? Self.washFeatherOpen : Self.washFeather, dur * 0.45)
            func profile(_ t: Double) -> Double { smooth(0, 1, (t - w.t0) / fIn) * smooth(0, 1, (end - t) / fOut) }
            var cuts: [Double] = [a0, a1]
            for j in 1..<16 { cuts.append(w.t0 + fIn * Double(j) / 16); cuts.append(end - fOut * Double(j) / 16) }
            var t = a0 + step; while t < a1 { cuts.append(t); t += step }
            cuts = cuts.filter { $0 >= a0 && $0 <= a1 }.sorted()
            for (s, e) in zip(cuts, cuts.dropFirst()) where e - s > 0.01 {
                let al = w.a * 2.4 * profile((s + e) / 2) * visT((s + e) / 2) * crowd
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
            if cam == nil { tangent(c, s, tm, rr, v, 1); continue }
            let hw = (s.w - 6) / 2, box = (PX - hw, PY - 0.62 * n, PX + hw, PY + 0.62 * n)
            if PX < 4 || PX > W - 4 || PY < MAP_Y + 8 || PY > MAP_Y + MAP_H - 24 { continue }
            if numeralBoxes.contains(where: { box.0 < $0.2 && box.2 > $0.0 && box.1 < $0.3 && box.3 > $0.1 }) { continue }
            numeralBoxes.append(box)
            stand(c, s, PX, PY, v)
        }
        c.setAlpha(1)
    }
}
