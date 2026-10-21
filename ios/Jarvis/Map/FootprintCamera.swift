import Foundation

// The follow camera's motion, pure numbers (ADR 0323's map): every part of it (centre in map points, heading, pitch, height in log space) is one critically damped spring, stepped by the
// display link a frame at a time. A target that moves under it leaves its speed where it is, so nothing in the camera jumps. The map's own types are `FootprintMap.swift`'s.

struct FootprintCamera {
    /// x'' = w^2 (target - x) - 2 w x', stepped by its exact solution so that the frame length does not matter; `response` is the undamped period, as SwiftUI's spring has it (w = 2 pi / response)
    struct Spring {
        var value: Double, velocity = 0.0, target: Double
        init(_ v: Double) { value = v; target = v }

        mutating func step(dt: Double, omega w: Double) {
            let d = value - target, b = velocity + w * d, e = exp(-w * dt)
            value = target + (d + b * dt) * e
            velocity = (b - w * (d + b * dt)) * e
        }
        func settled(within eps: Double, omega w: Double) -> Bool { abs(value - target) < eps && abs(velocity) < eps * w }
    }

    /// settled when each part is this close and slow: half a map point (a few centimetres), a twentieth of a degree, a thousandth of the height
    static let epsPoints = 0.5, epsDegrees = 0.05, epsLog = 0.001

    var x: Spring, y: Spring, heading: Spring, pitch: Spring, logHeight: Spring
    /// the map's top margin in points, which puts the camera's centre (the middle of what the margins leave) below the fade: it eases with the rest, so that starting or ending a follow moves nothing at once
    var top = Spring(0)
    var response = TLMotion.mapResponse
    var omega: Double { 2 * .pi / response }

    init(x: Double, y: Double, heading: Double, pitch: Double, height: Double) {
        self.x = Spring(x); self.y = Spring(y); self.heading = Spring(heading); self.pitch = Spring(pitch); logHeight = Spring(log(height))
    }

    /// The shortest way round from one heading to another, in degrees: -180...180.
    static func turn(from a: Double, to b: Double) -> Double {
        let t = (b - a).truncatingRemainder(dividingBy: 360)
        return t > 180 ? t - 360 : t < -180 ? t + 360 : t
    }

    /// new aims; a nil heading keeps the one it has, and the heading goes the short way round from where it stands
    mutating func aim(x tx: Double, y ty: Double, heading th: Double?, pitch tp: Double, height: Double, response r: Double, top margin: Double? = nil) {
        x.target = tx; y.target = ty; pitch.target = tp; logHeight.target = log(height); response = r
        if let margin { top.target = margin }
        if let th { heading.target = heading.value + Self.turn(from: heading.value, to: th) }
    }

    mutating func step(dt: Double) {
        let w = omega
        x.step(dt: dt, omega: w); y.step(dt: dt, omega: w); heading.step(dt: dt, omega: w); pitch.step(dt: dt, omega: w); logHeight.step(dt: dt, omega: w); top.step(dt: dt, omega: w)
    }

    /// the aims taken at once
    mutating func cut() {
        for p in [\Self.x, \.y, \.heading, \.pitch, \.logHeight, \.top] { self[keyPath: p].value = self[keyPath: p].target; self[keyPath: p].velocity = 0 }
    }

    var settled: Bool {
        let w = omega
        return x.settled(within: Self.epsPoints, omega: w) && y.settled(within: Self.epsPoints, omega: w) && heading.settled(within: Self.epsDegrees, omega: w)
            && pitch.settled(within: Self.epsDegrees, omega: w) && logHeight.settled(within: Self.epsLog, omega: w) && top.settled(within: Self.epsPoints, omega: w)
    }

    /// 0..<360
    var headingDegrees: Double { (heading.value.truncatingRemainder(dividingBy: 360) + 360).truncatingRemainder(dividingBy: 360) }
    var height: Double { exp(logHeight.value) }
}
