#if DEBUG
import SwiftUI

/// `-scene footprintTrip`: the outing page full screen on the made-up day's drive; `-scene footprintTrip:held` with a finger held on it at 55% of the way, where the drive is fastest.
enum FootprintTripScene {
    @MainActor static func present(held: Bool) {
        guard var top = UIApplication.shared.connectedScenes.compactMap({ ($0 as? UIWindowScene)?.keyWindow }).first?.rootViewController else { return }
        while let next = top.presentedViewController { top = next }
        let cal = Cal.cur.cal
        let model = FootprintModel(reply: FootprintFixture.footprint(cal), day: FootprintFixture.day(cal))
        guard let drive = model.reply.outings.first else { return }
        let page = held ? FootprintOutingPage(model: model, outing: drive, held: drive.start_ms + Int64(Double((drive.end_ms ?? drive.start_ms) - drive.start_ms) * 0.55)) : FootprintOutingPage(model: model, outing: drive, back: {})
        let host = UIHostingController(rootView: ZStack(alignment: .topLeading) { TLPaint.ground.color; page }.ignoresSafeArea())
        host.modalPresentationStyle = .fullScreen
        host.overrideUserInterfaceStyle = .dark
        top.present(host, animated: false)
    }
}
#endif
