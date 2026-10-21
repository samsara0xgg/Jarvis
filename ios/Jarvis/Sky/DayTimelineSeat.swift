import UIKit

// Where she is on the timeline. The sky above the bar is hers (yilun, 2026-10-21): she sits in it beside the date, under the right end of the island and bigger than she was in the corner,
// and she moves inside it, sometimes behind its words. She leans in while he speaks to her; when nothing happens for a while she drifts down to where her sentence is, behind it, and back;
// for a talk, her answer and her cards (which always come out round her home) she is home; while one of the footprint's pages is open she goes to the corner those pages are laid out
// around. The engine moves her there on springs every frame (`stepSeat`), and what she is touched at and where her eyes look from read the same place. Everything is in the chrome's
// 440 x 956 design points.

/// her places on the timeline, and how big she is in each
enum TLSeat {
    /// At rest: in the top row, right of the date and its weather, with the row's right end clear (the map and settings are in the bar at the foot); her top 9 pt under the island.
    static let home = CGPoint(x: 288, y: 79)
    static let radius = 22.0
    /// the corner of the top row that the footprint's pages leave her (Sky/FootprintDayPage.swift), at the size she had there
    static let corner = CGPoint(x: 47, y: 79), cornerRadius = 15.0
    /// how much bigger she is while she listens
    static let listening = 1.12
    /// a finger this far outside her disc is still on her
    static let slop = 8.0
    /// Where she drifts to when nothing happens: down where her sentence is, behind its words (she may hide behind words, yilun said), at its right end, past its middle,
    /// and toward its left; under the top row, inside the screen's margins, and 12 pt over the bar.
    static let perches = [CGPoint(x: 388, y: 134), CGPoint(x: 236, y: 140), CGPoint(x: 112, y: 142)]
    /// how long nothing has to happen before she drifts (ms), how long she stays off, and how long she takes back home after (as slowly as she went)
    static let calmFor = 25_000.0...60_000.0, offFor = 6_000.0...11_000.0, backFor = 4_000.0
}

extension StarEngine {
    /// where she is on the timeline now
    var seat: CGPoint { CGPoint(x: seatX.value, y: seatY.value) }
    /// her size there, as a scale of her size on the dial
    var seatScale: Double { seatR.value / R_PS }
    /// a finger within this of her middle is on her
    var seatReach: Double { seatR.value + TLSeat.slop }

    /// nothing is happening on the timeline: no talk going on, no word of hers still coming and none on show in her sentence's place (she does not go behind her own answer), no finger
    /// on her or the bar, her cards in, no keyboard, no row, page or sheet open, the brain not tidying
    private var calm: Bool {
        talk == nil && speaking == nil && voiceState == .idle && !ui.tlTalk && g == nil && ui.tl.finger == nil && !ui.tl.arcMounted && !ui.typing && !ui.panelOpen && !ui.settingsOpen
            && ui.tl.openRow == nil && ui.tl.tidy != .running
    }

    /// Every frame: where she should be, and the springs that take her there. Off on a drift she stays where she is while anything happens (a touch on her there is a touch on her), and
    /// drifts home a moment after it is over. She hurries home on her way back when anything happens, at once for her cards (they come out round her home), and once his finger is off
    /// her for a talk, his voice or hers, or her answer on show. She drifts only on the timeline, and never under Reduce Motion or VoiceOver.
    func stepSeat(_ now: Double, _ dt: Double) {
        let still = UIAccessibility.isReduceMotionEnabled || UIAccessibility.isVoiceOverRunning
        if !HomeModule.shared.isTimeline || still || ui.footprintOpen {
            wander = nil; seatCalmSince = now
        } else if !calm {
            seatCalmSince = now
            let hers = ui.tl.arcMounted || (g == nil && (talk != nil || speaking != nil || voiceState != .idle || ui.tlTalk))
            if let w = wander { if w.to == TLSeat.home || hers { wander = nil } else { wander = (w.to, max(w.until, now + 1500)) } }
        } else if let w = wander {
            if now > w.until {
                if w.to == TLSeat.home { wander = nil } else {
                    wander = (TLSeat.home, now + TLSeat.backFor)
                    herDrive?.herLookAt(TLGaze.toward(TLSeat.home, from: seat), now: now, ms: 1100)          // and where she goes back to
                }
                seatCalmSince = now
            }
        } else if now - seatCalmSince > seatWait {
            let to = TLSeat.perches.randomElement() ?? TLSeat.home          // she is home: a drift starts only after a while of nothing
            wander = (to, now + Double.random(in: TLSeat.offFor))
            seatWait = Double.random(in: TLSeat.calmFor)
            herDrive?.herLookAt(TLGaze.toward(to, from: seat), now: now, ms: 1100)          // she looks where she is going
        }
        let listening = voiceState == .listening || talk == "listen"
        let goal = ui.footprintOpen ? TLSeat.corner : wander?.to ?? TLSeat.home
        let r = ui.footprintOpen ? TLSeat.cornerRadius : TLSeat.radius * (listening ? TLSeat.listening : 1)
        let w = still ? 30.0 : wander != nil ? 2.4 : 6.5          // a drift, and the way back from it, is slow; hurrying home, or going to the corner, is quick
        crit(&seatX, goal.x, w, dt); crit(&seatY, goal.y, w, dt); crit(&seatR, r, 9, dt)
        seatMoving = abs(seatX.value - goal.x) > 0.05 || abs(seatY.value - goal.y) > 0.05 || abs(seatR.value - r) > 0.02
            || abs(seatX.velocity) > 0.05 || abs(seatY.velocity) > 0.05 || abs(seatR.velocity) > 0.02
    }
}
