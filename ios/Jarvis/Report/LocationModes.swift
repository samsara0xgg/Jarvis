import CoreLocation

/// When standard updates run and how (ADR 0312), decided only from what the location collector hears: fixes, motion samples, signs
/// that he left (a visit's departure, the rest fence, CarPlay) and the clock. No managers and no timers, so a drive can be replayed in a
/// test; the collector feeds it and applies `plan`, and wakes it at `deadline`.
struct LocationModes {
    enum Mode: Equatable {
        /// Visits, significant changes and the rest geofence only. GPS is off and iOS may suspend the app.
        case deepSleep
        /// GPS on, every fix kept. In a vehicle at its best accuracy.
        case moving
        /// Updates at three kilometres: GPS off, but the app keeps running and live motion keeps coming, so a move is caught at once.
        case lightSleep
    }

    /// What the collector should be running now.
    struct Plan: Equatable {
        var updates: Bool
        var accuracy: CLLocationAccuracy
        var activityType: CLActivityType
        var liveMotion: Bool
        /// Device motion is recorded only in a vehicle.
        var recordMotion: Bool
    }

    private(set) var mode = Mode.deepSleep
    /// He is in a vehicle: only while moving.
    private(set) var vehicle = false
    private(set) var activity: Activity?
    /// The last fix that counted as a move (150 m from the one before), what the idle clock and a light sleep measure from.
    private(set) var anchor: CLLocation?
    /// The last sign of moving; the idle clock runs from it.
    private var movedAt: Date?
    /// Motion has said still since then (outside a vehicle).
    private var stillSince: Date?
    /// Fixes have been at vehicle speed since then.
    private var fastSince: Date?
    private var sleptAt: Date?

    var plan: Plan {
        switch mode {
        case .deepSleep:
            Plan(updates: false, accuracy: LocationTuning.movingAccuracy, activityType: .other, liveMotion: false, recordMotion: false)
        case .moving:
            Plan(updates: true, accuracy: vehicle ? LocationTuning.vehicleAccuracy : LocationTuning.movingAccuracy,
                 activityType: LocationTuning.activityType(for: vehicle ? .automotive : activity), liveMotion: true, recordMotion: vehicle)
        case .lightSleep:
            Plan(updates: true, accuracy: LocationTuning.sleepAccuracy, activityType: .other, liveMotion: true, recordMotion: false)
        }
    }

    /// When `tick` has something to do, if ever.
    var deadline: Date? {
        switch mode {
        case .deepSleep: nil
        case .lightSleep: sleptAt.map { $0 + LocationTuning.lightSleep }
        case .moving:
            if vehicle { movedAt.map { $0 + LocationTuning.vehicleStop } } else {
                [movedAt.map { $0 + LocationTuning.idleTimeout }, stillSince.map { $0 + LocationTuning.stationaryGrace }].compactMap { $0 }.min()
            }
        }
    }

    // MARK: what he does

    /// A sign that he is on the move (a visit's departure, the rest fence left, a fresh significant change, CarPlay, a motion sample
    /// that is not still). `automotive` puts him in a vehicle, walking, running or cycling take him out. True when it woke the deep sleep.
    @discardableResult
    mutating func moved(_ now: Date, activity a: Activity? = nil, at loc: CLLocation? = nil) -> Bool {
        let woke = mode == .deepSleep
        mode = .moving
        sleptAt = nil
        stillSince = nil
        movedAt = now
        if let loc { anchor = loc }
        if let a { activity = a }
        switch a {
        case .automotive: vehicle = true
        case .walking, .running, .cycling: vehicle = false; fastSince = nil
        default: break
        }
        return woke
    }

    /// A motion sample, live or the newest of the history. Outside a vehicle, still starts the stationary grace and anything else is a
    /// move. In a vehicle the fixes say when the car stops (motion says automotive at a red light and while parked alike), so only
    /// walking, running or cycling counts: he got out. True when it woke the deep sleep.
    @discardableResult
    mutating func motion(_ s: MotionSample, now: Date) -> Bool {
        guard s.confidence != .low, s.activity != .unknown else { return false }
        let still = s.still || s.activity == .stationary
        if mode == .moving && vehicle {
            if !still, [.walking, .running, .cycling].contains(s.activity) { moved(now, activity: s.activity) }
            return false
        }
        if still {
            if mode == .moving, stillSince == nil { stillSince = min(s.start, now) }
            return false
        }
        return moved(now, activity: s.activity)
    }

    /// A fix while updates run. Moving: a fix 150 m from the anchor, or (in a vehicle) one at walking pace or faster, is a move; ten
    /// seconds at vehicle speed outside cycling puts him in a vehicle. Light sleep: a coarse fix at walking pace, or farther from the
    /// anchor than both their accuracies and 150 m, wakes GPS.
    mutating func fix(_ loc: CLLocation, now: Date) {
        switch mode {
        case .deepSleep:
            return
        case .lightSleep:
            let far = anchor.map { loc.distance(from: $0) - loc.horizontalAccuracy - $0.horizontalAccuracy > LocationTuning.idleRadius } ?? false
            if loc.speed >= LocationTuning.movingSpeed || far { moved(now) }
        case .moving:
            if loc.speed >= 0 {
                if loc.speed >= LocationTuning.vehicleSpeed {
                    let since = fastSince ?? loc.timestamp
                    fastSince = since
                    if loc.timestamp.timeIntervalSince(since) >= LocationTuning.vehicleSpeedFor, activity != .cycling { vehicle = true }
                } else {
                    fastSince = nil
                }
                if loc.speed >= LocationTuning.movingSpeed {
                    stillSince = nil
                    if vehicle { movedAt = now }
                }
            }
            if anchor.map({ loc.distance(from: $0) >= LocationTuning.idleRadius }) ?? true {
                anchor = loc
                movedAt = now
            }
        }
    }

    // MARK: the clock

    /// What is due at `now`: a stop puts GPS to sleep, a long light sleep becomes the deep one.
    mutating func tick(_ now: Date) {
        guard let due = deadline, now >= due else { return }
        switch mode {
        case .moving: sleep(now)
        case .lightSleep: deepSleep()
        case .deepSleep: break
        }
    }

    /// GPS off, the app awake (the stop of ADR 0312's first step).
    mutating func sleep(_ now: Date) {
        mode = .lightSleep
        sleptAt = now
        vehicle = false
        stillSince = nil
        fastSince = nil
    }

    /// Back to visits, significant changes and the rest fence (authorization lost, or a light sleep ran out).
    mutating func deepSleep() {
        mode = .deepSleep
        vehicle = false
        activity = nil
        anchor = nil
        movedAt = nil
        stillSince = nil
        fastSince = nil
        sleptAt = nil
    }
}

/// Cuts the run of fixes into track frames (ADR 0312): a frame holds less than a minute from its first fix, at most
/// `ReportFrame.maxTrackFixes`, and is sent when the next fix would not fit, or when updates sleep.
struct TrackChunker {
    static let span: TimeInterval = 60

    private(set) var open: [TrackFix] = []

    /// Adds a fix; the frame it closed, if any. A fix older than the one before starts a frame of its own, so every frame is in time order.
    mutating func add(_ f: TrackFix) -> [TrackFix]? {
        var done: [TrackFix]?
        if let first = open.first, f.at.timeIntervalSince(first.at) >= Self.span || f.at < open[open.count - 1].at { done = close() }
        open.append(f)
        if open.count >= ReportFrame.maxTrackFixes { return close() }  // never right after a close: that leaves one fix open
        return done
    }

    /// The frame being filled, now.
    mutating func close() -> [TrackFix]? {
        defer { open = [] }
        return open.isEmpty ? nil : open
    }
}

extension TrackFix {
    /// A fix as iOS reported it; what iOS marks invalid (a negative accuracy or value) is left out.
    init(_ loc: CLLocation) {
        let source = loc.sourceInformation
        self.init(
            at: loc.timestamp, lat: loc.coordinate.latitude, lng: loc.coordinate.longitude, accuracyM: loc.horizontalAccuracy,
            altitudeM: loc.verticalAccuracy >= 0 ? loc.altitude : nil, verticalAccuracyM: loc.verticalAccuracy >= 0 ? loc.verticalAccuracy : nil,
            speedMps: loc.speed >= 0 ? loc.speed : nil, speedAccuracyMps: loc.speedAccuracy >= 0 ? loc.speedAccuracy : nil,
            courseDeg: loc.course >= 0 ? loc.course : nil, courseAccuracyDeg: loc.courseAccuracy >= 0 ? loc.courseAccuracy : nil,
            simulated: source?.isSimulatedBySoftware ?? false, accessory: source?.isProducedByAccessory ?? false)
    }
}
