import SwiftUI

// Where her eyes look in the timeline (docs: the day timeline's design, section 5). While a finger is on the bar they look toward it, and come back to him when it lifts; when a row
// opens they go to it once. While a finger slides over her cards they follow it from card to card, and while he types they are on the field. Her eyes could already look anywhere
// (HerCore: a gaze held for as long as it is given, which the dial gives while it spins, and a glance of so many milliseconds, which a tap on a row of the dial gives); this is only where
// they are asked to look, worked out from where she is (Sky/DayTimelineSeat.swift).

enum TLGaze {
    /// how long a glance at a row lasts, and how long her eyes take to find him again when a finger lifts (ms)
    static let glance = 700.0, back = 650.0

    /// where her eyes look (-1...1 across, y down) to see a point of the chrome from where she stands: the way to it, turned 0.8 at the most to a side and 0.55 down, which is as far as her eyes go
    /// without the iris leaving the disc
    static func toward(_ p: CGPoint, from her: CGPoint = TLSeat.home) -> (Double, Double) {
        let dx = Double(p.x - her.x), dy = Double(p.y - her.y), d = hypot(dx, dy)
        guard d > 1 else { return (0, 0) }
        return (clamp(0.8 * dx / d, -0.8, 0.8), clamp(0.8 * dy / d, -0.5, 0.55))
    }

    /// where a finger at `x` on the bar is, in the chrome's design points
    static func bar(x: Double) -> CGPoint { CGPoint(x: 30 + clamp(x, 0, TLLayout.barW), y: TLLayout.barY + TLLayout.barH / 2) }

    /// where a row of the list is, for her eyes: the beginning of its words, half way down its line (`top` and `height` as the list lays it out, `scrolled` how far the list has gone up)
    static func row(top: Double, height: Double, scrolled y: Double) -> CGPoint {
        CGPoint(x: 30 + TLColumns.text + 60, y: TLLayout.listTop + top + height / 2 - y)
    }
}

extension StarEngine {
    /// where her eyes are asked to look: at a finger on the bar, at the card a finger is on, at the field while he types (none: nowhere in particular)
    var timelineGaze: (Double, Double)? {
        guard ui.tlHome else { return nil }
        if let p = ui.tl.finger { return TLGaze.toward(p, from: seat) }
        if let p = ui.tl.arcHoverAt { return TLGaze.toward(p, from: seat) }
        if ui.typing { return TLGaze.toward(CGPoint(x: W / 2, y: H - StarUI.restGap - 48 - ui.footLift), from: seat) }
        return nil
    }

    /// every frame: her eyes were on his finger and it has lifted (or the corner was left): they come back to him
    func syncGaze() {
        let looking = timelineGaze != nil
        guard looking != gazing else { return }
        gazing = looking
        if !looking { herDrive?.herLookAt((0, -0.03), now: nowMs(), ms: TLGaze.back) }
    }

    /// a row of the list opens: her eyes go to it once and come back
    func glance(at p: CGPoint) {
        guard ui.tlHome else { return }
        herDrive?.herLookAt(TLGaze.toward(p, from: seat), now: nowMs(), ms: TLGaze.glance)
    }
}
