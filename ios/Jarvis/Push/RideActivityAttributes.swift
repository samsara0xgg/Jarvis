// words: legacy
import ActivityKit
import Foundation

// The bus ride's Live Activity shape, compiled into the app (which tells the brain its tokens) and the widget extension (which draws it).
// The brain starts it by push-to-start under the type name "RideAttributes" and updates it with this content-state
// (jarvis/surface/apns.py `ride_activity_payload`, jarvis/runtime/rides.py `_attributes` and `_content`, ADR 0224).

struct RideAttributes: ActivityAttributes {
    struct ContentState: Codable, Hashable {
        var route: String?
        /// the stop the bus comes to next
        var nextStop: String?
        /// stops until his; null when his stop is unknown or the bus has no data
        var stopsLeft: Int?
        var minutesToAlight: Int?
        /// his stop; null until he has said which
        var alightStop: String?
        /// a live vehicle matched his trip
        var positionLive: Bool?
        /// his stop's arrival is a live prediction; false is a timetable estimate
        var arrivalLive: Bool?
        /// the one flag an older brain sent instead of the two above; read only when they are absent
        var stale: Bool?
        /// `riding`, `arriving` (one stop before his) or `ended`
        var phase: String?

        enum CodingKeys: String, CodingKey {
            case route, stale, phase
            case nextStop = "next_stop"
            case stopsLeft = "stops_left"
            case minutesToAlight = "minutes_to_alight"
            case alightStop = "alight_stop"
            case positionLive = "position_live"
            case arrivalLive = "arrival_live"
        }

        var isArriving: Bool { phase == "arriving" }
        var isEnded: Bool { phase == "ended" }
        /// the bus's own position is being read
        var hasLivePosition: Bool { positionLive ?? !(stale ?? true) }
        /// the minutes are the brain's live prediction, not the timetable's
        var hasLiveArrival: Bool { arrivalLive ?? !(stale ?? true) }

        /// minutes to his stop as the lock screen says them: 「约 6 分钟」, 「马上到」, nil when unknown
        var minutesText: String? {
            guard let m = minutesToAlight else { return nil }
            if m <= 0 { return "马上到" }
            return (hasLiveArrival ? "" : "约 ") + "\(m) 分钟"
        }

        /// one line on how far to trust the minutes; empty when they come from a live prediction
        var trustNote: String {
            if !hasLivePosition { return "没有车的实时位置，按时刻表" }
            return hasLiveArrival ? "" : "到站时间按时刻表估算"
        }
    }

    let rideId: String
    let route: String
    let boardStop: String?
    let startedAtMs: Int64?

    enum CodingKeys: String, CodingKey {
        case route
        case rideId = "ride_id"
        case boardStop = "board_stop"
        case startedAtMs = "started_at_ms"
    }
}
