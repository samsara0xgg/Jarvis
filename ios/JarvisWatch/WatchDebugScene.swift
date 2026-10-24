#if DEBUG
import SwiftUI

/// Debug only: `-watchScene <name>` puts the app in one state with a made-up day (WatchDaySample), for screenshots: the home (rest, waiting, aod), a talk
/// (listen, think, answer), a card (reminder, leave), a page (today, ride), and the complications' gallery.
enum WatchScene: String {
    case home, waiting, aod, listen, think, answer, reminder, leave, today, ride, complications

    static var current: WatchScene? { UserDefaults.standard.string(forKey: "watchScene").flatMap(WatchScene.init(rawValue:)) }

    /// adopt the sample day (with nothing waiting where the board's her is at rest), and make the talk buttons play a small talk of their own (there is no phone here)
    @MainActor static func apply() {
        guard let scene = current else { return }
        let m = WatchModel.shared
        switch scene {
        case .waiting, .reminder, .today, .ride, .complications: m.adopt(.sample)
        case .leave: m.adopt(.sampleLeaving)
        default: m.adopt(.sampleQuiet)
        }
        m.startTalk = { m.talk = .listening(heard: "") }
        m.endTalk = { m.talk = .thinking(heard: WatchDay.sampleHeard) }
        m.cancelTalk = { m.talk = .idle }
        switch scene {
        case .listen: m.talk = .listening(heard: WatchDay.sampleHeard)
        case .think: m.talk = .thinking(heard: WatchDay.sampleHeard)
        case .answer: m.talk = .answer(spoken: WatchDay.sampleSpoken, written: WatchDay.sampleWritten, card: WatchDay.sampleCard)
        default: break
        }
    }
}

struct WatchDebugHost: View {
    var body: some View {
        switch WatchScene.current {
        case .complications: WatchComplicationGallery()
        case .reminder: WatchRoot(card: WatchDay.sample.waiting[0].id)
        case .today: WatchRoot(page: .today)
        case .ride: WatchRoot(page: .ride)
        case .aod: WatchRoot().transformEnvironment(\.isLuminanceReduced) { $0 = true }
        default: WatchRoot()
        }
    }
}

/// every complication family's view at its size, on a dark card
struct WatchComplicationGallery: View {
    var body: some View {
        let day = WatchDay.sample
        let now = WatchRing.nowMinute(day, at: .now)
        ScrollView {
            VStack(alignment: .leading, spacing: 8) {
                HStack(alignment: .top, spacing: 8) {
                    card("circular", w: 52, h: 52) { CircularComplication(day: day, now: now) { HerSprite(face: .rest, size: 30) } }
                    card("corner", w: 100, h: 52) {
                        HStack { CornerComplication(day: day) { HerSprite(face: .rest, size: 30) }.frame(width: 40, height: 40); Text(complicationLine(day)).font(.system(size: 12)).foregroundStyle(WatchTheme.ink) }
                    }
                }
                card("rectangular", w: 160, h: 56) { RectangularComplication(day: day) }
                card("inline", w: 170, h: 22) { InlineComplication(day: day) }
            }
        }
    }

    private func card<V: View>(_ name: String, w: CGFloat, h: CGFloat, @ViewBuilder _ v: () -> V) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(name).font(.system(size: 9)).foregroundStyle(WatchTheme.muted)
            v().frame(width: w, height: h).background(Color.white.opacity(0.06), in: RoundedRectangle(cornerRadius: 10))
        }
    }
}
#endif
