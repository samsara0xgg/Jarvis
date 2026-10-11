import UIKit
import CoreGraphics

// Where he was: each stay is a very faint wedge of colour behind the ring for its time span, like the haze of the sky, with the place's name set inside his lane.
// The stays (and their colours and names) are built once when the days load (`RealDay.washes`); this only paints them.

extension StarEngine {
    /// A wash is a few stacked layers, each a little narrower in radius and held in a little further from the ends than the last, so it fades out instead of ending in edges.
    /// `share` is how much of the wash's strength a layer carries.
    private static let washLayers: [(r0: Double, r1: Double, inset: Double, share: Double)] = [
        (R_HER + 12, R_SC - 2, 0, 0.3), (R_YOU - 30, R_YOU + 22, 3, 0.4), (R_YOU - 12, R_YOU + 10, 7, 0.4),
    ]
    /// the names go between her lane and his
    private static let washNameR = 118.0

    /// the part of a stay that is on show: nil when none of it is
    private func washSpan(_ w: RealDay.Wash) -> (end: Double, a0: Double, a1: Double)? {
        let sp = span(), lo = max(F - sp, TRACE0), hi = min(F + sp, Trev), end = w.t1 ?? Trev
        let a0 = max(w.t0, lo), a1 = min(end, hi)
        return a1 > a0 ? (end, a0, a1) : nil
    }

    /// an annular sector, left as the current path; false when too little of it is in front of the camera
    private func sector(_ c: CGContext, _ r0: Double, _ r1: Double, _ a0: Double, _ a1: Double) -> Bool {
        c.beginPath()
        if cam == nil {
            c.addArc(center: CGPoint(x: CX, y: CY), radius: r1, startAngle: a0, endAngle: a1, clockwise: false)
            c.addArc(center: CGPoint(x: CX, y: CY), radius: r0, startAngle: a1, endAngle: a0, clockwise: true)
            c.closePath()
            return true
        }
        let n = max(2, Int(ceil((a1 - a0) / 0.05)))
        var pts: [CGPoint] = []
        for (r, rev) in [(r1, false), (r0, true)] {
            for i in 0...n {
                let f = Double(rev ? n - i : i) / Double(n)
                if P(r, a0 + (a1 - a0) * f) { pts.append(CGPoint(x: PX, y: PY)) }
            }
        }
        if pts.count < 4 { return false }
        c.addLines(between: pts); c.closePath()
        return true
    }

    func placeWash(_ c: CGContext) {
        let washes = RealDay.shared.washes
        if washes.isEmpty { return }
        let step = 60 * max(1, z), crowd = (cam == nil ? 1 : 0.7) / max(1, z * 0.7)       // zoomed out, several days lie on top of each other: each is fainter
        c.setBlendMode(.plusLighter); c.setAlpha(1)
        for w in washes {
            guard let (end, a0, a1) = washSpan(w) else { continue }
            var s = a0
            while s < a1 {
                let e = min(a1, s + step), mid = (s + e) / 2
                var v = visT(mid)
                if cam != nil, P(R_YOU, ang(mid)) { v *= fog() }
                if v >= 0.02 {
                    for L in Self.washLayers where cam == nil || L.r0 > R_HER + 20 {      // close up the wide layer would be a slab; the narrow ones read as haze
                        let inset = min(L.inset, (end - w.t0) / 4)
                        let ts = s <= w.t0 ? s + inset : s, te = e >= end ? e - inset : e
                        if te <= ts || !sector(c, L.r0, L.r1, ang(ts), ang(te)) { continue }
                        c.setFillColor(w.col.cg(w.a * L.share * v * crowd)); c.fillPath()
                    }
                }
                s = e
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
