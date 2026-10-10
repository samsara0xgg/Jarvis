import UIKit
import CoreGraphics

// Painting the dial. Every function draws into a y-down design space (440 × 956 points); the map layer's context is set up that way by StarMapLayer.

private let TICKS: [(Double, Double, Double, Double)] = [(5, 2.2, 0.2, 0.8), (15, 3.4, 0.27, 0.9), (60, 6.2, 0.4, 1), (180, 8.6, 0.5, 1.1), (720, 9.8, 0.54, 1.2), (1440, 13, 0.72, 1.5)]
private let C_DONE = RGB.done.tint(0.15), C_WAIT = RGB.wait.tint(0.15), C_WORK = RGB.work.tint(0.2), C_HEAD = RGB.work.tint(0.5)

extension StarEngine {
    var spr: SpriteCache { sprites }

    // MARK: rings

    func arcPath(_ p: CGMutablePath, _ r: Double, _ a0: Double, _ a1: Double) {
        if cam == nil { p.addArc(center: CGPoint(x: CX, y: CY), radius: r, startAngle: a0, endAngle: a1, clockwise: false); return }
        let n = max(2, Int(ceil((a1 - a0) / 0.018)))
        var on = false
        for i in 0...n {
            if P(r, a0 + (a1 - a0) * Double(i) / Double(n)) {
                if on { p.addLine(to: CGPoint(x: PX, y: PY)) } else { p.move(to: CGPoint(x: PX, y: PY)); on = true }
            } else { on = false }
        }
    }

    /// stroke part of a ring; seen close up, its line thickens as it comes near
    func strokeRing(_ c: CGContext, _ r: Double, _ a0: Double, _ a1: Double, _ w: Double, dash: [CGFloat] = []) {
        if cam == nil {
            c.setLineWidth(w); c.setLineDash(phase: 0, lengths: dash)
            c.beginPath(); c.addArc(center: CGPoint(x: CX, y: CY), radius: r, startAngle: a0, endAngle: a1, clockwise: false); c.strokePath()
            return
        }
        let n = max(2, Int(ceil((a1 - a0) / 0.018)))
        var paths: [Double: CGMutablePath] = [:]
        var ok0 = P(r, a0), x0 = PX, y0 = PY
        for i in 1...n {
            let ok = P(r, a0 + (a1 - a0) * Double(i) / Double(n))
            if ok && ok0 {
                let key = (w * clamp(PS, 0.6, 3.2) * 4).rounded() / 4
                let p = paths[key] ?? CGMutablePath(); paths[key] = p
                p.move(to: CGPoint(x: x0, y: y0)); p.addLine(to: CGPoint(x: PX, y: PY))
            }
            ok0 = ok; x0 = PX; y0 = PY
        }
        for (wd, p) in paths {
            c.setLineWidth(wd)
            c.setLineDash(phase: 0, lengths: dash.map { $0 * CGFloat(wd / w) })
            c.addPath(p); c.strokePath()
        }
        c.setLineDash(phase: 0, lengths: dash)
    }

    func linGradient(_ stops: [(CGFloat, CGColor)]) -> CGGradient {
        CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(), colors: stops.map { $0.1 } as CFArray, locations: stops.map { $0.0 })!
    }

    /// the fixed rings, the meridian and its light (painted once into the base layer when flat, with everything else when not)
    func frame(_ c: CGContext) {
        c.setAlpha(1); c.setLineCap(.round)
        c.setStrokeColor(RGB.dial.cg(0.26))
        strokeRing(c, R_HER, 0, TAU, 1.1, dash: [0, CGFloat(TAU * R_HER / 168)])      // her ring: fine dots
        c.setLineDash(phase: 0, lengths: [])
        func ring(_ r: Double, _ a: Double, _ w: Double) { c.setStrokeColor(RGB.dial.cg(a)); strokeRing(c, r, 0, TAU, w) }
        ring(R_YOU, 0.075 + 0.14 * camE, 1); ring(R_SC, 0.22, 0.8); ring(R_BEZ, 0.075, 0.7); ring(R_BEZ + 3.5, 0.04, 0.6)
        // a narrow light along the meridian, and the meridian itself; it marks F, so under the lens it bends aside too (close up you stand on F)
        let a = ang(F), cs = cos(a), sn = sin(a)
        func pts(_ q: [(Double, Double)]) -> [CGPoint]? {
            var o: [CGPoint] = []
            for (r, w) in q { if !Pxy(r * cs - w * sn, r * sn + w * cs) { return nil }; o.append(CGPoint(x: PX, y: PY)) }
            return o
        }
        let far = 1 - smooth(0, 0.4, camE)
        if far > 0.02, let k1 = pts([(R_PS, 2), (R_PS, -2), (R_BEZ, -12), (R_BEZ, 12)]) {
            c.saveGState(); c.setAlpha(far)
            c.beginPath(); c.move(to: k1[0]); c.addLine(to: k1[1]); c.addLine(to: k1[2]); c.addLine(to: k1[3]); c.closePath(); c.clip()
            let g = linGradient([(0, CGColor(srgbRed: 200 / 255, green: 212 / 255, blue: 1, alpha: 0.07)), (1, CGColor(srgbRed: 200 / 255, green: 212 / 255, blue: 1, alpha: 0))])
            c.drawLinearGradient(g, start: CGPoint(x: (k1[0].x + k1[1].x) / 2, y: (k1[0].y + k1[1].y) / 2), end: CGPoint(x: (k1[2].x + k1[3].x) / 2, y: (k1[2].y + k1[3].y) / 2), options: [])
            c.restoreGState()
        }
        if far > 0.02, let k2 = pts([(R_PS + 8, 0), (R_BEZ + 2, 0)]) {
            c.saveGState(); c.setAlpha(far); c.setLineWidth(0.8)
            c.move(to: k2[0]); c.addLine(to: k2[1]); c.replacePathWithStrokedPath(); c.clip()
            let g = linGradient([(0, CGColor(srgbRed: 226 / 255, green: 232 / 255, blue: 1, alpha: 0)), (0.55, CGColor(srgbRed: 226 / 255, green: 232 / 255, blue: 1, alpha: 0.26)),
                                 (1, CGColor(srgbRed: 226 / 255, green: 232 / 255, blue: 1, alpha: 0.46))])
            c.drawLinearGradient(g, start: k2[0], end: k2[1], options: [.drawsAfterEndLocation, .drawsBeforeStartLocation])
            c.restoreGState()
        }
        if far > 0.02, let k3 = pts([(R_BEZ + 11, 4.2), (R_BEZ + 11, -4.2), (R_BEZ + 5, 0)]) {       // the index under the bezel
            c.saveGState(); c.setAlpha(far); c.setFillColor(CGColor(srgbRed: 240 / 255, green: 244 / 255, blue: 1, alpha: 0.9))
            c.beginPath(); c.move(to: k3[0]); c.addLine(to: k3[1]); c.addLine(to: k3[2]); c.closePath(); c.fillPath(); c.restoreGState()
        }
        c.setAlpha(1)
    }

    // MARK: the dial, every frame

    func drawMap(_ c: CGContext, _ now: Double) {
        if mon > 0.985 { return }
        if baseOff { frame(c) }
        c.setLineCap(.round)
        if cam != nil { sunBand(c) }
        ticks(c)
        numerals(c)                         // close up they go down before the names, so the names keep out of their way
        arcs(c); trace(c, now); constellations(c); events(c, now)
        nowMark(c)
        if ls.value > 0.02 && cam == nil { lensMark(c) }
        if cam != nil { fadeBottom(c) }
    }

    /// close up the road and the ring run to the bottom of the map layer; let them go out before the big time instead of stopping in a line
    func fadeBottom(_ c: CGContext) {
        let y0 = MAP_Y + MAP_H - 24, y1 = MAP_Y + MAP_H
        c.saveGState(); c.setBlendMode(.destinationOut); c.setAlpha(1)
        c.clip(to: CGRect(x: 0, y: y0, width: W, height: y1 - y0))
        c.drawLinearGradient(linGradient([(0, CGColor(gray: 0, alpha: 0)), (1, CGColor(gray: 0, alpha: 1))]), start: CGPoint(x: 0, y: y0), end: CGPoint(x: 0, y: y1), options: [])
        c.restoreGState()
    }

    /// seen at a slant: short strokes, each its own colour (flat, the band is a conic gradient layer of its own)
    func sunBand(_ c: CGContext) {
        let sp = DAY * z, s0 = F - sp / 2
        var ok0 = P(R_BAND, ang(s0)), x0 = PX, y0 = PY
        c.setAlpha(1); c.setLineCap(.butt)
        for i in 1...180 {
            let t = s0 + sp * Double(i) / 180, ok = P(R_BAND, ang(t))
            if ok && ok0 {
                let (col, al) = sunAt(t), a = al * visT(t) * fog()
                if a > 0.01 {
                    c.setStrokeColor(col.cg(a)); c.setLineWidth(2.4 * clamp(PS, 0.6, 2.2))
                    c.beginPath(); c.move(to: CGPoint(x: x0, y: y0)); c.addLine(to: CGPoint(x: PX, y: PY)); c.strokePath()
                }
            }
            ok0 = ok; x0 = PX; y0 = PY
        }
        c.setLineCap(.round)
    }

    /// colour stops for the flat band's conic gradient layer: (location 0...1 around the ring, colour) starting at angle `a0`
    func sunStops() -> (a0: Double, stops: [(Double, RGB, Double)]) {
        let sp = DAY * z, s0 = F - sp / 2
        var fs2 = Set<Double>()
        for f in FADE { fs2.insert(f); fs2.insert(1 - f) }
        for dd in floorDay(s0)...floorDay(s0 + sp) {
            for (m, _, _) in SUN { let f = (Double(dd) * DAY + m - s0) / sp; if f > 0 && f < 1 { fs2.insert(f) } }
        }
        let a0 = ang(s0)
        var out: [(Double, RGB, Double)] = []
        for f in fs2.sorted() {
            let t = s0 + sp * f, (col, al) = sunAt(t)
            out.append((clamp((ang(t) - a0) / TAU, 0, 1), col, al * visT(t)))
        }
        return (a0, out)
    }

    func ticks(_ c: CGContext) {
        let sp = span(), pxm = k * R_SC
        var paths: [String: (CGMutablePath, Double, Double)] = [:]
        func push(_ an: Double, _ r0: Double, _ r1: Double, _ wd0: Double, _ a0: Double) {
            var wd = wd0, a = a0
            if a < 0.015 { return }
            let cs = cos(an), sn = sin(an)
            if !Pxy(r0 * cs, r0 * sn) { return }
            let x0 = PX, y0 = PY, s = PS
            if !Pxy(r1 * cs, r1 * sn) { return }
            if cam != nil { wd = (wd * clamp(s, 0.6, 2.4) * 4).rounded() / 4; a *= fog() }
            let aq = (a * 20).rounded() / 20, key = "\(wd)|\(aq)"
            var e = paths[key]
            if e == nil { e = (CGMutablePath(), wd, aq); paths[key] = e }
            e!.0.move(to: CGPoint(x: x0, y: y0)); e!.0.addLine(to: CGPoint(x: PX, y: PY))
        }
        // each level shows where it has room, which the lens and the camera make locally
        var li = 0
        if cam == nil { while li < TICKS.count - 1 && TICKS[li].0 * pxm * lensM < 4.5 { li += 1 } }
        let stp = TICKS[li].0
        var m = ceil((F - sp) / stp) * stp
        while m <= F + sp {
            defer { m += stp }
            var L = li
            while L + 1 < TICKS.count && m.truncatingRemainder(dividingBy: TICKS[L + 1].0) == 0 { L += 1 }
            let v = visT(m); if v < 0.02 { continue }
            let an = ang(m); var sc = magAt(m)
            if cam != nil { if !P(R_SC, an) { continue }; sc *= PS }
            let fine = smooth(4.5, 9, TICKS[L].0 * pxm * sc); if fine < 0.02 { continue }
            let near = 1 - smooth(0, 75 * z, abs(m - F)), (_, len0, a0, wd) = TICKS[L]
            push(an, R_SC - len0 * (1 + 0.5 * near), R_SC + (L == 5 ? 5 : 0), wd, (a0 + 0.34 * near) * v * fine)
        }
        let vz = li != 0 ? 1 - smooth(1.15, 1.6, z) : 0            // a vernier of five-minute marks around the meridian
        if vz > 0.01 {
            var m = ceil((F - 70) / 5) * 5
            while m <= F + 70 {
                defer { m += 5 }
                if m.truncatingRemainder(dividingBy: 15) == 0 { continue }
                let near = 1 - smooth(8, 70, abs(m - F))
                push(ang(m), R_SC - 2.2 - 1.4 * near, R_SC, 0.7, 0.34 * near * vz)
            }
        }
        c.setAlpha(1)
        for (_, e) in paths { c.setLineWidth(e.1); c.setStrokeColor(RGB.dial.cg(e.2)); c.addPath(e.0); c.strokePath() }
    }

    /// a word set along the dial, its top toward her
    func tangent(_ c: CGContext, _ s: Sprite, _ t: Double, _ r: Double, _ a: Double, _ scale: Double) {
        if a < 0.01 { return }
        let an = ang(t), x = CX + r * cos(an), y = CY + r * sin(an), ro = an - Double.pi / 2
        c.saveGState(); c.translateBy(x: x, y: y); c.rotate(by: ro); c.scaleBy(x: scale, y: scale)
        c.setAlpha(min(1, a)); blit(c, s.img, -s.w / 2, -s.h / 2, s.w, s.h)
        c.restoreGState()
    }
    /// seen from the camera, a word turns from lying along the ring to standing up, facing you
    func stand(_ c: CGContext, _ s: Sprite, _ x: Double, _ y: Double, _ a: Double) {
        if a < 0.01 || x < -60 || x > W + 60 || y < MAP_Y - 20 || y > MAP_Y + MAP_H + 20 { return }
        let ro = (atan2(y - cenY, x - cenX) - Double.pi / 2) * (1 - smooth(0, 0.55, camE))
        c.saveGState(); c.translateBy(x: x, y: y); c.rotate(by: ro)
        c.setAlpha(min(1, a)); blit(c, s.img, -s.w / 2, -s.h / 2, s.w, s.h)
        c.restoreGState()
    }

    func numerals(_ c: CGContext) {
        let sp = span(), pxh = 60 * k * R_NUM, today = floorDay(nowT())
        if cam != nil {
            var m = ceil((F - sp) / 60) * 60
            while m <= F + sp {
                defer { m += 60 }
                let v = visT(m), an = ang(m)
                if v < 0.02 || !Pxy((R_SC + 5) * cos(an), (R_SC + 5) * sin(an), 5) { continue }
                let hr = Int((m / 60).rounded()), h = ((hr % 24) + 24) % 24
                let major = h % 3 == 0, a0 = smooth(26, 40, (major ? 3 : 1) * pxh * PS)
                if a0 < 0.01 { continue }
                let near = 1 - smooth(0, 150 * z, abs(m - F)), dd = Int((m / DAY).rounded()), n = sizeAt(10, PS)
                let s = h == 0 ? spr.word(dayWord(dd), .sans(600, n), dd == today ? .warm : .ink, n) : spr.word(two(h), .mono(500, n), .ink, n)
                if names.contains(where: { PX - s.w / 2 < $0.2 && PX + s.w / 2 > $0.0 && PY - n * 0.6 < $0.3 && PY + n * 0.6 > $0.1 }) { continue }
                stand(c, s, PX, PY, ((major ? 0.5 : 0.3) + 0.5 * near) * a0 * v * fog())
            }
            c.setAlpha(1)
            return
        }
        // the lens turns the stretch under it into a closer zoom: z / magnification is the zoom it reads at
        func hA(_ lm: Double, _ n: Double) -> Double { smooth(26, 40, n * pxh * lm) * (1 - smooth(2.3, 3.2, z / lm)) }
        if hA(lensM, 3) > 0.01 {
            var m = ceil((F - sp) / 60) * 60
            while m <= F + sp {
                defer { m += 60 }
                let hr = Int((m / 60).rounded()), h = ((hr % 24) + 24) % 24, major = h % 3 == 0, lm = magAt(m), a0 = hA(lm, major ? 3 : 1)
                if a0 < 0.01 { continue }
                let v = visT(m); if v < 0.02 { continue }
                let near = 1 - smooth(0, 150 * z, abs(m - F)), dd = Int((m / DAY).rounded())
                let s = h == 0 ? spr.word(dayWord(dd), .sans(600, 10.5), dd == today ? .warm : .ink, 10.5) : spr.word(two(h), .mono(500, major ? 10 : 9), .ink, 10)
                tangent(c, s, m, R_NUM, ((major ? 0.5 : 0.3) + 0.5 * near) * a0 * v, 1 + 0.14 * near)
            }
        }
        if smooth(2.3, 3.4, z) > 0.01 {
            for d in floorDay(F - sp)...Int(ceil((F + sp) / DAY)) {
                let m = Double(d) * DAY + 720, v = visT(m), dayA = smooth(2.3, 3.4, z / magAt(m))
                if v < 0.02 || dayA < 0.01 { continue }
                let near = 1 - smooth(0.1 * DAY, 0.5 * DAY, abs(m - F))
                tangent(c, spr.dayWord(d, d == today ? .warm : .ink), m, R_NUM + 1, (0.4 + 0.55 * near) * v * dayA, 1 + 0.08 * near)
            }
        }
        c.setAlpha(1)
    }

    func arcs(_ c: CGContext) {
        let now = nowMs()
        for e in EV {
            guard let dur = e.d, e.born <= now else { continue }
            let v = min(visT(e.t), visT(e.t + dur)); if v < 0.03 { continue }
            let youR = e.lane == .you, r = youR ? R_YOU - 7 : R_HER, end = e.t + dur, ahead = e.t > Trev
            func seg(_ a: Double, _ b: Double, _ col: RGB, _ al: Double, _ w: Double, _ dash: [CGFloat] = []) {
                if b <= a { return }
                c.setAlpha(1); c.setStrokeColor(col.cg(al * v))
                strokeRing(c, r, ang(a), ang(b), w, dash: dash)
            }
            if e.st == .next || ahead { seg(e.t, end, .next, 0.26, 1.1, [1.5, 3.6]) } else {
                let mid = min(end, max(e.t, Trev))
                let col = e.st == .work ? RGB.work : youR ? RGB.lit : RGB.done
                seg(e.t, mid, col, e.st == .work ? 0.55 : youR ? 0.3 : 0.4, e.st == .work ? 2.2 : 1.8)
                seg(mid, end, col, 0.28, 1.2, [2, 4])
            }
        }
        c.setLineDash(phase: 0, lengths: [])
    }

    func trace(_ c: CGContext, _ now: Double) {
        let sp = span(), a = max(F - sp, TRACE0), b = min(F + sp, Trev)
        if b <= a { return }
        let stepM = 5 * max(1, z * 0.72)
        var paths = [CGMutablePath?](repeating: nil, count: 36)
        var prev = -1, lx = 0.0, ly = 0.0
        var m = a
        while true {
            if m > b { m = b }
            let ac = actAt(m), kd = kindAt(m), cls = isQuiet(kd) ? 0 : ac > 0.4 ? 2 : 1
            if !P(R_YOU + amp * ac - (cls == 0 ? 1.2 : 0), ang(m)) {
                prev = -1
                if m >= b { break }
                m += stepM; continue
            }
            let x = PX, y = PY, v = visT(m) * fog()
            let key = (cls * 4 + min(3, Int((v * 4).rounded(.down)))) * 3 + (cam == nil || PS < 1.3 ? 0 : PS < 2.3 ? 1 : 2)
            if key != prev {
                if prev >= 0 { paths[prev]!.addLine(to: CGPoint(x: x, y: y)) }
                if paths[key] == nil { paths[key] = CGMutablePath() }
                paths[key]!.move(to: CGPoint(x: prev >= 0 ? lx : x, y: prev >= 0 ? ly : y))
            }
            paths[key]!.addLine(to: CGPoint(x: x, y: y)); lx = x; ly = y; prev = key
            if m >= b { break }
            m += stepM
        }
        let WK: [Double] = [1, 1.7, 2.6]
        c.setAlpha(1); c.setLineJoin(.round)
        c.setBlendMode(.plusLighter)                  // the moving parts glow a little
        for i in 24..<36 {
            guard let p = paths[i] else { continue }
            c.setStrokeColor(RGB.warm.cg(0.1 * (Double(((i / 3) & 3)) + 0.5) / 4)); c.setLineWidth(5 * WK[i % 3]); c.setLineDash(phase: 0, lengths: [])
            c.addPath(p); c.strokePath()
        }
        c.setBlendMode(.normal)
        for i in 0..<36 {
            guard let p = paths[i] else { continue }
            let j = i / 3, cls = j >> 2, v = (Double(j & 3) + 0.5) / 4, wk = WK[i % 3]
            c.setLineDash(phase: 0, lengths: cls == 0 ? [CGFloat(1.1 * wk), CGFloat(3.4 * wk)] : [])
            c.setStrokeColor(RGB.lit.cg((cls == 0 ? 0.34 : cls == 1 ? 0.46 : 0.8) * v)); c.setLineWidth((cls == 2 ? 1.5 : 1.15) * wk)
            c.addPath(p); c.strokePath()
        }
        c.setLineDash(phase: 0, lengths: [])
        if b == Trev && visT(b) > 0.05 && P(R_YOU + amp * actAt(b), ang(b)) {       // where you are now
            let v = visT(b) * fog(), s = cam != nil ? clamp(PS, 0.5, 2) : 1
            c.setBlendMode(.plusLighter); spr.glow(c, .warm, PX, PY, 11 * s, 0.85 * v); c.setBlendMode(.normal)
            c.setAlpha(v); c.setFillColor(CGColor(srgbRed: 1, green: 250 / 255, blue: 240 / 255, alpha: 1)); fillCircle(c, PX, PY, 1.9 * s); c.setAlpha(1)
        }
    }

    func constellations(_ c: CGContext) {
        let win = 26 * z, now = nowMs()
        c.setLineWidth(0.7); c.setAlpha(1)
        for (ia, ib) in CONS {
            guard let A = byId(ia), let B = byId(ib), A.born <= now, B.born <= now else { continue }
            let v = min(visT(A.t), visT(B.t)); if v < 0.03 { continue }
            let hot = abs(A.t - F) < win || abs(B.t - F) < win || openId == A.id || openId == B.id
            let ra = laneR(A), rb = laneR(B), aa = ang(A.t), ab = ang(B.t)
            let xa = ra * cos(aa), ya = ra * sin(aa), xb = rb * cos(ab), yb = rb * sin(ab)
            if !Pxy(xa, ya) { continue }; let x1 = PX, y1 = PY
            if !Pxy(xb, yb) { continue }; let x2 = PX, y2 = PY, f2 = fog()
            if !Pxy((xa + xb) / 2 * 0.86, (ya + yb) / 2 * 0.86) { continue }   // bowed a little toward her
            c.setStrokeColor(RGB.ink.cg(v * (hot ? 0.36 : 0.09) * f2))
            c.beginPath(); c.move(to: CGPoint(x: x1, y: y1)); c.addQuadCurve(to: CGPoint(x: x2, y: y2), control: CGPoint(x: PX, y: PY)); c.strokePath()
        }
    }

    func events(_ c: CGContext, _ now: Double) {
        let t = now / 1000, win = 26 * z, sc = z > 2.6 ? 0.8 : 1.0
        var list: [Ev] = []
        for e in EV {
            e.on = false
            if e.born > now { continue }
            let r = laneR(e), an = ang(e.t)
            if !P(r, an) { continue }
            let v = visT(e.t) * clamp((now - e.born) / 600, 0, 1) * fog(); if v < 0.02 { continue }
            e.kd = kdOf(e)
            if let d = e.d { e.focus = F >= e.t - win && F <= e.t + d + win } else { e.focus = abs(e.t - F) < win }
            e.on = true; e.v = v; e.r = r; e.an = an; e.x = PX; e.y = PY; e.pr = PS; e.ps = cam != nil ? clamp(PS, 0.45, 2.4) : 1
            list.append(e)
        }
        pulsing = list.contains { $0.kd == .wait || $0.kd == .work || $0.kd == .lit }     // these breathe, so the dial keeps drawing a little at rest
        let calmK = self.calmK
        c.setBlendMode(.plusLighter)
        for e in list {
            let m = (e.mag ?? 1) * sc * e.ps, v = e.v, fh = clamp(1 - (now - e.flash) / 700, 0, 1), ig = clamp(1 - (now - e.ign) / 900, 0, 1)
            switch e.kd {
            case .lit: spr.glow(c, .lit, e.x, e.y, 9 * m, 0.42 * v)
            case .wait: spr.glow(c, .wait, e.x, e.y, 13 * m, (0.44 + 0.16 * sin(t * 2.6)) * v)
            case .work: spr.glow(c, .work, e.x, e.y, 12 * m, 0.42 * v)
            case .done: spr.glow(c, .done, e.x, e.y, 8 * m, 0.2 * v)
            case .next: break
            }
            if e.focus || fh > 0 || openId == e.id { spr.glow(c, .white, e.x, e.y, (17 + 7 * fh) * m, ((openId == e.id ? 0.15 : 0.15 * calmK) + 0.34 * fh) * v) }
            if ig > 0 { spr.glow(c, .warm, e.x, e.y, (7 + 28 * (1 - ig)) * m, 0.95 * ig * ig * v) }
        }
        c.setBlendMode(.normal)
        for e in list { c.setAlpha(e.v); mark(c, e, t, now, (e.mag ?? 1) * sc * e.ps) }
        c.setAlpha(1)
        let lensOn = ls.value > 0.02
        if cam != nil {        // close up, every name in view stands up and faces you, larger as it comes near; too far to read, it lets go
            var placed: [(Double, Double, Double, Double)] = []
            let fin = smooth(0.25, 0.7, camE)
            for e in list.filter({ $0.s != nil }).sorted(by: { $0.pr > $1.pr }) {
                let la = e.v * fin * smooth(0.8, 1.1, e.pr) * calmK; if la < 0.03 { continue }
                let n = sizeAt(11, e.ps), w = spr.word(e.ps > 0.95 ? "\(clock(e.t))  \(e.s!)" : e.s!, .sans(500, n), .ink, n)
                if e.x < 0 || e.x > W { continue }
                let lx = clamp(e.x, w.w / 2 + 8, W - w.w / 2 - 8), ly = e.y - (8 + 7 * e.ps) - n * 0.3
                let box = (lx - w.w / 2 - 4, ly - n * 0.62, lx + w.w / 2 + 4, ly + n * 0.62)
                if placed.contains(where: { box.0 < $0.2 && box.2 > $0.0 && box.1 < $0.3 && box.3 > $0.1 }) { continue }
                placed.append(box); c.setAlpha(0.84 * la); blit(c, w.img, lx - w.w / 2, ly - w.h / 2, w.w, w.h)
            }
            c.setAlpha(1); names = placed
            return
        }
        if z > 2.6 && !lensOn { return }
        // names of what is near the meridian (or under the lens) but not in focus, nearest first, never on top of each other
        var placed: [(Double, Double, Double, Double)] = []
        var cand: [(e: Ev, dist: Double, lm: Double)] = []
        for e in list where e.s != nil && (!e.focus || lensOn) { cand.append((e, abs(e.t - F), lensOn ? magAt(e.t) : 1)) }
        cand.sort { (a: (e: Ev, dist: Double, lm: Double), b: (e: Ev, dist: Double, lm: Double)) -> Bool in
            if a.lm != b.lm { return a.lm > b.lm }
            return a.dist < b.dist
        }
        for item in cand {
            let e = item.e, dist = item.dist, lm = item.lm
            var la = z > 2.6 ? 0 : e.v * smooth(14 * z, 34 * z, dist) * (1 - smooth(100 * z, 190 * z, dist)) * calmK
            if lensOn { la = max(la, e.v * ls.value * smooth(1.3, 2.2, lm)) }
            if la < 0.03 { continue }
            let r = e.r + (e.lane == .you ? 17 : -16), lx = CX + r * cos(e.an), ly = CY + r * sin(e.an)
            let w = spr.word(e.s!, .sans(500, 11), .ink, 11), box = (lx - w.w / 2 + 2, ly - 7, lx + w.w / 2 - 2, ly + 7)
            if placed.contains(where: { box.0 < $0.2 && box.2 > $0.0 && box.1 < $0.3 && box.3 > $0.1 }) { continue }
            placed.append(box)
            c.setAlpha(0.62 * la); blit(c, w.img, lx - w.w / 2, ly - w.h / 2, w.w, w.h)
        }
        c.setAlpha(1)
    }

    func mark(_ c: CGContext, _ e: Ev, _ t: Double, _ now: Double, _ m: Double) {
        let x = e.x, y = e.y, ig = clamp((now - e.ign) / 520, 0, 1)
        switch e.kd {
        case .next:
            let f = e.focus || openId == e.id
            c.setStrokeColor(RGB.next.cg(f ? 0.95 : 0.58)); c.setLineWidth(0.9); star4(c, x, y, 4.4 * m); c.strokePath()
            if f { c.setFillColor(RGB.next.cg(0.55)); fillCircle(c, x, y, 0.9 * m) }
        case .lit:
            let s = ig < 1 ? Ease.back(max(0.01, ig)) : 1 + 0.07 * sin(t * 2.1 + e.t)
            c.setFillColor(CGColor(srgbRed: 1, green: 244 / 255, blue: 228 / 255, alpha: 0.97)); star4(c, x, y, 3.9 * m * s); c.fillPath()
            c.setFillColor(CGColor(gray: 1, alpha: 1)); fillCircle(c, x, y, 0.9 * m * min(1, s))
            if ig < 1 { c.setStrokeColor(RGB.next.cg(0.6 * (1 - ig))); c.setLineWidth(0.9); star4(c, x, y, 4.4 * m); c.strokePath() }
        case .done:
            c.setStrokeColor(C_DONE.cg()); c.setLineWidth(1.5); strokeCircle(c, x, y, 3.1 * m * (ig < 1 ? 1 + 0.45 * (1 - ig) : 1))
        case .wait:
            c.setFillColor(C_WAIT.cg(0.82 + 0.18 * sin(t * 2.6))); fillCircle(c, x, y, 3.3 * m)
        case .work:
            c.saveGState(); c.translateBy(x: x, y: y); c.rotate(by: e.an + Double.pi / 2); c.scaleBy(x: m, y: m)
            let len = 6.6, hx = 1.7 + sin(t * 1.4) * 0.45
            c.setLineWidth(1.8); c.setLineCap(.round)
            c.move(to: CGPoint(x: hx - len, y: 0)); c.addLine(to: CGPoint(x: hx, y: 0)); c.replacePathWithStrokedPath(); c.clip()
            c.drawLinearGradient(linGradient([(0, RGB.work.cg(0)), (1, C_WORK.cg())]), start: CGPoint(x: hx - len, y: 0), end: CGPoint(x: hx, y: 0), options: [])
            c.resetClip()
            c.setFillColor(C_HEAD.cg()); fillCircle(c, hx, 0, 1.7)
            c.restoreGState()
        }
    }

    func nowMark(_ c: CGContext) {
        let nT = nowT(), v = visT(nT); if v < 0.02 { return }
        let an = ang(nT), cs = cos(an), sn = sin(an), off = smooth(6 * z, 40 * z, abs(nT - F))
        if off > 0.01 && cam == nil && Pxy((R_PS + 16) * cs, (R_PS + 16) * sn) {      // the hand of now, when you have turned away from it
            let x0 = PX, y0 = PY
            if Pxy((R_SC - 3) * cs, (R_SC - 3) * sn) {
                c.setAlpha(1); c.setStrokeColor(RGB.warm.cg(0.28 * off * v)); c.setLineWidth(1); c.setLineDash(phase: 0, lengths: [1.5, 4.5])
                c.beginPath(); c.move(to: CGPoint(x: x0, y: y0)); c.addLine(to: CGPoint(x: PX, y: PY)); c.strokePath(); c.setLineDash(phase: 0, lengths: [])
            }
        }
        if !P(R_SC, an) { return }
        let s = cam != nil ? clamp(PS, 0.5, 2) : 1
        c.setBlendMode(.plusLighter); spr.glow(c, .warm, PX, PY, 13 * s, 0.72 * v); c.setBlendMode(.normal)
        c.setAlpha(v); c.setFillColor(CGColor(srgbRed: 1, green: 248 / 255, blue: 234 / 255, alpha: 1)); fillCircle(c, PX, PY, 2.4 * s); c.setAlpha(1)
    }

    /// a hairline at the finger, and an index on the bezel like the meridian's
    func lensMark(_ c: CGContext) {
        let v = ls.value, cs = cos(lensTh), sn = sin(lensTh), r0 = R_HER - 10, r1 = R_BEZ + 2, rb = R_BEZ + 5
        c.setAlpha(1); c.setStrokeColor(RGB.white.cg(0.4 * v)); c.setLineWidth(0.8)
        c.beginPath(); c.move(to: CGPoint(x: CX + r0 * cs, y: CY + r0 * sn)); c.addLine(to: CGPoint(x: CX + r1 * cs, y: CY + r1 * sn)); c.strokePath()
        c.setFillColor(RGB.white.cg(0.9 * v)); c.beginPath()
        c.move(to: CGPoint(x: CX + (rb + 6) * cs - 4.2 * sn, y: CY + (rb + 6) * sn + 4.2 * cs)); c.addLine(to: CGPoint(x: CX + (rb + 6) * cs + 4.2 * sn, y: CY + (rb + 6) * sn - 4.2 * cs))
        c.addLine(to: CGPoint(x: CX + rb * cs, y: CY + rb * sn)); c.closePath(); c.fillPath()
    }

    // MARK: what moves with the hand: a faint streak, her listening, a star flying out

    /// a faint long exposure on the brightest stars, only while your finger flings the sky fast; sent somewhere by a button, it turns clean
    var trailAmount: Double { UIAccessibility.isReduceMotionEnabled || fl > 0.02 ? 0 : smooth(1, 3, abs(omegaS)) * (1 - mon) * hand }
    var fxBusy: Bool { trailAmount > 0.01 || !sonar.isEmpty || flyer != nil }

    func drawFx(_ c: CGContext, _ now: Double) {
        let ta = trailAmount
        c.setLineCap(.round)
        if ta > 0.01 { skyTrails(c, ta) }
        sonarRings(c, now); flyerDraw(c, now)
    }
    func skyTrails(_ c: CGContext, _ ta: Double) {
        let sweep = clamp(-omegaS * 0.14, -0.45, 0.45)
        var buckets: [[SkyStar]] = [[], [], [], []]
        for s in STARS where s.trail {
            let a = s.a + sky, y = CY + s.R * sin(a), x = CX + s.R * cos(a)
            if y > 610 || y < -30 || x < -30 || x > W + 30 { continue }
            buckets[min(3, Int((s.b * 4.4).rounded(.down)))].append(s)
        }
        for (bi, list) in buckets.enumerated() where !list.isEmpty {
            for half in [0, 1] {
                let p = CGMutablePath()
                for s in list {
                    let a = s.a + sky, a0 = a + sweep * (half == 1 ? 0.5 : 1), a1 = a + sweep * (half == 1 ? 0 : 0.5)
                    p.move(to: CGPoint(x: CX + s.R * cos(a0), y: CY + s.R * sin(a0)))
                    p.addArc(center: CGPoint(x: CX, y: CY), radius: s.R, startAngle: a0, endAngle: a1, clockwise: a1 < a0)
                }
                c.setLineWidth(0.55 + Double(bi) * 0.32); c.setStrokeColor(RGB.trail.cg((0.05 + 0.1 * Double(bi)) * (half == 1 ? 1 : 0.4) * ta))
                c.addPath(p); c.strokePath()
            }
        }
    }
    func sonarRings(_ c: CGContext, _ now: Double) {
        let (r, g, b) = herRGB, (sx, sy) = poleXY(), ss = mon > 0.5 ? 0.48 : herP.2
        for i in stride(from: sonar.count - 1, through: 0, by: -1) {
            let k2 = (now - sonar[i]) / 1500
            if k2 >= 1 { sonar.remove(at: i); continue }
            c.setStrokeColor(CGColor(srgbRed: CGFloat(r) / 255, green: CGFloat(g) / 255, blue: CGFloat(b) / 255, alpha: 0.3 * pow(1 - k2, 1.5))); c.setLineWidth(1.3)
            strokeCircle(c, sx, sy, (40 + (R_SC - 20) * Ease.out(k2)) * ss)
        }
    }
    func flyerDraw(_ c: CGContext, _ now: Double) {
        guard let f = flyer else { return }
        let k2 = clamp((now - f.at) / 1500, 0, 1), e = Ease.inOut(k2), r = laneR(f.e), an = ang(f.e.t)
        let (ox, oy) = poleXY()
        P(r, an)
        let tx = PX, ty = PY, sp = (1 - e) * 1.6, ax = ox + (tx - ox) * e, ay = oy + (ty - oy) * e
        let x = ax + (ay - oy) * sp * 0.25, y = ay - (ax - ox) * sp * 0.25
        c.setBlendMode(.plusLighter); spr.glow(c, RGB(255, 240, 214), x, y, 18, 0.95); c.setBlendMode(.normal)
        c.setAlpha(1); c.setFillColor(CGColor(gray: 1, alpha: 1)); star4(c, x, y, 5); c.fillPath()
        if k2 >= 1 {
            flyer = nil; f.e.born = now - 600; f.e.flash = now; EV.append(f.e); sound(.done, 1)
        }
    }

    // MARK: the month: a grid of daily star maps

    func miniSprite(_ d: Int) -> Sprite {
        let key = "\(d)"
        if let s = spr.minis[key] { return s }
        let w = ceil(GW), h = 62.0, cx = w / 2, cy = MR + 3
        let today = d == 0, future = d > 0
        let a: (Double) -> Double = { tm in Double.pi / 2 + (tm - 720) * TAU / DAY }
        let nT = nowT()
        let img = render(size: CGSize(width: w, height: h), scale: spr.px) { c in
            c.setLineCap(.round)
            func ring(_ r: Double, _ col: RGB, _ al: Double, _ wd: Double) { c.setStrokeColor(col.cg(al)); c.setLineWidth(wd); strokeCircle(c, cx, cy, r) }
            // daylight round the rim, noon at the bottom
            var stops: [(Double, RGB, Double)] = []
            for i in 0...24 { let (col, al) = sunAt(Double(i) * 60); stops.append((Double(i) / 24, col, al * (future ? 0.55 : 0.9))) }
            blit(c, conicRing(size: CGSize(width: w, height: h), scale: spr.px, center: CGPoint(x: cx, y: cy), radius: MR + 1.2, width: 1.6, startAngle: -Double.pi / 2, stops: stops), 0, 0, w, h)
            ring(MR - 1.5, .dial, today ? 0.3 : 0.13, 0.6)
            ring(MR * 0.5, .dial, 0.1, 0.6)
            if d <= 0 {                                              // the path you walked, rising where you moved
                let end = today ? nT - Double(floorDay(nT)) * DAY : DAY, p = CGMutablePath(), pd = CGMutablePath()
                var m = 0.0
                while m <= end {
                    let t = Double(d) * DAY + m, r = MR * 0.74 + 3.4 * actAt(t), an = a(m), x = cx + r * cos(an), y = cy + r * sin(an), sl = isQuiet(kindAt(t))
                    let pt = CGPoint(x: x, y: y)
                    if sl { if m != 0 { pd.addLine(to: pt) } else { pd.move(to: pt) }; p.move(to: pt) }
                    else { if m != 0 { p.addLine(to: pt) } else { p.move(to: pt) }; pd.move(to: pt) }
                    m += 10
                }
                c.setStrokeColor(RGB.lit.cg(0.62)); c.setLineWidth(0.9); c.addPath(p); c.strokePath()
                c.setStrokeColor(RGB.lit.cg(0.22)); c.setLineWidth(0.7); c.addPath(pd); c.strokePath()
            }
            let now = nowMs()
            let evs: [MiniEv] = monthDays(d) ?? EV.filter { floorDay($0.t) == d && $0.born <= now }.map { MiniEv(t: $0.t - Double(d) * DAY, lane: $0.lane, st: $0.st, star: ($0.mag ?? 1) > 1.2) }
            for e in evs {
                let r = e.lane == .her ? MR * 0.5 : MR * 0.74, an = a(e.t), x = cx + r * cos(an), y = cy + r * sin(an)
                let kd: Kd = e.st == .next ? .next : e.st == .done ? (e.lane == .you ? .lit : .done) : e.st == .wait ? .wait : .work
                if e.star && kd != .next {
                    c.setBlendMode(.plusLighter); self.spr.glow(c, .lit, x, y, 6, 0.7); c.setBlendMode(.normal)
                    c.setFillColor(CGColor(srgbRed: 1, green: 246 / 255, blue: 232 / 255, alpha: 1)); star4(c, x, y, 2.8); c.fillPath(); continue
                }
                if kd == .next { c.setStrokeColor(RGB.next.cg(e.star ? 0.8 : 0.45)); c.setLineWidth(0.7); star4(c, x, y, e.star ? 2.8 : 1.9); c.strokePath(); continue }
                let col: RGB = kd == .lit ? .lit : kd == .done ? .done : kd == .wait ? .wait : .work
                c.setFillColor(col.cg(kd == .done ? 0.75 : 0.95)); fillCircle(c, x, y, kd == .wait ? 1.4 : 1.1)
            }
            if today {
                let an = a(nT.truncatingRemainder(dividingBy: DAY))
                c.setBlendMode(.plusLighter); self.spr.glow(c, .warm, cx + MR * cos(an), cy + MR * sin(an), 5, 0.9); c.setBlendMode(.normal)
            }
            let (_, dn) = dateOf(d), wkend = isWeekend(d)
            if today {
                c.setFillColor(CGColor(srgbRed: 238 / 255, green: 241 / 255, blue: 1, alpha: 1))
                c.addPath(CGPath(roundedRect: CGRect(x: cx - 11, y: h - 15, width: 22, height: 14), cornerWidth: 7, cornerHeight: 7, transform: nil)); c.fillPath()
            }
            let f = WFont.mono(500, 10.5).ui, str = String(dn) as NSString
            let col = today ? UIColor(red: 11 / 255, green: 13 / 255, blue: 26 / 255, alpha: 1) : RGB.ink.ui(future ? 0.42 : wkend ? 0.56 : 0.72)
            let sz = str.size(withAttributes: [.font: f])
            str.draw(at: CGPoint(x: cx - sz.width / 2, y: h - 7.5 - f.lineHeight / 2), withAttributes: [.font: f, .foregroundColor: col])
        }
        let s = Sprite(img: img, w: w, h: h)
        spr.minis[key] = s
        return s
    }

    /// warms the month's day sprites one at a time while the sky is quiet, so opening the month does not draw 31 of them in one frame
    func warmMonth() {
        for d in M_FIRST...M_LAST where spr.minis["\(d)"] == nil { _ = miniSprite(d); return }
    }
}
