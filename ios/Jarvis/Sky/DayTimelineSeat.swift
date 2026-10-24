import UIKit

// Where she is on the phone's home (跨端UI标准 §1). She floats over every page and takes no room of it: on the four pages of the bar at the foot (今天, 聊天, 地图, 设置) she sits top centre and
// stays there across tab switches; on a page stepped into (a trip of the map) she is smaller at the top right, with the way back at the left. She leans in while he speaks to her. The engine
// moves her there on springs every frame (`stepSeat`), so a change of page glides her; what she is touched at and where her eyes look from read the same place. Everything is in the
// chrome's 440 x 956 design points.

/// her places on the home, and how big she is in each
enum TLSeat {
    /// At rest on the four pages: the middle of the top, under the island, the date's line (Sky/HomeTop.swift) 8 pt under her disc
    static let home = CGPoint(x: 220, y: 83)
    static let radius = 26.0
    /// On a page stepped into (a trip), smaller at the top right
    static let corner = CGPoint(x: 400, y: 88), cornerRadius = 20.0
    /// The conversation's page is one of the four: her seat there is the home one
    static let talk = home, talkRadius = radius
    /// how much bigger she is while she listens
    static let listening = 1.12
    /// a finger this far outside her disc is still on her
    static let slop = 8.0
    /// no longer a drift of hers; kept so the debug scene `飘` (Sky/StarTimelineScene.swift) still builds, and goes with it
    static let perches: [CGPoint] = []
}

extension StarEngine {
    /// where she is on the home now
    var seat: CGPoint { CGPoint(x: seatX.value, y: seatY.value) }
    /// her size there, as a scale of her size on the dial
    var seatScale: Double { seatR.value / R_PS }
    /// a finger within this of her middle is on her
    var seatReach: Double { seatR.value + TLSeat.slop }
    /// a page stepped into is on show: a trip of the map
    var pushedPage: Bool { ui.footprintOpen && ui.footprint.openOuting != nil }
    /// where she rests on the page on show, and how big she is there: the top right on a page stepped into, else the middle of the top
    var restSeat: (at: CGPoint, r: Double) {
        pushedPage ? (TLSeat.corner, TLSeat.cornerRadius) : (TLSeat.home, TLSeat.radius)
    }

    /// A hold on her, or a touch that slides off her: 聊天 with the field taking the keyboard (the field is on that page only)
    func typeToHer() {
        Haptic.impact(.medium)
        herDrive?.herEffect("ripple", now: nowMs())
        beginEditing?()
    }

    /// Every frame: the springs that take her to where she rests (a bigger disc while she listens, except on a page stepped into). Under Reduce Motion or VoiceOver she is there at once.
    func stepSeat(_ now: Double, _ dt: Double) {
        let still = UIAccessibility.isReduceMotionEnabled || UIAccessibility.isVoiceOverRunning
        let listening = voiceState == .listening || talk == "listen"
        let rest = restSeat
        let r = rest.r * (listening && !pushedPage ? TLSeat.listening : 1)
        let w = still ? 30.0 : 6.5
        crit(&seatX, rest.at.x, w, dt); crit(&seatY, rest.at.y, w, dt); crit(&seatR, r, 9, dt)
        seatMoving = abs(seatX.value - rest.at.x) > 0.05 || abs(seatY.value - rest.at.y) > 0.05 || abs(seatR.value - r) > 0.02
            || abs(seatX.velocity) > 0.05 || abs(seatY.velocity) > 0.05 || abs(seatR.velocity) > 0.02
    }
}
