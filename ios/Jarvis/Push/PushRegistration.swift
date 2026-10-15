import Foundation
import UIKit
import UserNotifications

// The phone's APNs tokens, told to the brain (docs/spec.html `POST /inherent/device/push`, ADR 0210). The tokens are held in memory only and never logged:
// the brain keeps them, and iOS issues the device token again at every launch.

/// The pure parts: how a token is spelled, which APNs endpoint this build's tokens belong to, and the body of the registration.
enum PushWire {
    /// the token as the brain wants it: lowercase hex
    static func hex(_ token: Data) -> String { token.map { String(format: "%02x", $0) }.joined() }

    /// What the brain is to do with the Live Activity token: leave the one it has, replace it, or forget it.
    enum ActivityToken: Equatable { case keep, set(String), clear }

    /// `environment` is the one the build's tokens are issued by. Each activity token (`live_activity_token`, and the ride's `ride_start_token` and `ride_activity_token`, ADR 0224)
    /// is left out to keep the stored one, and `null` clears it.
    static func registration(deviceToken: String, environment: String, activity: ActivityToken, rideStart: ActivityToken = .keep, rideActivity: ActivityToken = .keep) -> [String: Any] {
        var body: [String: Any] = ["device_token": deviceToken, "environment": environment]
        for (key, token) in [("live_activity_token", activity), ("ride_start_token", rideStart), ("ride_activity_token", rideActivity)] {
            switch token {
            case .keep: break
            case .set(let value): body[key] = value
            case .clear: body[key] = NSNull()
            }
        }
        return body
    }

    /// The `aps-environment` a provisioning profile signs in, as the brain names it; nil when the data is no profile or says nothing about push.
    static func environment(inProfile profile: Data) -> String? {
        guard let start = profile.range(of: Data("<?xml".utf8)), let end = profile.range(of: Data("</plist>".utf8)), start.lowerBound < end.upperBound,
              let plist = try? PropertyListSerialization.propertyList(from: Data(profile[start.lowerBound..<end.upperBound]), format: nil) as? [String: Any],
              let aps = (plist["Entitlements"] as? [String: Any])?["aps-environment"] as? String else { return nil }
        return aps == "production" ? "production" : "sandbox"
    }

    /// A build installed from Xcode carries its profile, which says which endpoint its tokens come from (a Release build signed for development is `sandbox`, too).
    /// No profile: the simulator, or a TestFlight or store build, which Apple signs for production.
    static var environment: String {
        if let url = Bundle.main.url(forResource: "embedded", withExtension: "mobileprovision"), let data = try? Data(contentsOf: url), let e = environment(inProfile: data) { return e }
        #if targetEnvironment(simulator)
        return "sandbox"
        #else
        return "production"
        #endif
    }
}

extension BrainClient {
    /// `POST /inherent/device/push`: 200 the brain took it; 400 (a body it does not accept), 403 (not a paired device's token) or 413 come back as their status.
    static func registerPush(_ body: [String: Any], bases: [URL], token: String) async throws -> Int {
        let data = try JSONSerialization.data(withJSONObject: body)
        return try await send("POST", "/inherent/device/push", body: data, also: [400, 403, 413], bases: bases, token: token, unauthorized: .revoked).status
    }
}

@MainActor
final class PushRegistry {
    static let shared = PushRegistry()

    private var deviceToken: String?
    private var activity = PushWire.ActivityToken.keep
    private var rideStart = PushWire.ActivityToken.keep, rideActivity = PushWire.ActivityToken.keep
    private var busy = false, again = false, failed = false

    /// Paired: ask for permission the first time, then have iOS issue the device token (it does so again at each launch, and when it changes).
    func start() async {
        guard BrainLink.shared.hasToken else { return }
        let center = UNUserNotificationCenter.current()
        var status = await center.notificationSettings().authorizationStatus
        if status == .notDetermined {
            _ = try? await center.requestAuthorization(options: [.alert, .sound, .badge])
            status = await center.notificationSettings().authorizationStatus
        }
        guard [.authorized, .provisional, .ephemeral].contains(status) else { return }
        UIApplication.shared.registerForRemoteNotifications()
    }

    func gotDeviceToken(_ data: Data) {
        deviceToken = PushWire.hex(data)
        Task { await send() }
    }

    /// The running Live Activity's token changed (`.set`) or the activity ended (`.clear`).
    func setActivityToken(_ token: PushWire.ActivityToken) {
        activity = token
        Task { await send() }
    }

    /// The ride Live Activity's push-to-start token changed (iOS issues it again at launch), or its own token (`rideActivity`) changed or the activity ended.
    func setRideStartToken(_ token: PushWire.ActivityToken) { rideStart = token; Task { await send() } }
    func setRideActivityToken(_ token: PushWire.ActivityToken) { rideActivity = token; Task { await send() } }

    /// A registration the brain did not take is made again when the app comes back to the screen.
    func retryIfNeeded() { if failed { Task { await send() } } }

    private func send() async {
        // one registration at a time, in order: the last one told is the one the brain keeps
        if busy { again = true; return }
        busy = true
        defer { busy = false }
        repeat {
            again = false
            guard BrainLink.shared.hasToken, let device = deviceToken else { return }
            let told = activity, toldStart = rideStart, toldRide = rideActivity
            let body = PushWire.registration(deviceToken: device, environment: PushWire.environment, activity: told, rideStart: toldStart, rideActivity: toldRide)
            let status = try? await BrainLink.shared.call { try await BrainClient.registerPush(body, bases: $0, token: $1) }
            failed = status != 200
            if status == 200 {
                if told == .clear && activity == .clear { activity = .keep }
                if toldStart == .clear && rideStart == .clear { rideStart = .keep }
                if toldRide == .clear && rideActivity == .clear { rideActivity = .keep }
            }
        } while again
    }
}
