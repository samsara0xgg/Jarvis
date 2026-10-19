import HealthKit

/// Apple Health, read-only (ADR 0293): what Health recorded, as it recorded it. The app is woken by HealthKit's background delivery (an observer
/// query per type, registered at each launch) and asks what is new; nothing polls.
///
/// Health never tells an app whether a read was refused, so a refused read looks like no data.
///
/// What is sent, and why it does not double count:
/// - Cumulative quantities (steps, energy, exercise and stand minutes, distance, flights, daylight): `HKStatisticsCollectionQuery` sums in hourly
///   buckets. Health merges iPhone and Watch only in statistics; their raw samples would count twice. Each wake re-reads the last 24 hours of
///   closed hours and sends the buckets whose total differs from the one sent (the Watch may deliver late); the brain keeps the later row of a span.
/// - Everything else is one frame per sample, exactly as recorded (UUID, span, value, unit, source, device and the metadata that carries meaning):
///   heart rate and its kin, heartbeat series, blood oxygen, respiration, wrist temperature, VO2 max, audio exposure, sleep stages, workouts,
///   mindful sessions, stand hours. An anchored query per type, with the anchor saved, returns each sample once; nothing is merged here.
/// - `ts_epoch_ms` of every frame is the end of the span it covers.
@MainActor
final class HealthCollector: Collector {
    /// The motion sense sends the totals; the health sense sends the samples.
    enum Kind { case totals, records }

    /// How a type is turned into frames.
    enum Shape {
        case total(HealthMetric, HKUnit, label: String)
        case quantity(MeasureMetric, HKUnit, label: String)
        case sleep, standHour, mindful, workout, heartbeats
    }

    struct Stream {
        let id: String
        let type: HKSampleType
        let shape: Shape
        /// rare types wake the app at once; frequent ones at most hourly
        let delivery: HKUpdateFrequency
        /// for the shapes that are quantities
        var quantityType: HKQuantityType { type as! HKQuantityType }  // swiftlint:disable:this force_cast
    }

    private nonisolated static let perMinute = HKUnit.count().unitDivided(by: .minute())

    static let totals: [Stream] = [
        Stream(id: "steps", type: HKQuantityType(.stepCount), shape: .total(.steps, .count(), label: "count"), delivery: .hourly),
        Stream(id: "active_energy", type: HKQuantityType(.activeEnergyBurned), shape: .total(.activeEnergy, .kilocalorie(), label: "kcal"), delivery: .hourly),
        Stream(id: "basal_energy", type: HKQuantityType(.basalEnergyBurned), shape: .total(.basalEnergy, .kilocalorie(), label: "kcal"), delivery: .hourly),
        Stream(id: "exercise_time", type: HKQuantityType(.appleExerciseTime), shape: .total(.exerciseTime, .minute(), label: "min"), delivery: .hourly),
        Stream(id: "stand_time", type: HKQuantityType(.appleStandTime), shape: .total(.standTime, .minute(), label: "min"), delivery: .hourly),
        Stream(id: "distance", type: HKQuantityType(.distanceWalkingRunning), shape: .total(.distanceWalkingRunning, .meter(), label: "m"), delivery: .hourly),
        Stream(id: "flights", type: HKQuantityType(.flightsClimbed), shape: .total(.flightsClimbed, .count(), label: "count"), delivery: .hourly),
        Stream(id: "daylight", type: HKQuantityType(.timeInDaylight), shape: .total(.timeInDaylight, .minute(), label: "min"), delivery: .hourly),
    ]

    static let records: [Stream] = [
        Stream(id: "heart_rate", type: HKQuantityType(.heartRate), shape: .quantity(.heartRate, perMinute, label: "count/min"), delivery: .hourly),
        Stream(id: "resting_hr", type: HKQuantityType(.restingHeartRate), shape: .quantity(.restingHeartRate, perMinute, label: "count/min"), delivery: .immediate),
        Stream(id: "walking_hr", type: HKQuantityType(.walkingHeartRateAverage), shape: .quantity(.walkingHeartRateAverage, perMinute, label: "count/min"), delivery: .immediate),
        Stream(id: "hrv", type: HKQuantityType(.heartRateVariabilitySDNN), shape: .quantity(.heartRateVariabilitySDNN, .secondUnit(with: .milli), label: "ms"), delivery: .immediate),
        Stream(id: "heartbeats", type: HKSeriesType.heartbeat(), shape: .heartbeats, delivery: .immediate),
        Stream(id: "oxygen", type: HKQuantityType(.oxygenSaturation), shape: .quantity(.oxygenSaturation, .percent(), label: "fraction"), delivery: .immediate),
        Stream(id: "respiratory", type: HKQuantityType(.respiratoryRate), shape: .quantity(.respiratoryRate, perMinute, label: "count/min"), delivery: .hourly),
        Stream(id: "wrist_temp", type: HKQuantityType(.appleSleepingWristTemperature), shape: .quantity(.sleepingWristTemperature, .degreeCelsius(), label: "degC"), delivery: .immediate),
        Stream(id: "vo2_max", type: HKQuantityType(.vo2Max), shape: .quantity(.vo2Max, HKUnit.literUnit(with: .milli).unitDivided(by: HKUnit.gramUnit(with: .kilo).unitMultiplied(by: .minute())), label: "mL/(kg*min)"), delivery: .immediate),
        Stream(id: "env_audio", type: HKQuantityType(.environmentalAudioExposure), shape: .quantity(.environmentalAudioExposure, .decibelAWeightedSoundPressureLevel(), label: "dBASPL"), delivery: .hourly),
        Stream(id: "headphone_audio", type: HKQuantityType(.headphoneAudioExposure), shape: .quantity(.headphoneAudioExposure, .decibelAWeightedSoundPressureLevel(), label: "dBASPL"), delivery: .hourly),
        Stream(id: "sleep", type: HKCategoryType(.sleepAnalysis), shape: .sleep, delivery: .immediate),
        Stream(id: "workouts", type: HKObjectType.workoutType(), shape: .workout, delivery: .immediate),
        Stream(id: "mindful", type: HKCategoryType(.mindfulSession), shape: .mindful, delivery: .immediate),
        Stream(id: "stand_hour", type: HKCategoryType(.appleStandHour), shape: .standHour, delivery: .hourly),
    ]

    private let kind: Kind
    private let store = HKHealthStore()
    private var observers: [HKObserverQuery] = []
    private var requested = false
    /// streams being read now: the observer and a wake may both ask, and one read at a time keeps the anchors in order
    private var reading: Set<String> = []

    init(_ kind: Kind) { self.kind = kind }

    var name: String { kind == .totals ? "steps" : "health" }
    private var streams: [Stream] { kind == .totals ? Self.totals : Self.records }

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

    func start() {
        guard Self.entitled, HKHealthStore.isHealthDataAvailable() else { return }
        // The observers are registered at once: a background launch for a Health delivery needs them in place
        // before the launch settles, not after the authorization question has been asked.
        for s in streams { register(s) }
        Task {
            // all the types at once: one sheet lists whatever was not asked yet
            let types = Set(streams.map(\.type) as [HKObjectType])
            if (try? await store.statusForAuthorizationRequest(toShare: [], read: types)) == .shouldRequest {
                try? await store.requestAuthorization(toShare: [], read: types)
            }
            requested = true
            for s in streams { try? await store.enableBackgroundDelivery(for: s.type, frequency: s.delivery) }
            await wake()
            Reporter.shared.refreshStatus()
        }
    }

    func stop() {
        guard Self.entitled else { return }
        observers.forEach(store.stop)
        observers = []
        for s in streams { Task { try? await store.disableBackgroundDelivery(for: s.type) } }
    }

    func wake() async {
        guard Self.entitled else { return }
        for s in streams { await sync(s) }
    }

    /// The observer is what HealthKit calls, also in a background launch, when new samples arrive. It must
    /// tell HealthKit it is done, or delivery to the app slows down.
    private func register(_ s: Stream) {
        let q = HKObserverQuery(sampleType: s.type, predicate: nil) { [weak self] _, done, error in
            Task { @MainActor in
                if error == nil { await self?.sync(s) }
                done()
            }
        }
        store.execute(q)
        observers.append(q)
    }

    private func sync(_ s: Stream) async {
        guard reading.insert(s.id).inserted else { return }
        defer { reading.remove(s.id) }
        switch s.shape {
        case .total(let metric, let unit, let label): await syncTotal(s, metric, unit, label)
        case .quantity(let metric, let unit, let label):
            await syncSamples(s, HKSamplePredicate.quantitySample(type: s.quantityType, predicate: since(s))) { sample in
                [.measure(Self.measure(sample, metric, unit, label))]
            }
        case .sleep:
            await syncSamples(s, HKSamplePredicate.categorySample(type: HKCategoryType(.sleepAnalysis), predicate: since(s))) { sample in
                Self.sleepSample(sample).map { [$0] } ?? []
            }
        case .standHour:
            await syncSamples(s, HKSamplePredicate.categorySample(type: HKCategoryType(.appleStandHour), predicate: since(s))) { sample in
                Self.standHour(sample).map { [$0] } ?? []
            }
        case .mindful:
            await syncSamples(s, HKSamplePredicate.categorySample(type: HKCategoryType(.mindfulSession), predicate: since(s))) { sample in
                [Self.mindful(sample)]
            }
        case .workout:
            await syncSamples(s, HKSamplePredicate.workout(since(s))) { workout in [Self.workoutFrame(workout)] }
        case .heartbeats:
            await syncSeries(s)
        }
    }

    // MARK: Totals

    private func syncTotal(_ s: Stream, _ metric: HealthMetric, _ unit: HKUnit, _ label: String) async {
        let now = Date(), cal = Calendar.current
        // Closed hours of the last 24 hours; the hour in progress waits until it is over.
        guard let end = cal.dateInterval(of: .hour, for: now)?.start,
              let start = cal.dateInterval(of: .hour, for: now - ReportMemory.firstLookback)?.start, end > start else { return }
        let query = HKStatisticsCollectionQueryDescriptor(
            predicate: .quantitySample(type: s.quantityType,
                                       predicate: HKQuery.predicateForSamples(withStart: start, end: end)),
            options: .cumulativeSum, anchorDate: start, intervalComponents: DateComponents(hour: 1))
        guard let result = try? await query.result(for: store) else { return }  // locked phone: try at the next wake
        var buckets: [(start: Date, end: Date, value: Double)] = []
        result.enumerateStatistics(from: start, to: end) { stat, _ in
            buckets.append((stat.startDate, stat.endDate, stat.sumQuantity()?.doubleValue(for: unit) ?? 0))
        }
        let key = "health.t.\(s.id).sent"
        var sent = ReportMemory.data(key).flatMap { try? JSONDecoder().decode([String: Double].self, from: $0) } ?? [:]
        let out = Self.newTotals(metric, label: label, buckets, sent: &sent)
        sent = sent.filter { (Double($0.key) ?? 0) >= start.timeIntervalSince1970 }
        ReportMemory.set(data: try? JSONEncoder().encode(sent), for: key)
        Reporter.shared.emit(out)
    }

    /// The hourly totals to send: those above zero whose total differs from the one last sent for that hour
    /// (`sent`, by start second, is updated). A different total is a new frame for the same span.
    nonisolated static func newTotals(_ metric: HealthMetric, label: String, _ buckets: [(start: Date, end: Date, value: Double)], sent: inout [String: Double]) -> [Sensed] {
        var out: [Sensed] = []
        for b in buckets {
            // counts are whole; energy, distance and minutes keep hundredths
            let n = metric == .steps || metric == .flightsClimbed ? b.value.rounded() : (b.value * 100).rounded() / 100
            let hour = String(Int64(b.start.timeIntervalSince1970))
            guard n > 0, sent[hour] != n else { continue }
            sent[hour] = n
            out.append(.health(metric: metric, start: b.start, end: b.end, value: n, detail: nil, extra: HealthExtra(unit: label)))
        }
        return out
    }

    // MARK: Samples

    /// Where the first read of a type begins: 24 hours back, fixed the first time so later reads (with the anchor) never go further back.
    private func since(_ s: Stream) -> NSPredicate {
        let key = "health.r.\(s.id).since"
        let from = ReportMemory.date(key) ?? Date() - ReportMemory.firstLookback
        ReportMemory.set(date: from, for: key)
        return HKQuery.predicateForSamples(withStart: from, end: nil)
    }

    private func anchor(_ s: Stream) -> HKQueryAnchor? {
        ReportMemory.data("health.r.\(s.id).anchor").flatMap { try? NSKeyedUnarchiver.unarchivedObject(ofClass: HKQueryAnchor.self, from: $0) }
    }

    private func save(_ anchor: HKQueryAnchor, _ s: Stream) {
        ReportMemory.set(data: try? NSKeyedArchiver.archivedData(withRootObject: anchor, requiringSecureCoding: true), for: "health.r.\(s.id).anchor")
    }

    /// Reads what is new since the saved anchor, queues its frames, and only then moves the anchor, so a failure between loses nothing.
    private func syncSamples<S: HKSample>(_ s: Stream, _ predicate: HKSamplePredicate<S>, _ frames: (S) -> [Sensed]) async {
        let query = HKAnchoredObjectQueryDescriptor(predicates: [predicate], anchor: anchor(s))
        guard let result = try? await query.result(for: store) else { return }  // locked phone: try at the next wake
        Reporter.shared.emit(result.addedSamples.flatMap(frames))
        save(result.newAnchor, s)
    }

    /// Heartbeat series: the beat times of each new series, fetched one series at a time, cut into frames of at most `maxBeats`.
    private func syncSeries(_ s: Stream) async {
        let query = HKAnchoredObjectQueryDescriptor(
            predicates: [.sample(type: HKSeriesType.heartbeat(), predicate: since(s))], anchor: anchor(s))
        guard let result = try? await query.result(for: store) else { return }
        for series in result.addedSamples.compactMap({ $0 as? HKHeartbeatSeriesSample }) {
            guard let beats = await Self.beats(of: series, in: store) else { return }  // not read: the anchor stays, the series comes again
            Reporter.shared.emit(Self.seriesFrames(series, beats))
        }
        save(result.newAnchor, s)
    }

    private nonisolated static func beats(of series: HKHeartbeatSeriesSample, in store: HKHealthStore) async -> [HeartBeat]? {
        await withCheckedContinuation { c in
            var beats: [HeartBeat] = []
            let q = HKHeartbeatSeriesQuery(heartbeatSeries: series) { _, offset, gap, done, error in
                if error != nil { return c.resume(returning: nil) }
                beats.append(HeartBeat(offsetMs: Int64((offset * 1000).rounded()), gap: gap))
                if done { c.resume(returning: beats) }
            }
            store.execute(q)
        }
    }

    // MARK: Frames (pure)

    nonisolated static func seriesFrames(_ series: HKHeartbeatSeriesSample, _ beats: [HeartBeat]) -> [Sensed] {
        seriesFrames(uuid: series.uuid.uuidString, start: series.startDate, end: series.endDate, beats, source: sourceName(series), device: deviceName(series.device),
                     meta: meta(series.metadata))
    }

    /// A series of any length as frames the brain takes; a long one is `part` 0, 1, ... with offsets from the series' own start.
    nonisolated static func seriesFrames(uuid: String, start: Date, end: Date, _ beats: [HeartBeat], source: String?, device: String?, meta: [String: MetaValue]) -> [Sensed] {
        guard !beats.isEmpty else { return [] }
        let per = ReportFrame.maxBeats - 1000                  // room under the frame's 64 KiB for the longest offsets
        let parts = stride(from: 0, to: beats.count, by: per).map { Array(beats[$0..<min($0 + per, beats.count)]) }
        return parts.enumerated().map { i, part in
            .measure(MeasureRecord(metric: .heartbeatSeries, start: start, end: end, value: Double(part.count), unit: "count", uuid: uuid, source: source, device: device,
                                   meta: meta, beats: part, part: parts.count > 1 ? i : nil))
        }
    }

    private nonisolated static func measure(_ q: HKQuantitySample, _ metric: MeasureMetric, _ unit: HKUnit, _ label: String) -> MeasureRecord {
        MeasureRecord(metric: metric, start: q.startDate, end: q.endDate, value: q.quantity.doubleValue(for: unit), unit: label, uuid: q.uuid.uuidString,
                      source: sourceName(q), device: deviceName(q.device), meta: meta(q.metadata))
    }

    private nonisolated static func extra(_ s: HKSample, unit: String, meta extra: [String: MetaValue] = [:]) -> HealthExtra {
        HealthExtra(unit: unit, source: sourceName(s), device: deviceName(s.device), uuid: s.uuid.uuidString, meta: meta(s.metadata).merging(extra) { $1 })
    }

    nonisolated static func sleepStage(_ raw: Int) -> String? {
        switch HKCategoryValueSleepAnalysis(rawValue: raw) {
        case .inBed: "in_bed"
        case .awake: "awake"
        case .asleepUnspecified: "unspecified"
        case .asleepCore: "core"
        case .asleepDeep: "deep"
        case .asleepREM: "rem"
        default: nil
        }
    }

    private nonisolated static func sleepSample(_ c: HKCategorySample) -> Sensed? {
        guard let stage = sleepStage(c.value) else { return nil }
        return .health(metric: .sleep, start: c.startDate, end: c.endDate, value: Double(c.value), detail: stage, extra: extra(c, unit: "category"))
    }

    private nonisolated static func standHour(_ c: HKCategorySample) -> Sensed? {
        let state: String
        switch HKCategoryValueAppleStandHour(rawValue: c.value) {
        case .stood: state = "stood"
        case .idle: state = "idle"
        default: return nil
        }
        return .health(metric: .standHour, start: c.startDate, end: c.endDate, value: Double(c.value), detail: state, extra: extra(c, unit: "category"))
    }

    private nonisolated static func mindful(_ c: HKCategorySample) -> Sensed {
        .health(metric: .mindfulSession, start: c.startDate, end: c.endDate, value: Double(c.value), detail: nil, extra: extra(c, unit: "category"))
    }

    private nonisolated static func workoutFrame(_ w: HKWorkout) -> Sensed {
        func sum(_ id: HKQuantityTypeIdentifier, _ unit: HKUnit) -> Double? { w.statistics(for: HKQuantityType(id))?.sumQuantity()?.doubleValue(for: unit) }
        var m: [String: MetaValue] = ["activity_type": .number(Double(w.workoutActivityType.rawValue))]
        if let e = sum(.activeEnergyBurned, .kilocalorie()) { m["total_energy_kcal"] = .number(e) }
        if let d = [.distanceWalkingRunning, .distanceCycling, .distanceSwimming].lazy.compactMap({ sum($0, .meter()) }).first { m["distance_m"] = .number(d) }
        let hr = w.statistics(for: HKQuantityType(.heartRate))
        if let a = hr?.averageQuantity()?.doubleValue(for: perMinute) { m["avg_heart_rate"] = .number(a) }
        if let x = hr?.maximumQuantity()?.doubleValue(for: perMinute) { m["max_heart_rate"] = .number(x) }
        return .health(metric: .workout, start: w.startDate, end: w.endDate, value: w.duration, detail: workoutName(w.workoutActivityType), extra: extra(w, unit: "s", meta: m))
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

    // MARK: Source and metadata

    nonisolated static func sourceName(_ s: HKSample) -> String? { s.sourceRevision.source.name }

    /// `Watch7,1` style: the hardware version when Health has it, else the model, else the name.
    nonisolated static func deviceName(_ d: HKDevice?) -> String? { d.flatMap { $0.hardwareVersion ?? $0.model ?? $0.name } }

    /// The metadata that carries meaning, by our names: the zone the sample was taken in, whether the user typed it, the motion context of a
    /// heart rate and whether a workout was indoors. Everything else Health holds is left behind.
    nonisolated static func meta(_ md: [String: Any]?) -> [String: MetaValue] {
        guard let md else { return [:] }
        var out: [String: MetaValue] = [:]
        if let z = md[HKMetadataKeyTimeZone] as? String { out["time_zone"] = .text(z) }
        if let u = (md[HKMetadataKeyWasUserEntered] as? NSNumber)?.boolValue { out["user_entered"] = .flag(u) }
        if let i = (md[HKMetadataKeyIndoorWorkout] as? NSNumber)?.boolValue { out["indoor"] = .flag(i) }
        if let c = (md[HKMetadataKeyHeartRateMotionContext] as? NSNumber)?.intValue {
            switch HKHeartRateMotionContext(rawValue: c) {
            case .sedentary: out["motion_context"] = .text("sedentary")
            case .active: out["motion_context"] = .text("active")
            default: out["motion_context"] = .text("not_set")
            }
        }
        return out
    }
}
