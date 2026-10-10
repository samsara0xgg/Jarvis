import UIKit

/// The few taps the star chart gives: listening starts, her first words, the talk panel snapping, a card answered, a trouble shown.
enum Haptic {
    static func impact(_ style: UIImpactFeedbackGenerator.FeedbackStyle) { MainActor.assumeIsolated { UIImpactFeedbackGenerator(style: style).impactOccurred() } }
    static func selection() { MainActor.assumeIsolated { UISelectionFeedbackGenerator().selectionChanged() } }
    static func notify(_ type: UINotificationFeedbackGenerator.FeedbackType) { MainActor.assumeIsolated { UINotificationFeedbackGenerator().notificationOccurred(type) } }
}
