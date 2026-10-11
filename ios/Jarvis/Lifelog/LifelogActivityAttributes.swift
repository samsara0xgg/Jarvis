import ActivityKit
import Foundation

// The recording's Live Activity shape, compiled into the app (which starts it) and the widget extension (which draws it).

struct LifelogActivityAttributes: ActivityAttributes {
    struct ContentState: Codable, Hashable {
        /// A date such that now minus it is today's total recorded time (closed pieces plus the one being written), so the view counts up by itself
        /// and the app updates this only when a new piece or midnight changes the sum.
        var since: Date
    }
}
