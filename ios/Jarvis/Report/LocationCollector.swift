import AVFoundation
import CoreLocation
import CoreMotion
import os

/// The knobs of location, all here. Visits, significant changes and one rest geofence are always armed and cost next to nothing;
/// standard updates run while he moves and sleep in two steps when he stops (ADR 0312, `LocationModes`). Opening the app starts
/// nothing: he does not want the app on screen to be locating him.
enum LocationTuning {
    /// While moving: GPS at its second-best accuracy, every fix delivered. Each fix goes into the track; the thinning below decides
    /// which also go out as sampled fixes, and does not make the GPS cheaper. The battery cost of a trip is this.
    static let movingAccuracy: CLLocationAccuracy = kCLLocationAccuracyNearestTenMeters
    /// In a vehicle: GPS at its best.
    static let vehicleAccuracy: CLLocationAccuracy = kCLLocationAccuracyBest
    /// Light sleep: cell and Wi-Fi positioning only, which keeps the app running (and live motion coming) without GPS.
    static let sleepAccuracy: CLLocationAccuracy = kCLLocationAccuracyThreeKilometers
    /// Moving, outside a vehicle: updates sleep after this long without a fix `idleRadius` away from the last one that counted. The
    /// radius is above the jitter of a 100 m fix, so a phone lying still does not keep GPS on.
    static let idleTimeout: TimeInterval = 5 * 60
    static let idleRadius: CLLocationDistance = 150
    /// Moving, outside a vehicle: live motion says stationary for this long, updates sleep.
    static let stationaryGrace: TimeInterval = 90
    /// In a vehicle: GPS stays on through a stop this long (a red light, a pull-over, a queue); the fixes say when the car moves.
    static let vehicleStop: TimeInterval = 10 * 60
    /// Fixes at this speed (25 km/h) for this long, and motion not saying cycling, mean a vehicle.
    static let vehicleSpeed: CLLocationSpeed = 25 / 3.6
    static let vehicleSpeedFor: TimeInterval = 10
    /// How long the light sleep lasts before the deep one.
    static let lightSleep: TimeInterval = 30 * 60
    /// While updating: at most one sampled fix per `minInterval`, at least `minSeparation` from the last one sent.
    static let minInterval: TimeInterval = 30
    static let minSeparation: CLLocationDistance = 50
    /// One fix this fast (m/s, walking pace) says he is moving even when no motion history is read.
    static let movingSpeed: CLLocationSpeed = 1.0
    /// The geofence armed where the deep sleep begins; leaving it restarts tracking (and relaunches a terminated app).
    static let restRadius: CLLocationDistance = 100
    /// Reverse geocoding: once per new place, never more often than this, never longer than `geocodeTimeout`.
    static let geocodeSpacing: TimeInterval = 60
    static let geocodeTimeout: TimeInterval = 6

    /// Whether a fix taken while updating is sent as a sampled fix: far enough and late enough after the last one sent.
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

/// Where he goes. Always armed: visits, significant changes (iOS relaunches the app for both) and a rest geofence where the deep sleep
/// began. While updates run: every fix into the track (`phone.track_observed`), a sample of them as `phone.location_observed`, and in a
/// vehicle the device motion (`DriveMotion`). The coordinates leave this object only as frames.
@MainActor
final class LocationCollector: NSObject, Collector, CLLocationManagerDelegate {
    let name = "location"

    private nonisolated static let fenceID = "com.allen.jarvis.rest"

    private let manager = CLLocationManager()
    private let motionManager = CMMotionActivityManager()
    /// States only, never a coordinate.
    private let log = Logger(subsystem: "com.allen.jarvis", category: "location")
    private var modes = LocationModes()
    /// What `apply` last set up.
    private var applied: (mode: LocationModes.Mode, plan: LocationModes.Plan)?
    /// Standard updates are running.
    private(set) var updating = false
    private var watchingMotion = false
    private var chunker = TrackChunker()
    private var oneShotPending = false
    private var begunAt: Date?
    private var lastFix: CLLocation?
    /// The last fix taken while moving: where the rest fence goes, never a coarse fix of the light sleep.
    private var lastGood: CLLocation?
    private var lastSent: CLLocation?
    private var thinned: CLLocation?  // newest fix dropped by the thinning, sent when updates sleep
    private var clock: Task<Void, Never>?
    private var clockFor: Date?
    private var route: NSObjectProtocol?
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
        DriveMotion.shared.recover()
        // CarPlay is a car: the route change wakes nothing by itself, but while the app runs it is the earliest sign.
        route = NotificationCenter.default.addObserver(forName: AVAudioSession.routeChangeNotification, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in self?.routeChanged() }
        }
        authorizationChanged()
    }

    func stop() {
        route.map(NotificationCenter.default.removeObserver)
        route = nil
        modes.deepSleep()
        apply()
        removeRestFence()
        manager.stopMonitoringVisits()
        manager.stopMonitoringSignificantLocationChanges()
    }

    func wake() async {}

    /// Motion history says what he is doing now (`Reporter.movement`).
    func motion(_ sample: MotionSample?) {
        note("motion says \(String(describing: sample?.activity)), still \(sample?.still ?? false), mode \(modes.mode)")
        guard let sample, authorizedToTrack else { return }
        modes.motion(sample, now: Date())
        apply()
    }

    // MARK: Authorization

    /// States only, never a coordinate: to the unified log and to Library/Caches/location.log, so what kept location on can be read from the phone.
    private func note(_ s: String) {
        log.info("\(s, privacy: .public)")
        StallLog.append(s, to: "location.log")
    }

    private func authorizationChanged() {
        note("authorization \(manager.authorizationStatus.rawValue), full accuracy \(manager.accuracyAuthorization == .fullAccuracy)")
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
            modes.deepSleep()
            apply()
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
        if departed != nil { moved() } else if modes.mode == .deepSleep { armRestFence(at: loc) }
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
            self.note("left the rest fence")
            self.moved()
            hold.end()
        }
    }

    private func fix(_ loc: CLLocation) {
        lastFix = loc
        switch modes.mode {
        case .moving:
            lastGood = loc
            keep(loc)
            modes.fix(loc, now: Date())
            send(loc)
            apply()
        case .lightSleep:
            // A coarse fix: into the track as it is, never a sampled fix (a stay is read from the gap between sampled fixes).
            keep(loc)
            modes.fix(loc, now: Date())
            apply()
        case .deepSleep:
            // A significant-change fix, the one-shot, or the current fix iOS hands over when monitoring starts. A fresh significant
            // change means he went about 500 m, so tracking starts; so does a fix at walking pace. The one-shot, and an old fix iOS
            // hands over when monitoring starts, do not count. It does count when iOS launched the app for this very event.
            let oneShot = oneShotPending
            oneShotPending = false
            report(loc)
            let settled = Reporter.shared.launchedForLocation || begunAt.map { Date().timeIntervalSince($0) > 10 } ?? true
            let freshSignificantChange = !oneShot && settled && loc.timestamp.timeIntervalSinceNow > -120
            if freshSignificantChange || loc.speed >= LocationTuning.movingSpeed {
                lastGood = loc
                moved(at: loc)
            } else {
                armRestFence(at: loc)
            }
        }
    }

    /// Into the track; a frame that closed goes out.
    private func keep(_ loc: CLLocation) {
        if let frame = chunker.add(TrackFix(loc)) { Reporter.shared.emit([.track(frame)]) }
    }

    /// Sent unless the thinning holds it back; a held fix goes out when updates sleep.
    private func send(_ loc: CLLocation) {
        if LocationTuning.passesThinning(loc, after: lastSent) {
            report(loc)
        } else {
            thinned = loc
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

    // MARK: Modes

    private var authorizedToTrack: Bool {
        manager.authorizationStatus == .authorizedAlways || manager.authorizationStatus == .authorizedWhenInUse
    }

    /// A sign he is on the move. Waking from the deep sleep also reads the motion history now, so the brain hears when the move began
    /// even when GPS came late.
    private func moved(activity: Activity? = nil, at loc: CLLocation? = nil) {
        guard authorizedToTrack else { return }
        if modes.moved(Date(), activity: activity, at: loc) { Reporter.shared.readMotionNow() }
        apply()
    }

    /// Sets the manager, live motion and device motion to what the modes say, and does what a change of mode does: a sleep sends the
    /// held fix and the open track frame, the deep sleep arms the rest fence.
    private func apply() {
        let plan = modes.plan, mode = modes.mode
        let was = applied
        applied = (mode, plan)
        if was?.mode != mode {
            note("mode \(mode), vehicle \(modes.vehicle), activity \(String(describing: modes.activity))")
            if mode != .moving {
                if let t = thinned, lastSent.map({ t.distance(from: $0) >= LocationTuning.minSeparation }) ?? true { report(t) }
                thinned = nil
                if let frame = chunker.close() { Reporter.shared.emit([.track(frame)]) }
            }
        } else if was?.plan != plan {
            note("vehicle \(modes.vehicle), activity \(String(describing: modes.activity))")
        }
        if plan.updates {
            if was?.plan != plan || !updating {
                manager.desiredAccuracy = plan.accuracy
                manager.distanceFilter = kCLDistanceFilterNone
                manager.activityType = plan.activityType
            }
            if !updating {
                updating = true
                removeRestFence()
                // A pause would end tracking without a word; the modes decide when to sleep.
                manager.pausesLocationUpdatesAutomatically = false
                // Background updates only with Always: with "while using" iOS would add its blue bar for them.
                manager.allowsBackgroundLocationUpdates = manager.authorizationStatus == .authorizedAlways
                manager.startUpdatingLocation()
                note("updates started, background \(manager.allowsBackgroundLocationUpdates)")
                // No CLBackgroundActivitySession: it puts the location badge in the Dynamic Island for the whole trip. Always plus
                // allowsBackgroundLocationUpdates keeps the app running through it without one.
            }
        } else if updating {
            updating = false
            manager.stopUpdatingLocation()
            manager.allowsBackgroundLocationUpdates = false
            note("updates stopped")
            if let at = lastGood ?? lastFix { armRestFence(at: at) }
        }
        if plan.liveMotion { watchMotion() } else { stopWatchingMotion() }
        if plan.recordMotion { DriveMotion.shared.start() } else { DriveMotion.shared.stop() }
        armClock()
    }

    /// One task that wakes the modes at their deadline. A later deadline needs no new task: the one armed finds nothing due, and
    /// re-arms for the current one.
    private func armClock() {
        guard let due = modes.deadline else {
            clock?.cancel(); clock = nil; clockFor = nil
            return
        }
        if let armed = clockFor, armed <= due { return }
        clock?.cancel()
        clockFor = due
        clock = Task { [weak self] in
            try? await Task.sleep(for: .seconds(max(0, due.timeIntervalSinceNow)))
            guard !Task.isCancelled, let self else { return }
            self.clockFor = nil
            self.modes.tick(Date())
            self.apply()
        }
    }

    /// While updates run, live motion (only then, never otherwise) says when he stops, gets out of a car or sets off. Needs Motion
    /// access that the motion switch already asked for; it is not asked here.
    private func watchMotion() {
        guard !watchingMotion, CMMotionActivityManager.isActivityAvailable(), CMMotionActivityManager.authorizationStatus() == .authorized else { return }
        watchingMotion = true
        motionManager.startActivityUpdates(to: .main) { [weak self] a in
            guard let a else { return }
            let sample = MotionCollector.sample(a)
            Task { @MainActor in self?.live(sample) }
        }
    }

    private func stopWatchingMotion() {
        guard watchingMotion else { return }
        watchingMotion = false
        motionManager.stopActivityUpdates()
    }

    private func live(_ s: MotionSample) {
        guard authorizedToTrack else { return }
        modes.motion(s, now: Date())
        apply()
    }

    /// CarPlay is a car.
    private func routeChanged() {
        guard AVAudioSession.sharedInstance().currentRoute.outputs.contains(where: { $0.portType == .carAudio }) else { return }
        note("CarPlay")
        moved(activity: .automotive)
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
        note("rest fence armed")
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
