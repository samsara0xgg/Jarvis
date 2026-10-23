import Foundation
import WatchConnectivity
import WatchKit

// The watch's charge, for the brain (ADR 0377): read when the app starts, when it comes to the front, and in a background refresh asked for
// every 15 minutes, each refresh asking for the next. Every reading goes, unchanged ones too: a run of them says the watch stayed on its charger.
// Each is queued to the phone with `transferUserInfo`, never `sendMessage`, so one read while the phone is out of reach still arrives.

@MainActor
enum WatchCharge {
    static let every: TimeInterval = 15 * 60
    /// the background refresh's name, given as its userInfo so `.backgroundTask(.appRefresh(_:))` finds it
    static let refreshID = "com.yilunshi.jarvis.watch.charge"

    /// the last reading queued, so the launch and the first coming to the front are one reading
    private static var lastSent: Date?

    /// The charge as the brain takes it: whole percent and `charging`, `full` or `unplugged`; nil while watchOS does not know it (a simulator).
    nonisolated static func reading(level: Float, state: WKInterfaceDeviceBatteryState) -> (percent: Int, state: String)? {
        guard level >= 0 else { return nil }
        let s: String
        switch state {
        case .charging: s = "charging"
        case .full: s = "full"
        case .unplugged: s = "unplugged"
        default: return nil
        }
        return (min(100, Int((level * 100).rounded())), s)
    }

    /// Reads the charge and queues it to the phone, once the link is up (a background launch has only just asked for it).
    static func send() async {
        let now = Date()
        if let last = lastSent, now.timeIntervalSince(last) < 60 { return }
        let device = WKInterfaceDevice.current()
        device.isBatteryMonitoringEnabled = true
        guard let r = reading(level: device.batteryLevel, state: device.batteryState) else { return }
        lastSent = now
        let session = WCSession.default
        var waited = 0
        while session.activationState != .activated, waited < 100 { waited += 1; try? await Task.sleep(nanoseconds: 100_000_000) }   // up to 10 s
        guard session.activationState == .activated else { lastSent = nil; return }
        session.transferUserInfo(WatchWire.encode(WatchAsk.charge(percent: r.percent, state: r.state, at: now)))
    }

    /// The next background refresh, 15 minutes on (watchOS keeps one; asking again moves it).
    static func schedule() {
        WKApplication.shared().scheduleBackgroundRefresh(withPreferredDate: Date(timeIntervalSinceNow: every), userInfo: refreshID as NSString) { _ in }
    }

    /// A background refresh: the next one is asked for first, so one that runs out of time still has a successor.
    static func refresh() async {
        schedule()
        await send()
    }
}
