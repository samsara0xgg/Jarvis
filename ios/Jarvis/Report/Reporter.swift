// words: legacy
import BackgroundTasks
import Network
import Observation
import UIKit
import os

/// The switches of 手机感知 that send something to the brain. `title` is the row's title in Settings.
enum ReportSense: String, CaseIterable {
    case location, motion, health, focusAlarm, headphonesCalls

    var title: String {
        switch self {
        case .location: "位置"
        case .motion: "运动和步数"
        case .health: "健康"
        case .focusAlarm: "专注模式和闹钟"
        case .headphonesCalls: "耳机和通话"
        }
    }

    init?(title: String) {
        guard let s = Self.allCases.first(where: { $0.title == title }) else { return nil }
        self = s
    }
}

/// What iOS lets the app do for one signal. Worst first, so the weakest part of a sense is its `min`.
enum ReportAccess: Int, Comparable {
    case denied, unavailable, notAsked
    /// Only partly allowed (location "while using"; the app then sees no wake in the background).
    case partial
    /// Health never says whether a read was refused; the question was asked and that is all the app can know.
    case asked
    case granted

    static func < (a: Self, b: Self) -> Bool { a.rawValue < b.rawValue }
}

/// What the user can change in iOS Settings to let a signal work fully (`Reporter.openSettings` goes there).
enum AccessFix {
    /// Location is "while using"; the app sees no wake in the background until it is "Always".
    case allowAlways
    /// Location is approximate; region monitoring is off and significant changes are coarse.
    case allowPrecise
}

/// A part of the phone that is watched for one sense. Created when its switch is on, started at launch.
@MainActor
protocol Collector: AnyObject {
    var name: String { get }
    var access: ReportAccess { get }
    /// What to change in Settings to lift a `partial` access, if anything.
    var fix: AccessFix? { get }
    /// Begin watching; asks for the permission it needs if that was not asked yet.
    func start()
    func stop()
    /// Read what happened since the last wake (app opened, background refresh, a system wake).
    func wake() async
}

extension Collector {
    var fix: AccessFix? { nil }
}

/// Keeps the app running a little longer, for work that has to finish after a wake (a request, a place name).
/// Ask at the very top of the callback that woke the app, and `end()` when the work is done.
@MainActor
final class BackgroundHold {
    private var id = UIBackgroundTaskIdentifier.invalid

    static func begin(_ name: String) -> BackgroundHold {
        let hold = BackgroundHold()
        hold.id = UIApplication.shared.beginBackgroundTask(withName: name) { [weak hold] in hold?.end() }
        return hold
    }

    func end() {
        guard id != .invalid else { return }
        UIApplication.shared.endBackgroundTask(id)
        id = .invalid
    }
}

/// Small facts that outlive the process: switches, history cursors, the last state sent. All in UserDefaults.
enum ReportMemory {
    private static var d: UserDefaults { .standard }

    static func isOn(_ s: ReportSense) -> Bool { d.bool(forKey: "report.sense.\(s.rawValue)") }
    static func set(_ s: ReportSense, on: Bool) { d.set(on, forKey: "report.sense.\(s.rawValue)") }

    static func date(_ key: String) -> Date? { d.object(forKey: "report.\(key)") as? Date }
    static func set(date: Date?, for key: String) { d.set(date, forKey: "report.\(key)") }
    static func string(_ key: String) -> String? { d.string(forKey: "report.\(key)") }
    static func set(string: String?, for key: String) { d.set(string, forKey: "report.\(key)") }
    static func data(_ key: String) -> Data? { d.data(forKey: "report.\(key)") }
    static func set(data: Data?, for key: String) { d.set(data, forKey: "report.\(key)") }

    /// How far back a first read goes, so turning a switch on sends today and last night, not a week.
    static let firstLookback: TimeInterval = 24 * 3600

    /// True when `value` differs from the last one sent under `key`. The first look at a signal only counts
    /// when it is not the `quiet` value (no charger, no headphones, Focus off): quiet is not news.
    static func stateChanged(_ key: String, to value: String, quiet: String) -> Bool {
        let old = string(key)
        set(string: value, for: key)
        return old != value && !(old == nil && value == quiet)
    }

    /// Turning a switch off forgets where it had read to, so turning it on later does not send the time it was off.
    static func forget(_ s: ReportSense) {
        let keys: [ReportSense: [String]] = [
            .location: ["location.firstFix"],
            .motion: ["motion.cursor", "motion.open"],
            .focusAlarm: ["state.focus", "state.power"],
            .headphonesCalls: ["state.headphones", "state.headphones.name"],
        ]
        for k in keys[s] ?? [] { d.removeObject(forKey: "report.\(k)") }
        // the Health reads keep one anchor, one start and one sent-list per type
        let prefix: [ReportSense: String] = [.motion: "report.health.t.", .health: "report.health.r."]
        if let p = prefix[s] { for k in d.dictionaryRepresentation().keys where k.hasPrefix(p) { d.removeObject(forKey: k) } }
    }
}

/// The phone's reporter: collectors put observations in the queue, `flush` sends them to the brain.
///
/// The app wires two things: `Reporter.shared.transport` (an `HTTPReportTransport` fed by the paired brain's URL
/// and token) and, from Settings, `setEnabled`. Until a transport is set, frames wait in the queue.
@MainActor @Observable
final class Reporter {
    static let shared = Reporter(queue: .standard)

    static let refreshID = "com.yilunshi.jarvis.report-flush"
    /// Earliest start the app asks of the system for a background refresh; iOS decides the real time.
    static let refreshEvery: TimeInterval = 2 * 3600

    enum Problem { case offline, unauthorized, rejected }

    struct SenseStatus { var enabled: Bool; var access: ReportAccess; var fix: AccessFix? }

    /// What Settings can show: counts and states only, never what was sensed.
    struct Status {
        var queued = 0
        var lastSentAt: Date?
        /// Why the last send failed; nil when it worked or no brain is paired yet.
        var problem: Problem?
        /// Background App Refresh is off or restricted: the 2-hourly refresh and Health wakes will not come.
        var backgroundRefreshOff = false
        /// Low Power Mode throttles background refresh and Health delivery.
        var lowPowerMode = false
        var senses: [ReportSense: SenseStatus] = [:]
        /// Per collector ("location", "motion", "steps", "health" for workouts and sleep, "focus", "power", "headphones", "calls").
        var signals: [String: ReportAccess] = [:]
    }

    private(set) var status = Status()

    @ObservationIgnored var transport: ReportTransport?
    /// Takes a batch the network refused and sends it when the network is back, even with the app suspended (ReportUploads). Nil in tests.
    @ObservationIgnored var uploads: ReportUploading?
    /// Off in tests, which flush by hand.
    @ObservationIgnored var autoFlush = true
    /// iOS launched the app in the background for a location event (`launchOptions[.location]`).
    @ObservationIgnored var launchedForLocation = false
    @ObservationIgnored let queue: ReportQueue
    /// This device reports what it senses (a phone); an iPad starts no collector and takes no automation's change (ADR 0338). Tests give either, on any device.
    @ObservationIgnored let sensing: Bool
    @ObservationIgnored private var collectors: [ReportSense: [Collector]] = [:]
    @ObservationIgnored private var launched = false
    @ObservationIgnored private var waking = false
    @ObservationIgnored private var flushing = false
    @ObservationIgnored private var flushTask: Task<Void, Never>?
    @ObservationIgnored private var path: NWPathMonitor?
    @ObservationIgnored private let log = Logger(subsystem: "com.allen.jarvis", category: "report")

    init(queue: ReportQueue, sensing: Bool = DeviceRole.senses) {
        self.queue = queue
        self.sensing = sensing
        status.lastSentAt = ReportMemory.date("lastSentAt")
    }

    // MARK: Lifecycle

    /// Call from `application(_:didFinishLaunchingWithOptions:)`, also on a background launch: a location,
    /// Health or refresh wake needs its delegate and handler in place before launch returns.
    func launch() {
        guard !launched else { return }
        launched = true
        BGTaskScheduler.shared.register(forTaskWithIdentifier: Self.refreshID, using: nil) { task in
            Task { @MainActor in Reporter.shared.run(task) }
        }
        for s in ReportSense.allCases where ReportMemory.isOn(s) { startCollectors(s) }
        log.info("launched with \(self.collectors.count) senses on")
        let nc = NotificationCenter.default
        nc.addObserver(forName: UIApplication.didBecomeActiveNotification, object: nil, queue: .main) { _ in
            Task { @MainActor in
                await Reporter.shared.wake()
            }
        }
        nc.addObserver(forName: UIApplication.didEnterBackgroundNotification, object: nil, queue: .main) { _ in
            Task { @MainActor in Reporter.shared.scheduleRefresh() }
        }
        watchNetwork()
        refreshStatus()
        Task { await wake() }
    }

    /// The network came back while the app runs (it runs all day while he records): what waited for it goes now, not at the next wake.
    private func watchNetwork() {
        let monitor = NWPathMonitor()
        let satisfied = OSAllocatedUnfairLock<Bool?>(initialState: nil)
        monitor.pathUpdateHandler = { path in
            let now = path.status == .satisfied
            let back = satisfied.withLock { was in defer { was = now }; return now && was == false }
            if back { Task { @MainActor in Reporter.shared.networkBack() } }
        }
        monitor.start(queue: DispatchQueue(label: "jarvis.report.path"))
        path = monitor
    }

    func networkBack() {
        guard queue.count > 0 else { return }
        log.info("network is back, \(self.queue.count) frames waiting")
        flushSoon()
    }

    func isEnabled(_ s: ReportSense) -> Bool { ReportMemory.isOn(s) }

    /// The Settings switch of a sense: starts its collectors (asking for permission) or stops and forgets them.
    func setEnabled(_ s: ReportSense, _ on: Bool) {
        guard ReportMemory.isOn(s) != on else { return }
        ReportMemory.set(s, on: on)
        if on {
            startCollectors(s)
            Task { await wake() }
        } else {
            collectors[s]?.forEach { $0.stop() }
            collectors[s] = nil
            ReportMemory.forget(s)
            queue.remove(sense: s.rawValue)
        }
        refreshStatus()
    }

    private func startCollectors(_ s: ReportSense) {
        guard sensing else { return }          // an iPad senses nothing (ADR 0338)
        let cs: [Collector] = switch s {
        case .location: [LocationCollector()]
        case .motion: [MotionCollector(), HealthCollector(.totals), AccelRecorder()]
        case .health: [HealthCollector(.records)]
        case .focusAlarm: [FocusCollector(), PowerCollector()]
        case .headphonesCalls: [HeadphonesCollector(), CallCollector()]
        }
        collectors[s] = cs
        cs.forEach { $0.start() }
    }

    /// Motion history says what he is doing now; the location collector follows (adaptive updates).
    func movement(_ sample: MotionSample?) {
        (collectors[.location]?.first as? LocationCollector)?.motion(sample)
    }

    /// Location woke from its deep sleep: the motion history is read now, not at the next wake, so the brain hears when the move began
    /// even when GPS came late (ADR 0312).
    func readMotionNow() {
        guard let motion = collectors[.motion]?.first(where: { $0 is MotionCollector }) else { return }
        Task { await withBackgroundTime("jarvis.report.motion") { await motion.wake() } }
    }

    /// The iOS Settings page of the app, where Location can be set to Always and Precise.
    func openSettings() {
        if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) }
    }

    func refreshStatus() {
        var s = status
        s.queued = queue.count
        s.signals = [:]
        for sense in ReportSense.allCases {
            let cs = collectors[sense] ?? []
            s.senses[sense] = SenseStatus(enabled: ReportMemory.isOn(sense), access: cs.map(\.access).min() ?? .notAsked,
                                          fix: cs.lazy.compactMap(\.fix).first)
            for c in cs { s.signals[c.name] = c.access }
        }
        s.backgroundRefreshOff = UIApplication.shared.backgroundRefreshStatus != .available
        s.lowPowerMode = ProcessInfo.processInfo.isLowPowerModeEnabled
        status = s
    }

    // MARK: Collecting

    private static func sense(of o: Sensed) -> ReportSense {
        switch o {
        case .visit, .location, .track: .location
        case .motion: .motion
        case .health(let metric, _, _, _, _, _): metric.isTotal ? .motion : .health
        case .measure: .health
        case .state(let change, _, _):
            switch change {
            case .focus, .alarm, .power: .focusAlarm
            case .headphones, .call: .headphonesCalls
            case .battery, .watchBattery: .focusAlarm
            }
        }
    }

    /// Queues observations (those the brain would refuse are dropped) and, soon, sends them.
    func emit(_ observations: [Sensed]) {
        var any = false
        for o in observations {
            guard let frame = ReportFrame(o) else { continue }
            queue.append([frame], sense: Self.sense(of: o).rawValue)
            any = true
        }
        guard any else { return }
        status.queued = queue.count
        log.info("queued \(observations.count) frames, \(self.status.queued) waiting")
        if autoFlush { flushSoon() }
    }

    /// In the background a wake lasts seconds, so the frame goes out now; in the foreground a short pause
    /// gathers what arrives together into one request.
    private func flushSoon() {
        if UIApplication.shared.applicationState == .background {
            let hold = BackgroundHold.begin("jarvis.report.flush")
            Task {
                await flush()
                hold.end()
            }
        } else {
            scheduleFlush()
        }
    }

    /// A state change a Shortcuts automation reported through the app intent. Focus and power also keep the
    /// baseline the wake-time sampling compares with, so the same change is not sent twice.
    func tell(_ change: StateChange) async {
        guard sensing, ReportMemory.isOn(.focusAlarm) else { return }          // an iPad's automations say nothing of him (ADR 0338)
        switch change {
        case .focus: _ = ReportMemory.stateChanged("state.focus", to: change.value, quiet: "")
        case .power: _ = ReportMemory.stateChanged("state.power", to: change.value, quiet: "")
        default: break
        }
        emit([.state(change, at: Date(), detail: nil)])
        await flush()
    }

    // MARK: Waking and sending

    /// Read what each collector missed, then send. Runs at launch, when the app opens and on a background refresh.
    func wake() async {
        guard !waking else { return }
        waking = true
        defer { waking = false }
        await withBackgroundTime("jarvis.report.wake") {
            for sense in ReportSense.allCases {
                for c in collectors[sense] ?? [] { await c.wake() }
            }
            await flush()
        }
        refreshStatus()
    }

    private func scheduleFlush() {
        guard flushTask == nil else { return }
        flushTask = Task { [weak self] in
            // A short pause gathers what a wake produces into one request.
            try? await Task.sleep(for: .seconds(2))
            await self?.withBackgroundTime("jarvis.report.flush") { await self?.flush() }
            self?.flushTask = nil
        }
    }

    /// Batches one flush may send. A day of Watch heart rate is a few thousand frames; a wake lasts seconds, so the loop also stops when the time
    /// iOS gave is nearly spent, and the next wake sends the rest.
    static let maxBatchesPerFlush = 50

    /// Sends queued frames in batches. Frames the brain stored or refused are dropped; only `retry` ones stay. A batch the network refused is also
    /// handed to the background session, which sends it when the network is back; its frames stay queued until the brain's answer settles them.
    func flush() async {
        guard !flushing, let transport else { return }
        flushing = true
        defer { flushing = false; refreshStatus() }
        for _ in 0..<Self.maxBatchesPerFlush {
            let batch = queue.head()
            if batch.isEmpty { break }
            if UIApplication.shared.applicationState == .background, UIApplication.shared.backgroundTimeRemaining < 4 { break }
            do {
                let done = accept(batch.map(\.eventUid), try await transport.send(batch))
                if done < batch.count { break }  // a frame answered `retry`: next wake
            } catch ReportTransportError.notConfigured {
                break
            } catch ReportTransportError.unauthorized {
                status.problem = .unauthorized
                log.info("send refused: not authorized")
                break
            } catch ReportTransportError.rejected {
                queue.remove(batch.map(\.eventUid))  // the same bytes would be rejected again
                status.problem = .rejected
            } catch let error as URLError {
                status.problem = .offline
                log.info("send failed: \(error.localizedDescription, privacy: .public)")
                if let request = transport.backgroundRequest(for: batch) { uploads?.submit(batch, request: request) }
                break
            } catch {
                status.problem = .offline
                log.info("send failed: \(error.localizedDescription, privacy: .public)")
                break
            }
        }
    }

    /// The brain answered a batch: drops the frames it stored or refused, and returns how many that was.
    @discardableResult
    private func accept(_ uids: [String], _ acks: [FrameAck]) -> Int {
        let done = ReportBatch.settled(uids: uids, acks)
        queue.remove(done)
        log.info("sent \(uids.count) frames, \(done.count) settled")
        status.lastSentAt = Date()
        ReportMemory.set(date: status.lastSentAt, for: "lastSentAt")
        status.problem = nil
        return done.count
    }

    /// The answer of a batch the background session delivered (`status` nil: the task failed, the frames stay queued): settled as `flush` does.
    func settleBackground(uids: [String], status code: Int?, body: Data) {
        defer { refreshStatus() }
        guard let code else { return }
        do {
            accept(uids, try HTTPReportTransport.interpret(status: code, data: body))
        } catch ReportTransportError.unauthorized {
            status.problem = .unauthorized
        } catch ReportTransportError.rejected {
            queue.remove(uids)
            status.problem = .rejected
        } catch {
            log.info("background send answered \(code)")
        }
    }

    /// Asks iOS for the extra seconds a wake needs to finish its request.
    private func withBackgroundTime<T>(_ name: String, _ body: () async -> T) async -> T {
        let hold = BackgroundHold.begin(name)
        defer { hold.end() }
        return await body()
    }

    // MARK: Background refresh

    private func scheduleRefresh() {
        guard ReportSense.allCases.contains(where: ReportMemory.isOn) else { return }
        let request = BGAppRefreshTaskRequest(identifier: Self.refreshID)
        request.earliestBeginDate = Date(timeIntervalSinceNow: Self.refreshEvery)
        do { try BGTaskScheduler.shared.submit(request) } catch { log.info("background refresh not scheduled") }
    }

    private func run(_ task: BGTask) {
        scheduleRefresh()
        let work = Task { @MainActor in
            await wake()
            task.setTaskCompleted(success: !Task.isCancelled)
        }
        task.expirationHandler = { work.cancel() }
    }
}
