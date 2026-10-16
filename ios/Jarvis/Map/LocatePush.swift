import UIKit

// A silent push (`{"aps": {"content-available": 1}, "locate": "<request_id>"}`) wakes the app in the background to take one fix for the brain. The fix goes out the way every location of the phone
// does, as a `phone.location_observed` frame in the Report layer's queue, with the request's id on it. The system gives a wake some seconds: one fix is waited for at most `fixWait`, then the frame is sent.

enum LocatePush {
    static let fixWait = 15.0

    /// The request a push names, or nil for any other push.
    static func requestId(_ userInfo: [AnyHashable: Any]) -> String? {
        guard let id = userInfo["locate"] as? String, !id.isEmpty else { return nil }
        return id
    }

    /// One fix, reported with the request's id and sent. True when a fix was taken and its frame sent on (the system is told new data came); a push that finds no permission or no fix reports nothing,
    /// and the brain's own wait ends the request.
    @MainActor static func handle(_ requestId: String, source: LocationSource, reporter: Reporter = .shared, wait: Double = fixWait) async -> Bool {
        guard case .fix(let f) = await source.fix(wait: wait) else { return false }
        reporter.emit([.location(lat: f.lat, lng: f.lng, accuracyM: f.accuracyM, at: f.at, place: nil, speedMps: nil, requestId: requestId)])
        await reporter.flush()
        return true
    }
}
