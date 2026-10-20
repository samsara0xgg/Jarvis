import CoreMotion

/// One classification of the motion coprocessor: from `start` until the next sample.
struct MotionSample: Equatable {
    var start: Date
    var activity: Activity
    var confidence: Confidence
    /// The coprocessor's stationary flag, which can come with `automotive` (a car at a red light): the activity says automotive, this says still.
    var still = false
}

/// Walking, riding or staying put, read as history in short wakes (never streamed): the coprocessor keeps about
/// a week of it, so the app asks "what happened since I last looked" when it wakes anyway.
@MainActor
final class MotionCollector: Collector {
    let name = "motion"

    /// A closed segment shorter than this is a flicker of the classifier and is not reported.
    nonisolated static let minSegment: TimeInterval = 45

    private let manager = CMMotionActivityManager()

    var access: ReportAccess {
        guard CMMotionActivityManager.isActivityAvailable() else { return .unavailable }
        return switch CMMotionActivityManager.authorizationStatus() {
        case .notDetermined: .notAsked
        case .authorized: .granted
        default: .denied
        }
    }

    // The first history query is what makes iOS ask for Motion & Fitness access.
    func start() { Task { await wake(); Reporter.shared.refreshStatus() } }
    func stop() {}

    func wake() async {
        guard CMMotionActivityManager.isActivityAvailable() else { return }
        let now = Date()
        let from = max(ReportMemory.date("motion.cursor") ?? .distantPast, now - ReportMemory.firstLookback)
        let samples = await history(from: from, to: now)
        let observations = Self.observations(samples, now: now)
        // The next read starts where the open segment started, so that segment is read whole again.
        guard case .motion(_, _, let openStart, _)? = observations.last else { return }
        // The open segment is sent once; asking again from its start finds it again until it ends.
        let openUid = ReportMemory.string("motion.open")
        var emitted: [Sensed] = []
        for o in observations {
            guard let frame = ReportFrame(o, now: now) else { continue }
            if case .motion(_, _, _, nil) = o {
                if frame.eventUid == openUid { continue }
                ReportMemory.set(string: frame.eventUid, for: "motion.open")
            }
            emitted.append(o)
        }
        Reporter.shared.emit(emitted)
        ReportMemory.set(date: openStart, for: "motion.cursor")
        Reporter.shared.movement(Self.latest(samples))
    }

    private func history(from: Date, to: Date) async -> [MotionSample] {
        await withCheckedContinuation { cont in
            manager.queryActivityStarting(from: from, to: to, to: OperationQueue()) { activities, _ in
                cont.resume(returning: (activities ?? []).map(Self.sample))
            }
        }
    }

    nonisolated static func sample(_ a: CMMotionActivity) -> MotionSample {
        let activity: Activity = a.automotive ? .automotive : a.cycling ? .cycling : a.running ? .running
            : a.walking ? .walking : a.stationary ? .stationary : .unknown
        let confidence: Confidence = switch a.confidence {
        case .high: .high
        case .medium: .medium
        default: .low
        }
        return MotionSample(start: a.startDate, activity: activity, confidence: confidence, still: a.stationary)
    }

    /// Segments of one activity, ended where the next one starts; the last one stays open. A segment's
    /// confidence is the one that covered most of its time.
    nonisolated static func observations(_ samples: [MotionSample], now: Date) -> [Sensed] {
        struct Run { var activity: Activity; var start: Date; var end: Date?; var weight: [Confidence: TimeInterval] = [:] }
        var runs: [Run] = []
        let sorted = samples.sorted { $0.start < $1.start }
        for (i, s) in sorted.enumerated() {
            let end: Date? = i + 1 < sorted.count ? sorted[i + 1].start : nil
            let time = (end ?? now).timeIntervalSince(s.start)
            if runs.last?.activity == s.activity {
                runs[runs.count - 1].end = end
                runs[runs.count - 1].weight[s.confidence, default: 0] += time
            } else {
                runs.append(Run(activity: s.activity, start: s.start, end: end, weight: [s.confidence: time]))
            }
        }
        return runs.compactMap { r in
            if let end = r.end, end.timeIntervalSince(r.start) < minSegment { return nil }
            let confidence = r.weight.max { $0.value < $1.value }?.key ?? .low
            return .motion(activity: r.activity, confidence: confidence, startedAt: r.start, endedAt: r.end)
        }
    }

    /// What the newest sample says he is doing, or nil when it is not sure (low confidence or unknown).
    nonisolated static func current(_ samples: [MotionSample]) -> Activity? { latest(samples)?.activity }

    /// The newest sample, or nil when it is not sure (low confidence or unknown).
    nonisolated static func latest(_ samples: [MotionSample]) -> MotionSample? {
        guard let last = samples.max(by: { $0.start < $1.start }), last.confidence != .low, last.activity != .unknown else { return nil }
        return last
    }
}
