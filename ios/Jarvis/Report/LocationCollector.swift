import CoreLocation
import CoreMotion
import os

/// The knobs of the adaptive mode, all here. Visits, significant changes and one rest geofence are always
/// armed and cost next to nothing; standard updates run only while he moves, then stop by themselves. Opening the
/// app starts nothing: he does not want the app on screen to be locating him.
enum LocationTuning {
    /// While moving: GPS at its second-best accuracy, every fix delivered (`kCLDistanceFilterNone`); the thinning
    /// below decides what is sent, but it does not make the GPS cheaper. The battery cost of a trip is this.
    static let movingAccuracy: CLLocationAccuracy = kCLLocationAccuracyNearestTenMeters
    static let movingDistanceFilter: CLLocationDistance = kCLDistanceFilterNone
    /// Updates running without proof that he moves (a visit's departure): Wi-Fi and cell positioning.
    static let watchAccuracy: CLLocationAccuracy = kCLLocationAccuracyHundredMeters
    static let watchDistanceFilter: CLLocationDistance = 50
    /// Standard updates stop after this long without a fix `idleRadius` away from the last one that counted.
    /// The radius is above the jitter of a 100 m fix, so a phone lying still does not keep the mode alive.
    static let idleTimeout: TimeInterval = 5 * 60
    static let idleRadius: CLLocationDistance = 150
    /// Live motion says stationary for this long (a red light is not a stop): updates stop.
    static let stationaryGrace: TimeInterval = 90
    /// While updating: at most one frame per `minInterval`, at least `minSeparation` from the last frame.
    static let minInterval: TimeInterval = 30
    static let minSeparation: CLLocationDistance = 50
    /// One fix this fast (m/s, walking pace) says he is moving even when no motion history is read.
    static let movingSpeed: CLLocationSpeed = 1.0
    /// The geofence armed where he rests; leaving it restarts tracking (and relaunches a terminated app).
    static let restRadius: CLLocationDistance = 200
    /// Reverse geocoding: once per new place, never more often than this, never longer than `geocodeTimeout`.
    static let geocodeSpacing: TimeInterval = 60
    static let geocodeTimeout: TimeInterval = 6

    /// Whether a fix taken while updating is sent: far enough and late enough after the last one sent.
    static func passesThinning(_ loc: CLLocation, after last: CLLocation?) -> Bool {
        guard let last else { return true }
        return loc.timestamp.timeIntervalSince(last.timestamp) >= minInterval && loc.distance(from: last) >= minSeparation
    }

    /// Tells iOS what he is doing, which tunes its own power saving: walking and cycling are fitness, a car is
    /// navigation, anything unknown is the general kind.
    static func activityType(for activity: Activity?) -> CLActivityType {
        switch activity {
        case .automotive: .automotiveNavigation
        case .walking, .running, .cycling: .fitness
        default: .otherNavigation
        }
    }
}

/// Where he goes. Always armed: visits, significant changes (iOS relaunches the app for both) and a rest geofence
/// around where he last stopped. Adaptive: standard updates while he moves. The coordinates leave this object only
/// as frames.
@MainActor
final class LocationCollector: NSObject, Collector, CLLocationManagerDelegate {
    let name = "location"

    private nonisolated static let fenceID = "com.allen.jarvis.rest"

    private let manager = CLLocationManager()
    private let motionManager = CMMotionActivityManager()
    /// States only, never a coordinate.
    private let log = Logger(subsystem: "com.allen.jarvis", category: "location")
    /// Standard updates are running.
    private(set) var updating = false
    /// Updates are running and he is known to be moving: the fine accuracy applies.
    private(set) var moving = false
    private var activity: Activity?
    private var oneShotPending = false
    private var begunAt: Date?
    private var lastFix: CLLocation?
    private var lastSent: CLLocation?
    private var thinned: CLLocation?  // newest fix dropped by the thinning, sent when updates stop
    private var anchor: CLLocation?   // last fix that counted as moving, for the idle timeout
    private var idle: Task<Void, Never>?
    private var rest: Task<Void, Never>?
    private var geocoded: (loc: CLLocation, place: String?)?
    private var geocodedAt: Date?

    var access: ReportAccess {
        guard CLLocationManager.locationServicesEnabled() else { return .unavailable }
        return switch manager.authorizationStatus {
        case .notDetermined: .notAsked
        case .denied, .restricted: .denied
        case .authorizedWhenInUse: .partial
        // Reduced accuracy kills region monitoring and degrades significant changes.
        case .authorizedAlways: manager.accuracyAuthorization == .reducedAccuracy ? .partial : .granted
        @unknown default: .unavailable
        }
    }

    var fix: AccessFix? {
        switch manager.authorizationStatus {
        case .authorizedWhenInUse: .allowAlways
        case .authorizedAlways: manager.accuracyAuthorization == .reducedAccuracy ? .allowPrecise : nil
        default: nil
        }
    }

    func start() {
        manager.delegate = self
        authorizationChanged()
    }

    func stop() {
        exitUpdates()
        removeRestFence()
        manager.stopMonitoringVisits()
        manager.stopMonitoringSignificantLocationChanges()
    }

    func wake() async {}

    /// Motion history says what he is doing now (`Reporter.movement`).
    func motion(_ now: Activity?) {
        switch now {
        case .walking, .running, .cycling, .automotive: enterMoving(activity: now)
        case .stationary: if moving { exitUpdates() }
        default: break
        }
    }

    // MARK: Authorization

    private func authorizationChanged() {
        switch manager.authorizationStatus {
        case .notDetermined:
            manager.requestWhenInUseAuthorization()
        case .authorizedWhenInUse:
            begin()
            // The upgrade to Always is asked once; iOS shows its own prompt, and the user may keep "while using".
            if ReportMemory.string("location.askedAlways") == nil {
                ReportMemory.set(string: "yes", for: "location.askedAlways")
                manager.requestAlwaysAuthorization()
            }
        case .authorizedAlways:
            begin()
        default:
            exitUpdates()
            removeRestFence()
        }
        Reporter.shared.refreshStatus()
    }

    private func begin() {
        if begunAt == nil { begunAt = Date() }
        manager.startMonitoringVisits()
        manager.startMonitoringSignificantLocationChanges()
        // One fix when the switch is first turned on, so the brain knows where he is before the first move.
        // It is asked again at each launch until a fix has been reported.
        if ReportMemory.string("location.firstFix") == nil {
            oneShotPending = true
            manager.requestLocation()
        }
    }

    nonisolated func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        Task { @MainActor in self.authorizationChanged() }
    }

    // MARK: Visits

    // Each callback asks iOS for background time first: a wake gives seconds, and the request that follows
    // needs them. Frames are queued before any slow step (the place name), and the flush starts at once.

    nonisolated func locationManager(_ manager: CLLocationManager, didVisit visit: CLVisit) {
        let hold = MainActor.assumeIsolated { BackgroundHold.begin("jarvis.location.visit") }
        let coordinate = visit.coordinate, accuracy = visit.horizontalAccuracy
        let arrival = visit.arrivalDate, departure = visit.departureDate
        Task { @MainActor in
            await self.visited(coordinate, accuracy, arrival, departure)
            hold.end()
        }
    }

    private func visited(_ c: CLLocationCoordinate2D, _ accuracy: Double, _ arrival: Date, _ departure: Date) async {
        let arrived: Date? = arrival == .distantPast ? nil : arrival
        let departed: Date? = departure == .distantFuture ? nil : departure
        // With no known arrival the visit is "he left here at T", which is what the brain reads it as.
        guard let start = arrived ?? departed else { return }
        let loc = CLLocation(latitude: c.latitude, longitude: c.longitude)
        func emit(_ place: String?) {
            Reporter.shared.emit([.visit(lat: c.latitude, lng: c.longitude, accuracyM: accuracy, arrivedAt: start,
                                         departedAt: departed, place: place)])
        }
        emit(nil)
        // Leaving a place is a wake that says he is on the move; arriving is where the rest geofence goes.
        if departed != nil { enterMoving() } else if !updating { armRestFence(at: loc) }
        // The place name is a second frame (the brain keeps the later row of a visit), never a wait for the first.
        if let place = await place(for: loc) { emit(place) }
    }

    // MARK: Fixes

    nonisolated func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        let hold = MainActor.assumeIsolated { BackgroundHold.begin("jarvis.location.fix") }
        Task { @MainActor in
            for loc in locations.sorted(by: { $0.timestamp < $1.timestamp }) where loc.horizontalAccuracy >= 0 {
                self.fix(loc)
            }
            hold.end()
        }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {}

    nonisolated func locationManager(_ manager: CLLocationManager, didExitRegion region: CLRegion) {
        guard region.identifier == Self.fenceID else { return }
        let hold = MainActor.assumeIsolated { BackgroundHold.begin("jarvis.location.fence") }
        Task { @MainActor in
            self.log.info("left the rest fence")
            self.enterMoving()
            hold.end()
        }
    }

    private func fix(_ loc: CLLocation) {
        lastFix = loc
        if moving {
            track(loc)
            send(loc)
            return
        }
        // Not known to be moving: a significant-change fix, the one-shot, or a fix while the app watches.
        // A fresh significant-change fix means he went about 500 m, so tracking starts; so does a fix at walking
        // pace. The one-shot, and an old fix iOS hands over when monitoring starts, do not count.
        let oneShot = oneShotPending
        oneShotPending = false
        send(loc)
        // iOS also hands over the current fix right after monitoring starts; that is not a move. It does count when
        // iOS launched the app for this very event (`launchedForLocation`).
        let settled = Reporter.shared.launchedForLocation || begunAt.map { Date().timeIntervalSince($0) > 10 } ?? true
        let freshSignificantChange = !oneShot && !updating && settled && loc.timestamp.timeIntervalSinceNow > -120
        if freshSignificantChange || loc.speed >= LocationTuning.movingSpeed {
            enterMoving(from: loc)
        } else if updating {
            track(loc)
        } else {
            armRestFence(at: loc)
        }
    }

    /// Sent unless the thinning holds it back while updating; a held fix goes out when updates stop.
    private func send(_ loc: CLLocation) {
        if updating && !LocationTuning.passesThinning(loc, after: lastSent) {
            thinned = loc
        } else {
            report(loc)
        }
    }

    /// Moves the idle clock when a fix is far enough from the last one that counted.
    private func track(_ loc: CLLocation) {
        if anchor == nil || loc.distance(from: anchor!) >= LocationTuning.idleRadius {
            anchor = loc
            armIdle()
        }
    }

    private func report(_ loc: CLLocation) {
        ReportMemory.set(string: "yes", for: "location.firstFix")
        lastSent = loc
        thinned = nil
        // The place name goes with the fix (a later frame for the same fix would be dropped as a duplicate): the
        // cached name within 100 m is instant, a new place waits for the geocoder, at most `geocodeTimeout`.
        let hold = BackgroundHold.begin("jarvis.location.place")
        Task { @MainActor in
            let place = await self.place(for: loc)
            Reporter.shared.emit([.location(lat: loc.coordinate.latitude, lng: loc.coordinate.longitude,
                                            accuracyM: loc.horizontalAccuracy, at: loc.timestamp, place: place,
                                            speedMps: loc.speed >= 0 ? loc.speed : nil)])
            hold.end()
        }
    }

    // MARK: Adaptive updates

    private var authorizedToTrack: Bool {
        manager.authorizationStatus == .authorizedAlways || manager.authorizationStatus == .authorizedWhenInUse
    }

    /// He is moving: updates run at the fine accuracy until he stops.
    private func enterMoving(from loc: CLLocation? = nil, activity now: Activity? = nil) {
        guard authorizedToTrack else { return }
        moving = true
        activity = now ?? activity
        anchor = loc ?? anchor
        rest?.cancel()
        rest = nil
        startUpdates()
    }

    /// Starts standard updates if they are not running, applies the accuracy for the state, and (re)arms the
    /// idle timeout.
    private func startUpdates() {
        guard authorizedToTrack else { return }
        removeRestFence()
        manager.desiredAccuracy = moving ? LocationTuning.movingAccuracy : LocationTuning.watchAccuracy
        manager.distanceFilter = moving ? LocationTuning.movingDistanceFilter : LocationTuning.watchDistanceFilter
        manager.activityType = LocationTuning.activityType(for: activity)
        armIdle()
        guard !updating else { return }
        updating = true
        // A pause would end tracking without a word; the idle timeout and live motion decide when to stop.
        manager.pausesLocationUpdatesAutomatically = false
        // Background updates only with Always: with "while using" iOS would add its blue bar for them.
        manager.allowsBackgroundLocationUpdates = manager.authorizationStatus == .authorizedAlways
        manager.startUpdatingLocation()
        log.info("updates started, moving \(self.moving)")
        // No CLBackgroundActivitySession: it puts the location badge in the Dynamic Island for the whole trip. Always plus
        // allowsBackgroundLocationUpdates keeps the app running through it without one.
        watchMotion()
    }

    /// Back to visits, significant changes and the rest geofence only. The newest fix the thinning dropped goes
    /// out first, so the place he stopped at is not lost.
    private func exitUpdates() {
        idle?.cancel()
        idle = nil
        rest?.cancel()
        rest = nil
        anchor = nil
        guard updating else { return }
        updating = false
        moving = false
        activity = nil
        manager.stopUpdatingLocation()
        log.info("updates stopped")
        manager.allowsBackgroundLocationUpdates = false
        motionManager.stopActivityUpdates()
        if let t = thinned, let last = lastSent, t.distance(from: last) >= LocationTuning.minSeparation { report(t) }
        thinned = nil
        if let at = lastFix ?? lastSent { armRestFence(at: at) }
    }

    private func armIdle() {
        idle?.cancel()
        idle = Task { [weak self] in
            try? await Task.sleep(for: .seconds(LocationTuning.idleTimeout))
            guard !Task.isCancelled else { return }
            self?.exitUpdates()
        }
    }

    /// While updates run, live motion (only now, never otherwise) ends them sooner than the idle timeout, and tunes
    /// the activity type. Needs Motion access that the motion switch already asked for; it is not asked here.
    private func watchMotion() {
        guard CMMotionActivityManager.isActivityAvailable(), CMMotionActivityManager.authorizationStatus() == .authorized else { return }
        motionManager.startActivityUpdates(to: .main) { [weak self] a in
            guard let a else { return }
            let sample = MotionCollector.sample(a)
            Task { @MainActor in self?.live(sample) }
        }
    }

    private func live(_ s: MotionSample) {
        guard let now = MotionCollector.current([s]) else { return }
        if now == .stationary {
            guard rest == nil else { return }
            rest = Task { [weak self] in
                try? await Task.sleep(for: .seconds(LocationTuning.stationaryGrace))
                guard !Task.isCancelled else { return }
                self?.exitUpdates()
            }
        } else {
            rest?.cancel()
            rest = nil
            enterMoving(activity: now)
        }
    }

    // MARK: Rest geofence

    /// A circle where he is resting. Leaving it wakes the app (even a terminated one) within a minute or two, which
    /// is what catches the first stretch of a trip before significant-change monitoring would. Needs Always and
    /// precise location.
    private func armRestFence(at loc: CLLocation) {
        guard manager.authorizationStatus == .authorizedAlways, manager.accuracyAuthorization == .fullAccuracy,
              CLLocationManager.isMonitoringAvailable(for: CLCircularRegion.self),
              manager.maximumRegionMonitoringDistance > 0 else { return }
        removeRestFence()
        let radius = min(LocationTuning.restRadius, manager.maximumRegionMonitoringDistance)
        let region = CLCircularRegion(center: loc.coordinate, radius: radius, identifier: Self.fenceID)
        region.notifyOnEntry = false
        region.notifyOnExit = true
        manager.startMonitoring(for: region)
        log.info("rest fence armed")
    }

    private func removeRestFence() {
        for r in manager.monitoredRegions where r.identifier == Self.fenceID { manager.stopMonitoring(for: r) }
    }

    // MARK: Place name

    /// A short place text for a visit, or nil. One reverse geocode per new place, spaced and time-limited; the
    /// coordinate goes to Apple's geocoder for it. Without a name the visit has been sent as it is.
    private func place(for loc: CLLocation) async -> String? {
        if let g = geocoded, g.loc.distance(from: loc) < 100 { return g.place }
        if let at = geocodedAt, Date().timeIntervalSince(at) < LocationTuning.geocodeSpacing { return nil }
        geocodedAt = Date()
        let geocoder = CLGeocoder()
        let marks: [CLPlacemark]? = await withTaskGroup(of: [CLPlacemark]?.self) { group in
            group.addTask { try? await geocoder.reverseGeocodeLocation(loc) }
            group.addTask {
                try? await Task.sleep(for: .seconds(LocationTuning.geocodeTimeout))
                geocoder.cancelGeocode()
                return nil
            }
            let first = await group.next() ?? nil
            group.cancelAll()
            return first
        }
        let place = Self.describe(marks?.first)
        geocoded = (loc, place)
        return place
    }

    private static func describe(_ mark: CLPlacemark?) -> String? {
        guard let mark else { return nil }
        let parts = [mark.areasOfInterest?.first ?? mark.name, mark.subLocality ?? mark.locality].compactMap { $0 }
        var seen: [String] = []
        for p in parts where !seen.contains(p) { seen.append(p) }
        return seen.isEmpty ? nil : seen.joined(separator: ", ")
    }
}
