import SwiftUI
import MapKit

// What the footprint map draws: the route renderer (colour by speed, lit before the held minute and faint after it) and the marks, sized in points whatever the zoom.

// MARK: route

final class FootprintRouteRenderer: MKOverlayRenderer {
    private let route: FootprintMapRoute
    private let colors: [RGB]
    /// the segments that cross a gap: the dotted line stands for them
    private let cut: [Bool]
    var width: CGFloat
    /// the held minute: the route before it is lit, after it faint; nil lights it all
    var litMs: Int64?

    init(_ o: FootprintRoute, width: CGFloat) {
        route = o.route; colors = FootprintMapMath.pointColors(o.route.points); self.width = width
        let p = o.route.points
        cut = p.indices.dropLast().map { i in o.route.gaps.contains { $0.startMs <= p[i].ms && p[i + 1].ms <= $0.endMs } }
        super.init(overlay: o)
    }

    override func draw(_ mapRect: MKMapRect, zoomScale: MKZoomScale, in ctx: CGContext) {
        let k = 1 / zoomScale, tile = rect(for: mapRect).insetBy(dx: -(width + 4) * k, dy: -(width + 4) * k)
        var pts = route.points.map { point(for: MKMapPoint($0.coord)) }, colors = self.colors, cut = self.cut
        var lit = litMs.map { FootprintMapMath.litCount(route.points, ms: $0) } ?? pts.count
        // the held minute between two fixes: the line is lit as far as the dot, not only as far as the fix before it (a straight stretch has few fixes)
        if let t = litMs, lit > 0, lit < pts.count, !cut[lit - 1], route.points[lit].ms > route.points[lit - 1].ms {
            let f = Double(t - route.points[lit - 1].ms) / Double(route.points[lit].ms - route.points[lit - 1].ms), a = colors[lit - 1], b = colors[lit]
            pts.insert(CGPoint(x: lerp(pts[lit - 1].x, pts[lit].x, f), y: lerp(pts[lit - 1].y, pts[lit].y, f)), at: lit)
            colors.insert(RGB(lerp(a.r, b.r, f), lerp(a.g, b.g, f), lerp(a.b, b.b, f)), at: lit); cut.insert(false, at: lit); lit += 1
        }
        ctx.setLineCap(.round); ctx.setLineJoin(.round)

        for g in route.gaps {
            ctx.saveGState()
            ctx.setStrokeColor(FootprintPaint.gap.cg(FootprintPaint.gapAlpha * (litMs.map { g.startMs < $0 } ?? true ? 1 : Self.faint)))
            ctx.setLineWidth(2.2 * k); ctx.setLineDash(phase: 0, lengths: [0.1 * k, 5 * k])
            ctx.move(to: point(for: MKMapPoint(g.from))); ctx.addLine(to: point(for: MKMapPoint(g.to))); ctx.strokePath()
            ctx.restoreGState()
        }
        if lit < pts.count {                                    // after the held minute: thinner, no casing, flat at one alpha
            ctx.saveGState(); ctx.setAlpha(Self.faint); ctx.beginTransparencyLayer(auxiliaryInfo: nil)
            segments(max(lit - 1, 0)..<(pts.count - 1), pts, colors, cut, (width - 1.2) * k, tile, ctx)
            ctx.endTransparencyLayer(); ctx.restoreGState()
        }
        if lit > 1 {
            ctx.saveGState(); ctx.setAlpha(FootprintPaint.casingAlpha); ctx.beginTransparencyLayer(auxiliaryInfo: nil)
            ctx.setStrokeColor(FootprintPaint.casing.cg()); ctx.setLineWidth((width + 3.5) * k)
            for i in 0..<(lit - 1) where !cut[i] { ctx.move(to: pts[i]); ctx.addLine(to: pts[i + 1]) }
            ctx.strokePath()
            ctx.endTransparencyLayer(); ctx.restoreGState()
            segments(0..<(lit - 1), pts, colors, cut, width * k, tile, ctx)
        }
    }

    static let faint = 0.34

    /// each segment from its start colour to its end colour, clipped to the stroked segment so the round caps meet cleanly
    private func segments(_ range: Range<Int>, _ pts: [CGPoint], _ colors: [RGB], _ cut: [Bool], _ w: CGFloat, _ tile: CGRect, _ ctx: CGContext) {
        let space = CGColorSpace(name: CGColorSpace.sRGB)
        for i in range where i + 1 < pts.count && !cut[i] {
            let a = pts[i], b = pts[i + 1]
            guard tile.intersects(CGRect(x: min(a.x, b.x), y: min(a.y, b.y), width: abs(a.x - b.x), height: abs(a.y - b.y)).insetBy(dx: -w, dy: -w)) else { continue }
            let c0 = colors[i], c1 = colors[i + 1]
            ctx.saveGState()
            ctx.setLineWidth(w); ctx.move(to: a); ctx.addLine(to: b)
            if a == b || abs(c0.r - c1.r) + abs(c0.g - c1.g) + abs(c0.b - c1.b) < 6 {      // a blend nobody can see: one colour
                ctx.setStrokeColor(RGB((c0.r + c1.r) / 2, (c0.g + c1.g) / 2, (c0.b + c1.b) / 2).cg()); ctx.strokePath()
            } else {
                ctx.replacePathWithStrokedPath(); ctx.clip()
                if let g = CGGradient(colorsSpace: space, colors: [c0.cg(), c1.cg()] as CFArray, locations: nil) {
                    ctx.drawLinearGradient(g, start: a, end: b, options: [.drawsBeforeStartLocation, .drawsAfterEndLocation])
                }
            }
            ctx.restoreGState()
        }
    }
}

// MARK: marks

/// One mark on the map, drawn flat: a disc, a pin, a stop's token, the now dot or the scrub dot, each with an optional capsule word to its right.
final class FootprintMark: MKAnnotationView {
    enum Glyph: Equatable {
        case disc(CGFloat, RGB), pin(FootprintMapPlace.Pin, RGB), token(Int), now, dot(RGB)

        /// half the glyph's width in points (the pin counts its navy ring)
        var extent: CGFloat {
            switch self {
            case .disc(let r, _): return r + 1
            case .pin(let p, _): return p == .home ? 10 : 8.25
            case .token: return 9
            case .now: return 14
            case .dot: return 15
            }
        }
    }

    final class Note: NSObject, MKAnnotation {
        @objc dynamic var coordinate: CLLocationCoordinate2D
        private(set) var glyph: Glyph
        private(set) var label: String?
        let rank: Float, ms: Int64?
        init(_ c: CLLocationCoordinate2D, _ g: Glyph, label: String? = nil, rank: Float, ms: Int64? = nil) {
            coordinate = c; glyph = g; self.label = label; self.rank = rank; self.ms = ms
        }
        func set(_ g: Glyph, label: String?) { glyph = g; self.label = label }
    }

    private var note: Note?
    private static let font = UIFont.monospacedDigitSystemFont(ofSize: 12.5, weight: .medium)
    private static let capH: CGFloat = 20, gap: CGFloat = 4

    override init(annotation: MKAnnotation?, reuseIdentifier: String?) {
        super.init(annotation: annotation, reuseIdentifier: reuseIdentifier)
        backgroundColor = .clear; isOpaque = false; isEnabled = false; canShowCallout = false
        displayPriority = .required; collisionMode = .none
    }
    required init?(coder: NSCoder) { fatalError() }

    /// size the view to its glyph and word, and keep the glyph's centre on the coordinate
    func configure(_ n: Note) {
        note = n
        let e = n.glyph.extent, capW = n.label.map { Self.width($0) + 14 }
        let w = 2 * e + (capW.map { Self.gap + $0 } ?? 0), h = max(2 * e, capW == nil ? 0 : Self.capH)
        bounds = CGRect(x: 0, y: 0, width: w, height: h)
        centerOffset = CGPoint(x: w / 2 - e, y: 0)
        zPriority = MKAnnotationViewZPriority(rawValue: 100 + n.rank)
        setNeedsDisplay()
    }

    private static func width(_ s: String) -> CGFloat { ceil((s as NSString).size(withAttributes: [.font: font]).width) }

    override func draw(_ rect: CGRect) {
        guard let n = note else { return }
        let e = n.glyph.extent, c = CGPoint(x: e, y: bounds.midY)
        switch n.glyph {
        case .disc(let r, let tint):
            disc(c, r, tint, 0.13)
            tint.ui(0.5).setStroke()
            let ring = UIBezierPath(arcCenter: c, radius: r - 0.5, startAngle: 0, endAngle: 2 * .pi, clockwise: true); ring.lineWidth = 1; ring.stroke()
        case .pin(let pin, let tint):
            let home = pin == .home
            disc(c, e, FootprintPaint.navy, 1)
            disc(c, 8.25 - (home ? 0 : 1.75), home ? FootprintPaint.home : tint, 1)
            if home, let g = UIImage(systemName: "house.fill", withConfiguration: UIImage.SymbolConfiguration(pointSize: 8.5, weight: .bold))?
                .withTintColor(TLPaint.darkInk.ui(), renderingMode: .alwaysOriginal) {
                g.draw(at: CGPoint(x: c.x - g.size.width / 2, y: c.y - g.size.height / 2))
            }
        case .token(let k):
            disc(c, 8, FootprintPaint.token, 1)
            TLPaint.ink.ui(0.85).setStroke()
            let ring = UIBezierPath(arcCenter: c, radius: 7.375, startAngle: 0, endAngle: 2 * .pi, clockwise: true); ring.lineWidth = 1.25; ring.stroke()
            let s = NSAttributedString(string: "\(k)", attributes: [.font: UIFont.monospacedDigitSystemFont(ofSize: 9.5, weight: .medium), .foregroundColor: TLPaint.ink.ui()])
            s.draw(at: CGPoint(x: c.x - s.size().width / 2, y: c.y - s.size().height / 2))
        case .now:
            disc(c, 14, TLPaint.warm, 0.2); disc(c, 8, FootprintPaint.navy, 1); disc(c, 6, TLPaint.warm, 1)
        case .dot(let rgb):
            disc(c, 15, rgb, 0.22); disc(c, 8, RGB.white, 1); disc(c, 5.5, rgb, 1)
        }
        if let text = n.label {
            let w = Self.width(text) + 14, box = CGRect(x: 2 * e + Self.gap, y: bounds.midY - Self.capH / 2, width: w, height: Self.capH)
            let cap = UIBezierPath(roundedRect: box, cornerRadius: Self.capH / 2)
            FootprintPaint.navy.ui(0.82).setFill(); cap.fill()
            TLPaint.ink.ui(0.14).setStroke(); cap.lineWidth = 0.5; cap.stroke()
            let s = NSAttributedString(string: text, attributes: [.font: Self.font, .foregroundColor: TLPaint.ink.ui()])
            s.draw(at: CGPoint(x: box.minX + 7, y: box.midY - s.size().height / 2))
        }
    }

    private func disc(_ c: CGPoint, _ r: CGFloat, _ rgb: RGB, _ a: Double) {
        rgb.ui(a).setFill()
        UIBezierPath(arcCenter: c, radius: r, startAngle: 0, endAngle: 2 * .pi, clockwise: true).fill()
    }
}
