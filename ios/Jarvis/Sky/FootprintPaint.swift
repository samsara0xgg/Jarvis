import Foundation

/// The 我的足迹 page's own data colours (ADR 0323), kept apart from `TLPaint` and `PlaceTint`
/// so a change there cannot move the speed ramp the map, curves and ribbons share.
enum FootprintPaint {
    /// Speed ramp stops in km/h; colours blend linearly between them, 80 and above holds the last.
    static let ramp: [(kmh: Double, rgb: RGB)] = [
        (0, RGB(240, 132, 218)), (25, RGB(172, 124, 255)), (50, RGB(100, 200, 255)), (80, RGB(111, 224, 180)),
    ]
    static let casing = RGB(8, 10, 24)              // under every route, at casingAlpha
    static let casingAlpha = 0.55
    static let gap = RGB(214, 220, 246)             // a stretch with no fixes: dotted, straight, at gapAlpha
    static let gapAlpha = 0.75
    static let token = RGB(16, 18, 30)              // stop token fill; its ring is TLPaint.ink at 0.85
    static let heart = RGB(236, 128, 140)           // the heart-rate curve
    static let home = RGB(255, 168, 150)            // home pin, as PlaceTint's home line
    static let navy = RGB(11, 13, 26)               // ring under pins and dots

    /// The ramp colour at a speed.
    static func speed(kmh: Double) -> RGB {
        guard let first = ramp.first, let last = ramp.last else { return TLPaint.ink }
        if kmh <= first.kmh { return first.rgb }
        if kmh >= last.kmh { return last.rgb }
        let i = ramp.firstIndex { $0.kmh > kmh }!
        let a = ramp[i - 1], b = ramp[i]
        let f = (kmh - a.kmh) / (b.kmh - a.kmh)
        return RGB(a.rgb.r + (b.rgb.r - a.rgb.r) * f, a.rgb.g + (b.rgb.g - a.rgb.g) * f, a.rgb.b + (b.rgb.b - a.rgb.b) * f)
    }

    static func speed(mps: Double) -> RGB { speed(kmh: mps * 3.6) }
}
