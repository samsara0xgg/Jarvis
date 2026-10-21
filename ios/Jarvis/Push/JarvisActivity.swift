// words: legacy
import ActivityKit
import Foundation

// The Live Activity's shape, compiled into the app (which starts it) and the widget extension (which draws it).
// The content-state is the one the brain pushes (docs/spec.html, "Live Activity"; jarvis/runtime/push.py `_update_activity`).

struct JarvisActivityAttributes: ActivityAttributes {
    struct ContentState: Codable, Hashable {
        /// `conversation` while she talks with him; otherwise `idle` at quiet level off, or the level's name (`quiet`, `no-pop`, `dnd`)
        var state: String
        /// the reminder that rings first and when (milliseconds since 1970); both null when none waits
        var nextText: String?
        var nextAtMs: Int64?

        enum CodingKeys: String, CodingKey {
            case state
            case nextText = "next_text"
            case nextAtMs = "next_at_ms"
        }

        static let idle = ContentState(state: "idle", nextText: nil, nextAtMs: nil)

        var nextAt: Date? { nextAtMs.map { Date(timeIntervalSince1970: Double($0) / 1000) } }

        /// what she is doing, in a word
        var stateWord: String {
            switch state {
            case "conversation": "正在对话"
            case "idle": "在这儿"
            case "quiet": "安静"
            case "no-pop": "不弹卡片"
            case "dnd": "勿扰"
            default: state
            }
        }
    }
}
