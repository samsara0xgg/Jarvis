import Foundation

// The follow camera's motion, pure numbers (ADR 0323's map). Its centre is the aim itself: what it follows (a finger's eased playhead, the replay) already moves smoothly, so the camera holds
// him at one place on the screen whether he moves or stands. A jump of the aim (a follow starting, letting go, the aim put elsewhere at once) goes into an offset that eases to nothing; heading,
// pitch and height (in log space) each ease on a critically damped spring, all of them on the slower flight spring until a jump has landed. The map's own types are `FootprintMap.swift`'s.

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

    /// the centre aimed at, in map points: followed exactly
    private(set) var aimX: Double, aimY: Double
    /// how far the centre stands from the aim, per metre of the larger of the height and the height aimed at: going down to a place, it comes onto the screen at an even pace instead of
    /// running off it while the camera drops (the offset in map points shrinks with the height); going back up, it is plain map points
    var offX = Spring(0), offY = Spring(0)
    var heading: Spring, pitch: Spring, logHeight: Spring
    /// the map's top margin in points, which puts the camera's centre (the middle of what the margins leave) below the fade: it eases with the rest, so that starting or ending a follow moves nothing at once
    var top = Spring(0)
    /// the spring while it follows
    var response = TLMotion.mapResponse
    /// from a jump until it has landed: every part on `TLMotion.mapFlight`
    private(set) var flying = false
    var omega: Double { 2 * .pi / (flying ? TLMotion.mapFlight : response) }

    init(x: Double, y: Double, heading: Double, pitch: Double, height: Double) {
        aimX = x; aimY = y; self.heading = Spring(heading); self.pitch = Spring(pitch); logHeight = Spring(log(height))
    }

    /// where the camera looks now, in map points
    var x: Double { aimX + offX.value * scale }
    var y: Double { aimY + offY.value * scale }
    private var scale: Double { max(height, exp(logHeight.target)) }

    /// The shortest way round from one heading to another, in degrees: -180...180.
    static func turn(from a: Double, to b: Double) -> Double {
        let t = (b - a).truncatingRemainder(dividingBy: 360)
        return t > 180 ? t - 360 : t < -180 ? t + 360 : t
    }

    /// New aims. The centre goes with the aim; `jump` keeps it where it stands and eases it over (a follow starting or ending). A nil heading keeps the one it has, and the heading goes the short
    /// way round from where it stands.
    mutating func aim(x tx: Double, y ty: Double, heading th: Double?, pitch tp: Double, height: Double, response r: Double, top margin: Double? = nil, jump: Bool = false) {
        let before = scale
        var ox = offX.value * before, oy = offY.value * before
        if jump { ox += aimX - tx; oy += aimY - ty; flying = true }
        aimX = tx; aimY = ty
        pitch.target = tp; logHeight.target = log(height); response = r
        if let margin { top.target = margin }
        if let th { heading.target = heading.value + Self.turn(from: heading.value, to: th) }
        let after = scale
        offX.value = ox / after; offY.value = oy / after
        offX.velocity *= before / after; offY.velocity *= before / after
    }

    mutating func step(dt: Double) {
        let w = omega
        for p in Self.parts { self[keyPath: p].step(dt: dt, omega: w) }
        if flying && landed { flying = false }
    }

    private static let parts: [WritableKeyPath<Self, Spring>] = [\.offX, \.offY, \.heading, \.pitch, \.logHeight, \.top]

    /// the aims taken at once
    mutating func cut() {
        for p in Self.parts { self[keyPath: p].value = self[keyPath: p].target; self[keyPath: p].velocity = 0 }
        flying = false
    }

    /// the jump's parts have arrived (the heading may still be turning after a moving aim)
    private var landed: Bool {
        let w = omega, s = scale
        return offX.settled(within: Self.epsPoints / s, omega: w) && offY.settled(within: Self.epsPoints / s, omega: w)
            && pitch.settled(within: Self.epsDegrees, omega: w) && logHeight.settled(within: Self.epsLog, omega: w) && top.settled(within: Self.epsPoints, omega: w)
    }

    var settled: Bool { !flying && landed && heading.settled(within: Self.epsDegrees, omega: omega) }

    /// 0..<360
    var headingDegrees: Double { (heading.value.truncatingRemainder(dividingBy: 360) + 360).truncatingRemainder(dividingBy: 360) }
    var height: Double { exp(logHeight.value) }
}
