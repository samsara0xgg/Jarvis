import SwiftUI

// 「等你 N」 (跨端UI标准 §1): the amber number on her disc, top right, as proto/today.html's badge. It follows her seat and her size every frame (she is drawn on the stage, not here), grows in
// and out of her in 0.3 s without a bounce, and a tap on it goes to 今天 and asks that page for the first of what waits (`TimelineState.waitingTap`). Design points, as the chrome.

struct HerBadge: View {
    let engine: StarEngine
    let ui: StarUI
    @Environment(\.accessibilityReduceMotion) private var reduce
    var seat: (at: CGPoint, r: Double)?     // where her middle is and her radius, in this view's W x H, where she is not on the phone's stage (the iPad's her)
    @State private var shown = 0            // the last number, kept while the badge grows back into her

    private static let ink = Color(red: 27 / 255, green: 19 / 255, blue: 5 / 255)           // #1B1305
    /// where its middle is on her disc of radius 27 in the prototype (23.5 right of her middle, 18.5 up), and the disc it was drawn on
    private static let at = CGPoint(x: 23.5, y: -18.5), disc = 27.0

    var body: some View {
        let n = ui.tl.waitingCount, on = n > 0 && HomeModule.shared.isTimeline && ui.tlHome
        TimelineView(.animation(paused: !on && shown == 0)) { _ in
            let c = seat?.at ?? engine.seat, s = (seat?.r ?? engine.seatR.value) / Self.disc
            Button {
                engine.showPage(.day)
                ui.tl.waitingTap += 1
            } label: {
                Text("\(shown)").font(Look.figures(12, weight: .semibold)).foregroundStyle(Self.ink)
                    .padding(.horizontal, 5).frame(minWidth: 19, minHeight: 19)
                    .background(Capsule().fill(Look.waits))
                    .background(Capsule().fill(Look.sky).padding(-2))          // the ring of the sky round it
                    .padding(6).contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .scaleEffect(s * (on || reduce ? 1 : 0.2))
            .opacity(on ? 1 : 0)
            .animation(.easeOut(duration: 0.3), value: on)
            .allowsHitTesting(on)
            .accessibilityHidden(!on)
            .accessibilityLabel(Words.Home.waitingSpoken(shown))
            .position(x: c.x + Self.at.x * s, y: c.y + Self.at.y * s)
        }
        .frame(width: W, height: H, alignment: .topLeading)
        .onChange(of: n, initial: true) { _, v in if v > 0 { shown = v } }
    }
}
