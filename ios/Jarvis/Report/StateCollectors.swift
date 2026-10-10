import AVFoundation
import CallKit
import Intents
import UIKit

// The state signals, each from what iOS really lets a background app see:
//  - Focus: read at each wake (INFocusStatusCenter has no change callback); a Shortcuts automation running
//    `TellJarvisIntent` is the only way to learn the moment it flips.
//  - Power: battery notifications while the app runs, a read at each wake; also from the Shortcuts intent.
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
    private var observer: NSObjectProtocol?

    var access: ReportAccess { .granted }

    func start() {
        UIDevice.current.isBatteryMonitoringEnabled = true
        observer = NotificationCenter.default.addObserver(
            forName: UIDevice.batteryStateDidChangeNotification, object: nil, queue: .main
        ) { [weak self] _ in
            Task { @MainActor in await self?.wake() }
        }
        Task { await wake() }
    }

    func stop() {
        observer.map(NotificationCenter.default.removeObserver)
        observer = nil
        UIDevice.current.isBatteryMonitoringEnabled = false
    }

    func wake() async {
        let connected: Bool
        switch UIDevice.current.batteryState {
        case .charging, .full: connected = true
        case .unplugged: connected = false
        default: return
        }
        if ReportMemory.stateChanged("state.power", to: connected ? "connected" : "disconnected", quiet: "disconnected") {
            Reporter.shared.emit([.state(.power(connected: connected), at: Date(), detail: nil)])
        }
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
