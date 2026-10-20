import UIKit

/// The few taps the star chart gives: listening starts, her first words, the talk panel snapping, a card answered, a trouble shown. The generators are kept and prepared again after each tap
/// (a new one for every tap has the Taptic Engine wake up for it); a selection tick is not repeated within 40 ms, which a finger crossing stretches of the bar could ask for.
enum Haptic {
    private static var impacts: [UIImpactFeedbackGenerator.FeedbackStyle: UIImpactFeedbackGenerator] = [:]
    private static let ticker = UISelectionFeedbackGenerator()
    private static let notifier = UINotificationFeedbackGenerator()
    private static var lastTick = 0.0

    static func impact(_ style: UIImpactFeedbackGenerator.FeedbackStyle) {
        MainActor.assumeIsolated {
            let g = impacts[style] ?? UIImpactFeedbackGenerator(style: style)
            impacts[style] = g
            g.impactOccurred(); g.prepare()
        }
    }
    static func selection() {
        MainActor.assumeIsolated {
            let now = CACurrentMediaTime()
            guard now - lastTick >= 0.04 else { return }
            lastTick = now
            ticker.selectionChanged(); ticker.prepare()
        }
    }
    static func notify(_ type: UINotificationFeedbackGenerator.FeedbackType) { MainActor.assumeIsolated { notifier.notificationOccurred(type); notifier.prepare() } }
    /// a finger is down where a tick is about to come (a row, the bar): have the engine ready for it
    static func prepare() { MainActor.assumeIsolated { ticker.prepare() } }
}
