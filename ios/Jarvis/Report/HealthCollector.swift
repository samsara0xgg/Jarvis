import HealthKit

/// Steps, workouts and sleep from Apple Health, read-only. The app is woken by HealthKit's background delivery
/// (an observer query registered at each launch) and asks statistics queries for what is new; nothing polls.
///
/// Health never tells an app whether a read was refused, so a refused read looks like no data.
///
/// What is sent, and why it does not double count:
/// - Steps: `HKStatisticsCollectionQuery` cumulative sums in hourly buckets. HealthKit merges iPhone and Watch
///   itself; raw samples from both devices would count twice. Each wake re-reads the last 24 hours of closed
///   hours and sends the buckets whose total differs from the one sent (the Watch may deliver late); the brain
///   keeps the later row for a span.
/// - Sleep: only asleep stages (unspecified, core, deep, REM), never in-bed or awake; sources overlapping in
///   time are counted once. A block is sent once, after it is settled (45 minutes past its end), and never
///   amended, because a longer block would overlap the first and the brain adds the minutes of overlapping rows.
/// - Workouts: each once, with the activity name as `detail`.
/// - `ts_epoch_ms` of every frame is the end of the span it covers.
@MainActor
final class HealthCollector: Collector {
    enum Kind { case steps, workouts, sleep }

    nonisolated static let sleepSettle: TimeInterval = 45 * 60
    /// Asleep stretches this close are one block of sleep (the brain merges at the same gap).
    nonisolated static let sleepGap: TimeInterval = 30 * 60

    private let kinds: [Kind]
    private let store = HKHealthStore()
    private var observers: [HKObserverQuery] = []
    private var requested = false

    init(_ kinds: [Kind]) { self.kinds = kinds }

    var name: String { kinds == [.steps] ? "steps" : "health" }

    /// HEALTH_ENTITLED is set in project.yml together with the HealthKit entitlement; touching HealthKit without the entitlement would crash the app.
    nonisolated static let entitled: Bool = {
        #if HEALTH_ENTITLED
        true
        #else
        false
        #endif
    }()

    var access: ReportAccess {
        guard Self.entitled, HKHealthStore.isHealthDataAvailable() else { return .unavailable }
        return requested ? .asked : .notAsked
    }

    private func type(_ k: Kind) -> HKSampleType {
        switch k {
        case .steps: HKQuantityType(.stepCount)
        case .workouts: HKObjectType.workoutType()
        case .sleep: HKCategoryType(.sleepAnalysis)
        }
    }

    func start() {
        guard Self.entitled, HKHealthStore.isHealthDataAvailable() else { return }
        // The observers are registered at once: a background launch for a Health delivery needs them in place
        // before the launch settles, not after the authorization question has been asked.
        for k in kinds { register(k) }
        Task {
            let types = Set(kinds.map(type) as [HKObjectType])
            if (try? await store.statusForAuthorizationRequest(toShare: [], read: types)) == .shouldRequest {
                try? await store.requestAuthorization(toShare: [], read: types)
            }
            requested = true
            for k in kinds {
                // Steps are delivered at most hourly; workouts and sleep are rare and can come at once.
                try? await store.enableBackgroundDelivery(for: type(k), frequency: k == .steps ? .hourly : .immediate)
            }
            await wake()
            Reporter.shared.refreshStatus()
        }
    }

    func stop() {
        guard Self.entitled else { return }
        observers.forEach(store.stop)
        observers = []
        for k in kinds { Task { try? await store.disableBackgroundDelivery(for: type(k)) } }
    }

    func wake() async {
        guard Self.entitled else { return }
        for k in kinds { await sync(k) }
    }

    /// The observer is what HealthKit calls, also in a background launch, when new samples arrive. It must
    /// tell HealthKit it is done, or delivery to the app slows down.
    private func register(_ k: Kind) {
        let q = HKObserverQuery(sampleType: type(k), predicate: nil) { [weak self] _, done, error in
            Task { @MainActor in
                if error == nil { await self?.sync(k) }
                done()
            }
        }
        store.execute(q)
        observers.append(q)
    }

    private func sync(_ k: Kind) async {
        switch k {
        case .steps: await syncSteps()
        case .workouts: await syncWorkouts()
        case .sleep: await syncSleep()
        }
    }

    // MARK: Steps

    private func syncSteps() async {
        let now = Date(), cal = Calendar.current
        // Closed hours of the last 24 hours; the hour in progress waits until it is over.
        guard let end = cal.dateInterval(of: .hour, for: now)?.start,
              let start = cal.dateInterval(of: .hour, for: now - ReportMemory.firstLookback)?.start, end > start else { return }
        let query = HKStatisticsCollectionQueryDescriptor(
            predicate: .quantitySample(type: HKQuantityType(.stepCount),
                                       predicate: HKQuery.predicateForSamples(withStart: start, end: end)),
            options: .cumulativeSum, anchorDate: start, intervalComponents: DateComponents(hour: 1))
        guard let result = try? await query.result(for: store) else { return }  // locked phone: try at the next wake
        var buckets: [(start: Date, end: Date, steps: Double)] = []
        result.enumerateStatistics(from: start, to: end) { s, _ in
            buckets.append((s.startDate, s.endDate, s.sumQuantity()?.doubleValue(for: .count()) ?? 0))
        }
        var sent = ReportMemory.data("steps.sent").flatMap { try? JSONDecoder().decode([String: Double].self, from: $0) } ?? [:]
        let out = Self.newSteps(buckets, sent: &sent)
        sent = sent.filter { (Double($0.key) ?? 0) >= start.timeIntervalSince1970 }
        ReportMemory.set(data: try? JSONEncoder().encode(sent), for: "steps.sent")
        Reporter.shared.emit(out)
    }

    /// The hourly totals to send: those above zero whose total differs from the one last sent for that hour
    /// (`sent`, by start second, is updated). A different total is a new frame for the same span.
    nonisolated static func newSteps(_ buckets: [(start: Date, end: Date, steps: Double)], sent: inout [String: Double]) -> [Sensed] {
        var out: [Sensed] = []
        for b in buckets {
            let n = b.steps.rounded()
            let hour = String(Int64(b.start.timeIntervalSince1970))
            guard n > 0, sent[hour] != n else { continue }
            sent[hour] = n
            out.append(.health(metric: .steps, start: b.start, end: b.end, value: n, detail: nil))
        }
        return out
    }

    // MARK: Workouts

    private func syncWorkouts() async {
        let since = ReportMemory.date("workout.since") ?? Date() - ReportMemory.firstLookback
        ReportMemory.set(date: since, for: "workout.since")
        let anchor = ReportMemory.data("workout.anchor")
            .flatMap { try? NSKeyedUnarchiver.unarchivedObject(ofClass: HKQueryAnchor.self, from: $0) }
        let query = HKAnchoredObjectQueryDescriptor(
            predicates: [.workout(HKQuery.predicateForSamples(withStart: since, end: nil))], anchor: anchor)
        guard let result = try? await query.result(for: store) else { return }
        Reporter.shared.emit(result.addedSamples.map {
            .health(metric: .workoutMin, start: $0.startDate, end: $0.endDate, value: $0.duration / 60,
                    detail: Self.workoutName($0.workoutActivityType))
        })
        ReportMemory.set(data: try? NSKeyedArchiver.archivedData(withRootObject: result.newAnchor as Any, requiringSecureCoding: true),
                         for: "workout.anchor")
    }

    nonisolated static func workoutName(_ t: HKWorkoutActivityType) -> String {
        switch t {
        case .running: "running"
        case .walking: "walking"
        case .hiking: "hiking"
        case .cycling: "cycling"
        case .swimming: "swimming"
        case .yoga: "yoga"
        case .elliptical: "elliptical"
        case .rowing: "rowing"
        case .traditionalStrengthTraining, .functionalStrengthTraining: "strength"
        case .highIntensityIntervalTraining: "hiit"
        default: "other"
        }
    }

    // MARK: Sleep

    private func syncSleep() async {
        let now = Date()
        let from = max(ReportMemory.date("sleep.cursor") ?? .distantPast, now - ReportMemory.firstLookback)
        let query = HKSampleQueryDescriptor(
            predicates: [.categorySample(type: HKCategoryType(.sleepAnalysis),
                                         predicate: HKQuery.predicateForSamples(withStart: from, end: nil))],
            sortDescriptors: [SortDescriptor(\.startDate)])
        guard let samples = try? await query.result(for: store) else { return }
        let asleep: Set<Int> = [
            HKCategoryValueSleepAnalysis.asleepUnspecified.rawValue, HKCategoryValueSleepAnalysis.asleepCore.rawValue,
            HKCategoryValueSleepAnalysis.asleepDeep.rawValue, HKCategoryValueSleepAnalysis.asleepREM.rawValue,
        ]
        let spans = samples.filter { asleep.contains($0.value) }.map { (start: max($0.startDate, from), end: $0.endDate) }
        let blocks = Self.sleepBlocks(spans, now: now)
        guard case .health(_, _, let end, _, _)? = blocks.last else { return }
        Reporter.shared.emit(blocks)
        ReportMemory.set(date: end, for: "sleep.cursor")
    }

    /// Asleep spans from every source, overlaps counted once, grouped into blocks of sleep (gaps up to
    /// `sleepGap`), without the blocks that are not settled yet. `value` is the minutes asleep in the block.
    nonisolated static func sleepBlocks(_ spans: [(start: Date, end: Date)], now: Date) -> [Sensed] {
        var union: [(start: Date, end: Date)] = []
        for s in spans.sorted(by: { $0.start < $1.start }) where s.end > s.start {
            if let last = union.last, s.start <= last.end {
                union[union.count - 1].end = max(last.end, s.end)
            } else {
                union.append(s)
            }
        }
        var blocks: [(start: Date, end: Date, seconds: TimeInterval)] = []
        for u in union {
            if let last = blocks.last, u.start.timeIntervalSince(last.end) <= sleepGap {
                blocks[blocks.count - 1].end = u.end
                blocks[blocks.count - 1].seconds += u.end.timeIntervalSince(u.start)
            } else {
                blocks.append((u.start, u.end, u.end.timeIntervalSince(u.start)))
            }
        }
        return blocks.filter { now.timeIntervalSince($0.end) >= sleepSettle }.map {
            .health(metric: .sleepMin, start: $0.start, end: $0.end, value: ($0.seconds / 60).rounded(), detail: nil)
        }
    }
}
