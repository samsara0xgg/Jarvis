import AVFoundation
import CallKit
import Intents
import UIKit

// The state signals, each from what iOS really lets a background app see:
//  - Focus: read at each wake (INFocusStatusCenter has no change callback); a Shortcuts automation running
//    `TellJarvisIntent` is the only way to learn the moment it flips.
//  - Power: battery notifications while the app runs, a read at each wake; also from the Shortcuts intent. The battery level
//    (ADR 0292) is reported with it: at each level or state change while the app runs and at each wake.
//  - Headphones: the audio route notification while the app runs, a read at each wake.
//  - Calls: CXCallObserver while the app runs (a suspended app misses them).
// A change seen at a wake is stamped with the wake time, not with the moment it happened.

@MainActor
final class FocusCollector: Collector {
    let name = "focus"

    var access: ReportAccess {
        switch INFocusStatusCenter.default.authorizationStatus {
        case .authorized: .granted
        case .notDetermined: .notAsked
        case .denied: .denied
        default: .unavailable
        }
    }

    func start() {
        if INFocusStatusCenter.default.authorizationStatus == .notDetermined {
            INFocusStatusCenter.default.requestAuthorization { _ in
                Task { @MainActor in
                    await self.wake()
                    Reporter.shared.refreshStatus()
                }
            }
        }
    }

    func stop() {}

    func wake() async {
        guard INFocusStatusCenter.default.authorizationStatus == .authorized,
              let on = INFocusStatusCenter.default.focusStatus.isFocused else { return }
        if ReportMemory.stateChanged("state.focus", to: on ? "on" : "off", quiet: "off") {
            Reporter.shared.emit([.state(.focus(on: on), at: Date(), detail: nil)])
        }
    }
}

@MainActor
final class PowerCollector: Collector {
    let name = "power"
    private var observers: [NSObjectProtocol] = []
    /// the last level reported, so a level and a state change at the same moment are one frame
    private var lastReported: (percent: Int, detail: String, at: Date)?

    var access: ReportAccess { .granted }

    func start() {
        UIDevice.current.isBatteryMonitoringEnabled = true
        observers = [UIDevice.batteryStateDidChangeNotification, UIDevice.batteryLevelDidChangeNotification].map { name in
            NotificationCenter.default.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in
                Task { @MainActor in await self?.wake() }
            }
        }
        Task { await wake() }
    }

    func stop() {
        observers.forEach(NotificationCenter.default.removeObserver)
        observers = []
        UIDevice.current.isBatteryMonitoringEnabled = false
    }

    /// The charge as the brain takes it: whole percent and `charging`, `full` or `unplugged`; nil while iOS does not know it (a simulator).
    nonisolated static func reading(level: Float, state: UIDevice.BatteryState) -> (percent: Int, detail: String)? {
        guard level >= 0 else { return nil }
        let detail: String
        switch state {
        case .charging: detail = "charging"
        case .full: detail = "full"
        case .unplugged: detail = "unplugged"
        default: return nil
        }
        return (min(100, Int((level * 100).rounded())), detail)
    }

    func wake() async {
        let device = UIDevice.current
        let connected: Bool
        switch device.batteryState {
        case .charging, .full: connected = true
        case .unplugged: connected = false
        default: return
        }
        if ReportMemory.stateChanged("state.power", to: connected ? "connected" : "disconnected", quiet: "disconnected") {
            Reporter.shared.emit([.state(.power(connected: connected), at: Date(), detail: nil)])
        }
        guard let r = Self.reading(level: device.batteryLevel, state: device.batteryState) else { return }
        let now = Date()
        if let last = lastReported, last.percent == r.percent, last.detail == r.detail, now.timeIntervalSince(last.at) < 60 { return }
        lastReported = (r.percent, r.detail, now)
        Reporter.shared.emit([.state(.battery(percent: r.percent), at: now, detail: r.detail)])
    }
}

@MainActor
final class HeadphonesCollector: Collector {
    let name = "headphones"
    private var observer: NSObjectProtocol?

    /// Wired and Bluetooth outputs that mean headphones or earbuds (a car's Bluetooth shows up here too).
    private static let ports: Set<AVAudioSession.Port> = [.headphones, .bluetoothA2DP, .bluetoothHFP, .bluetoothLE]

    var access: ReportAccess { .granted }

    func start() {
        observer = NotificationCenter.default.addObserver(
            forName: AVAudioSession.routeChangeNotification, object: nil, queue: .main
        ) { [weak self] _ in
            Task { @MainActor in await self?.wake() }
        }
        Task { await wake() }
    }

    func stop() {
        observer.map(NotificationCenter.default.removeObserver)
        observer = nil
    }

    func wake() async {
        let port = AVAudioSession.sharedInstance().currentRoute.outputs.first { Self.ports.contains($0.portType) }
        let connected = port != nil
        // The name of a disconnected pair is the one that was connected.
        let name = port?.portName ?? ReportMemory.string("state.headphones.name")
        if connected { ReportMemory.set(string: port?.portName, for: "state.headphones.name") }
        if ReportMemory.stateChanged("state.headphones", to: connected ? "connected" : "disconnected", quiet: "disconnected") {
            Reporter.shared.emit([.state(.headphones(connected: connected), at: Date(), detail: name)])
        }
    }
}

@MainActor
final class CallCollector: NSObject, Collector, CXCallObserverDelegate {
    let name = "calls"
    private let observer = CXCallObserver()
    private var connected: Set<UUID> = []

    var access: ReportAccess { .granted }

    func start() { observer.setDelegate(self, queue: .main) }
    func stop() { observer.setDelegate(nil, queue: nil) }
    func wake() async {}

    /// A call counts when it connects and ends when it ends; one nobody answered is not reported. Only the
    /// direction is sent, never who it was with.
    nonisolated func callObserver(_ callObserver: CXCallObserver, callChanged call: CXCall) {
        let id = call.uuid, hasConnected = call.hasConnected, hasEnded = call.hasEnded, outgoing = call.isOutgoing
        Task { @MainActor in
            if hasEnded {
                if connected.remove(id) != nil {
                    Reporter.shared.emit([.state(.call(started: false), at: Date(), detail: outgoing ? "outgoing" : "incoming")])
                }
            } else if hasConnected, connected.insert(id).inserted {
                Reporter.shared.emit([.state(.call(started: true), at: Date(), detail: outgoing ? "outgoing" : "incoming")])
            }
        }
    }
}
