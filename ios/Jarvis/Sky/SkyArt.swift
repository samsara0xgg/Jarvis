import UIKit
import CoreGraphics

// Things painted once and then only moved: the star field, glow sprites, words, the month's little maps.
// Every CGImage here is upright; `blit` draws one into the y-down design space the dial is drawn in.

@inline(__always) func blit(_ c: CGContext, _ img: CGImage, _ x: Double, _ y: Double, _ w: Double, _ h: Double) {
    c.saveGState()
    c.translateBy(x: x, y: y + h); c.scaleBy(x: 1, y: -1)
    c.draw(img, in: CGRect(x: 0, y: 0, width: w, height: h))
    c.restoreGState()
    c.restoreGState()
}

func render(size: CGSize, scale: CGFloat, _ body: (CGContext) -> Void) -> CGImage {
    let f = UIGraphicsImageRendererFormat.preferred()
    f.scale = scale; f.opaque = false; f.preferredRange = .standard
    return UIGraphicsImageRenderer(size: size, format: f).image { body($0.cgContext) }.cgImage!
}

struct Sprite { let img: CGImage; let w: Double; let h: Double }

struct WFont: Hashable {
    var size: CGFloat, weight: Int, mono: Bool
    static func sans(_ w: Int, _ s: CGFloat) -> WFont { WFont(size: s, weight: w, mono: false) }
    static func mono(_ w: Int, _ s: CGFloat) -> WFont { WFont(size: s, weight: w, mono: true) }
    var ui: UIFont {
        let w: UIFont.Weight = weight >= 600 ? .semibold : weight >= 500 ? .medium : .regular
        if mono { return Look.figuresUI(size, weight: w) }
        return .systemFont(ofSize: size, weight: w)
    }
}

final class SpriteCache {
    var px: CGFloat = 3
    private var glows: [RGB: CGImage] = [:]
    private var words: [String: Sprite] = [:]
    var minis: [String: Sprite] = [:]

    func clear() { glows.removeAll(); words.removeAll(); minis.removeAll() }

    func glow(_ col: RGB) -> CGImage {
        if let s = glows[col] { return s }
        let n = ceil(64 * px)
        let img = render(size: CGSize(width: n / px, height: n / px), scale: px) { c in
            let stops: [(CGFloat, Double)] = [(0, 1), (0.08, 0.8), (0.2, 0.42), (0.36, 0.17), (0.58, 0.05), (1, 0)]
            let g = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(), colors: stops.map { col.cg($0.1) } as CFArray, locations: stops.map { $0.0 })!
            let r = n / px / 2
            c.drawRadialGradient(g, startCenter: CGPoint(x: r, y: r), startRadius: 0, endCenter: CGPoint(x: r, y: r), endRadius: r, options: [])
        }
        glows[col] = img
        return img
    }

    /// a soft light, drawn additively by the caller's blend mode
    func glow(_ c: CGContext, _ col: RGB, _ x: Double, _ y: Double, _ r: Double, _ a: Double) {
        if a < 0.004 || r < 0.5 { return }
        c.setAlpha(CGFloat(min(1, a)))
        blit(c, glow(col), x - r, y - r, 2 * r, 2 * r)
    }

    func word(_ s: String, _ font: WFont, _ col: RGB, _ size: Double? = nil) -> Sprite {
        let key = "\(s)|\(font.size)|\(font.weight)|\(font.mono)|\(col.r),\(col.g),\(col.b)"
        if let w = words[key] { return w }
        let f = font.ui, sz = size ?? Double(font.size)
        let attrs: [NSAttributedString.Key: Any] = [.font: f, .foregroundColor: UIColor.white]
        let tw = ceil(Double((s as NSString).size(withAttributes: attrs).width)) + 6, th = ceil(sz * 1.6) + 2
        let tint = col.ui()
        let img = render(size: CGSize(width: tw, height: th), scale: px) { _ in
            let a: [NSAttributedString.Key: Any] = [.font: f, .foregroundColor: tint]
            let lh = f.lineHeight
            (s as NSString).draw(at: CGPoint(x: 3, y: th / 2 + 0.5 - lh / 2), withAttributes: a)
        }
        let w = Sprite(img: img, w: tw, h: th)
        words[key] = w
        return w
    }

    static func pad(_ size: Double) -> Double { ceil(0.3 * size) + 2 }
    /// a word with a soft dark halo round it, so a line that runs under it is hidden there; the text box is the sprite inset by `pad`.
    /// `more` > 0 ends it in " +N", the same font at 0.7 of its alpha: the others standing in the same place
    func label(_ s: String, _ font: WFont, _ col: RGB, more: Int = 0) -> Sprite {
        let key = "L|\(s)|\(more)|\(font.size)|\(font.weight)|\(font.mono)|\(col.r),\(col.g),\(col.b)"
        if let w = words[key] { return w }
        let f = font.ui, p = Self.pad(Double(font.size)), full = more > 0 ? "\(s) +\(more)" : s
        let tw = ceil(Double((full as NSString).size(withAttributes: [.font: f]).width)), th = ceil(Double(f.lineHeight))
        let w = tw + 2 * p, h = th + 2 * p
        let img = render(size: CGSize(width: w, height: h), scale: px) { c in
            c.setLineJoin(.round)           // so a CJK corner does not spike
            let sh = NSShadow(); sh.shadowBlurRadius = 0.25 * Double(font.size); sh.shadowOffset = .zero
            sh.shadowColor = UIColor(red: 7 / 255, green: 8 / 255, blue: 24 / 255, alpha: 0.9)
            let halo = UIColor(red: 7 / 255, green: 8 / 255, blue: 24 / 255, alpha: 0.8)
            (full as NSString).draw(at: CGPoint(x: p, y: p), withAttributes: [.font: f, .strokeColor: halo, .strokeWidth: 20])
            let at = NSMutableAttributedString(string: full, attributes: [.font: f, .foregroundColor: col.ui(), .shadow: sh])
            if more > 0 { at.addAttribute(.foregroundColor, value: col.ui(0.7), range: NSRange(location: s.utf16.count, length: full.utf16.count - s.utf16.count)) }
            at.draw(at: CGPoint(x: p, y: p))
        }
        let sp = Sprite(img: img, w: w, h: h)
        words[key] = sp
        return sp
    }

    /// 周四 10.8, for the week's dial
    func dayWord(_ d: Int, _ col: RGB) -> Sprite {
        let key = "day\(d)|\(col.r),\(col.g),\(col.b)"
        if let w = words[key] { return w }
        let a = dayName(d), (mo, da) = dateOf(d), b = "\(mo).\(da)"
        let fa = WFont.sans(600, 11).ui, fb = WFont.mono(500, 9.5).ui
        let wa = Double((a as NSString).size(withAttributes: [.font: fa]).width), wb = Double((b as NSString).size(withAttributes: [.font: fb]).width)
        let tw = ceil(wa + 5 + wb) + 6, th = 19.0
        let img = render(size: CGSize(width: tw, height: th), scale: px) { _ in
            (a as NSString).draw(at: CGPoint(x: 3, y: th / 2 + 0.5 - fa.lineHeight / 2), withAttributes: [.font: fa, .foregroundColor: col.ui()])
            (b as NSString).draw(at: CGPoint(x: 3 + wa + 5, y: th / 2 + 1 - fb.lineHeight / 2), withAttributes: [.font: fb, .foregroundColor: col.ui(0.62)])
        }
        let w = Sprite(img: img, w: tw, h: th)
        words[key] = w
        return w
    }
}

/// A four-pointed star of the prototype's quadratic curves.
func star4(_ c: CGContext, _ x: Double, _ y: Double, _ r: Double) {
    c.beginPath()
    c.move(to: CGPoint(x: x, y: y - r))
    c.addQuadCurve(to: CGPoint(x: x + r, y: y), control: CGPoint(x: x, y: y))
    c.addQuadCurve(to: CGPoint(x: x, y: y + r), control: CGPoint(x: x, y: y))
    c.addQuadCurve(to: CGPoint(x: x - r, y: y), control: CGPoint(x: x, y: y))
    c.addQuadCurve(to: CGPoint(x: x, y: y - r), control: CGPoint(x: x, y: y))
    c.closePath()
}

func fillCircle(_ c: CGContext, _ x: Double, _ y: Double, _ r: Double) {
    c.fillEllipse(in: CGRect(x: x - r, y: y - r, width: 2 * r, height: 2 * r))
}
func strokeCircle(_ c: CGContext, _ x: Double, _ y: Double, _ r: Double) {
    c.strokeEllipse(in: CGRect(x: x - r, y: y - r, width: 2 * r, height: 2 * r))
}

func radialFill(_ c: CGContext, _ x: Double, _ y: Double, _ rad: Double, _ stops: [(CGFloat, CGColor)]) {
    let g = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(), colors: stops.map { $0.1 } as CFArray, locations: stops.map { $0.0 })!
    c.drawRadialGradient(g, startCenter: CGPoint(x: x, y: y), startRadius: 0, endCenter: CGPoint(x: x, y: y), endRadius: rad, options: [])
}

// MARK: - the sky: a still field of stars and the Milky Way, painted once, turned by the compositor

func paintField(scale d: CGFloat) -> CGImage {
    let n = 2 * RS
    return render(size: CGSize(width: n, height: n), scale: d) { c in
        c.translateBy(x: RS, y: RS)
        var r = Rng(51)
        func gauss() -> Double { (r.next() + r.next() + r.next() + r.next() - 2) / 0.577 }
        c.setBlendMode(.plusLighter)
        for _ in 0..<320 {
            let s = (r.next() * 2 - 1) * RS * 1.1, o = 86 + gauss() * 50, rad = 20 + r.next() * 72
            let x = BU.0 * s - BU.1 * o, y = BU.1 * s + BU.0 * o
            let col = r.next() < 0.28 ? RGB(208, 176, 196) : RGB(116, 124, 224)
            let a = 0.016 + r.next() * 0.022
            radialFill(c, x, y, rad, [(0, col.cg(a)), (1, col.cg(0))])
        }
        c.setBlendMode(.destinationOut)      // lanes of dust along the band
        for _ in 0..<120 {
            let s = (r.next() * 2 - 1) * RS, o = 84 + gauss() * 14 + sin(s / 58) * 12, rad = 8 + r.next() * 24
            let x = BU.0 * s - BU.1 * o, y = BU.1 * s + BU.0 * o
            let a = 0.05 + r.next() * 0.09
            radialFill(c, x, y, rad, [(0, CGColor(gray: 0, alpha: a)), (1, CGColor(gray: 0, alpha: 0))])
        }
        c.setBlendMode(.normal)
        for s in STARS where !s.tw {
            let col = starRGB(s)
            if s.b > 0.62 {
                let gr = 2 + 5 * s.b
                radialFill(c, s.x, s.y, gr, [(0, col.cg(0.2 * s.b)), (1, col.cg(0))])
            }
            c.setFillColor(col.cg(0.1 + 0.72 * s.b))
            fillCircle(c, s.x, s.y, 0.28 + 0.78 * s.b)
        }
        for s in STARS.prefix(4) {          // the brightest wear diffraction spikes
            for (dx, dy) in [(1.0, 0.0), (0.0, 1.0)] {
                let L = 5 + 6 * s.b
                let g = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(), colors: [CGColor(srgbRed: 230 / 255, green: 236 / 255, blue: 1, alpha: 0),
                                                                                       CGColor(srgbRed: 230 / 255, green: 236 / 255, blue: 1, alpha: 0.36),
                                                                                       CGColor(srgbRed: 230 / 255, green: 236 / 255, blue: 1, alpha: 0)] as CFArray, locations: [0, 0.5, 1])!
                c.saveGState()
                c.setLineWidth(0.5)
                c.move(to: CGPoint(x: s.x - dx * L, y: s.y - dy * L)); c.addLine(to: CGPoint(x: s.x + dx * L, y: s.y + dy * L))
                c.replacePathWithStrokedPath(); c.clip()
                c.drawLinearGradient(g, start: CGPoint(x: s.x - dx * L, y: s.y - dy * L), end: CGPoint(x: s.x + dx * L, y: s.y + dy * L), options: [])
                c.restoreGState()
            }
        }
    }
}

/// A conic gradient ring computed pixel by pixel (CoreGraphics has none): angle 0 is the +x axis, growing clockwise on screen.
/// `stops` are (position 0...1, r, g, b, a) with straight (non-premultiplied) colour.
func conicRing(size: CGSize, scale: CGFloat, center: CGPoint, radius: Double, width: Double, startAngle: Double, stops: [(Double, RGB, Double)]) -> CGImage {
    let W = Int((size.width * scale).rounded(.up)), H = Int((size.height * scale).rounded(.up))
    var px = [UInt8](repeating: 0, count: W * H * 4)
    let cs = Double(scale), cx = Double(center.x) * cs, cy = Double(center.y) * cs, rr = radius * cs, hw = width * cs / 2
    for y in 0..<H {
        for x in 0..<W {
            let dx = Double(x) + 0.5 - cx, dy = Double(y) + 0.5 - cy
            let cov = clamp(hw + 0.5 - abs(hypot(dx, dy) - rr), 0, 1)
            if cov <= 0 { continue }
            var f = (atan2(dy, dx) - startAngle) / TAU
            f -= f.rounded(.down)
            var i = 0
            while i + 2 < stops.count && stops[i + 1].0 <= f { i += 1 }
            let a = stops[i], b = stops[min(i + 1, stops.count - 1)]
            let k = b.0 > a.0 ? clamp((f - a.0) / (b.0 - a.0), 0, 1) : 0
            let al = (a.2 + (b.2 - a.2) * k) * cov
            let o = (y * W + x) * 4
            px[o] = UInt8(clamp((a.1.r + (b.1.r - a.1.r) * k) * al, 0, 255))
            px[o + 1] = UInt8(clamp((a.1.g + (b.1.g - a.1.g) * k) * al, 0, 255))
            px[o + 2] = UInt8(clamp((a.1.b + (b.1.b - a.1.b) * k) * al, 0, 255))
            px[o + 3] = UInt8(clamp(al * 255, 0, 255))
        }
    }
    let ctx = CGContext(data: &px, width: W, height: H, bitsPerComponent: 8, bytesPerRow: W * 4, space: CGColorSpaceCreateDeviceRGB(),
                        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
    return ctx.makeImage()!
}
