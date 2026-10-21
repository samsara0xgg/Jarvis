import UIKit

/// What this device is to the brain (docs/adr/0336). The phone goes where he goes, so it reports what it senses: where he is, how he moves, Health, focus,
/// calls and the day's recording. An iPad stays where it was left and is only a screen to look and talk on: it senses nothing, so "here" (ADR 0212)
/// never moves to wherever the iPad lies, and Health that iCloud syncs to it from the phone is never sent twice.
enum DeviceRole {
    /// this device reports what it senses
    static let senses = senses(on: UIDevice.current.userInterfaceIdiom)
    static func senses(on idiom: UIUserInterfaceIdiom) -> Bool { idiom == .phone }
}
