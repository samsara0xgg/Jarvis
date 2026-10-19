import UIKit

// The middle of the home screen is the dial or the timeline (Sky/HomeModule.swift). This is the engine's part of the change from one to the other:
// a spring from 0 (the dial) to 1 (the timeline) that the stage reads to glide her up to the corner and fade the dial out, and what the engine stops doing
// once the dial is out of sight.

extension StarEngine {
    /// 0 the dial, 1 the timeline
    var tm: Double { clamp(tms.value, 0, 1) }
    /// the timeline is all there is: the dial, the month and the trails are not drawn, nor drawn again until it gives way
    var timelineFull: Bool { tm > 0.999 }

    /// every frame
    func syncModule(_ dt: Double) {
        let kind = HomeModule.shared.kind
        if kind != moduleSeen {
            moduleSeen = kind
            wake()
            if kind == .timeline {
                if qGoal != 0 { setLevel(0) }
                ui.tuning = false
                ui.tl.letGo(); ui.tl.day = ui.tl.today
                ui.tl.open()                    // the bar grows in and the rows rise once the day is there
            } else {
                ui.tl.letGo()                   // a rename's field open when it was left must not keep the composer away from the dial
                rowKey = ""; viewKey = ""; dateD = nil          // the panel under the dial and the date are built again
            }
        }
        let goal = kind == .timeline ? 1.0 : 0.0
        crit(&tms, goal, UIAccessibility.isReduceMotionEnabled ? 30 : 7.5, dt)
        if abs(tms.value - goal) < 0.0005 && abs(tms.velocity) < 0.001 { tms.value = goal; tms.velocity = 0 }
        put(\.tlShow, kind == .timeline || tms.value > 0.001)
        put(\.tlHome, kind == .timeline && (1 - tm) * hypot(TLLayout.her.x - CX, TLLayout.her.y - CY) < 20)
        if timelineFull {
            // nothing draws the trails any more, and they are only removed when drawn
            sonar.removeAll()
            if let f = flyer { f.e.born = nowMs() - 600; f.e.flash = nowMs(); EV.append(f.e); flyer = nil }
        }
    }
}
