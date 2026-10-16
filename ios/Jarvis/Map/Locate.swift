import CoreLocation
import MapKit
import UIKit

// The brain asks where he is right now: `{"type":"locate","request_id"}` on /phone/ws, answered on the same socket with `located` (a fresh fix, and a small picture of the map around him
// when the app is on screen) or `located` with an error. Only a permission he already gave is used: nothing here asks for one.

/// One position, as the system reported it.
struct LocateFix: Equatable {
    let lat: Double, lng: Double, accuracyM: Double
    let at: Date
}

enum LocateResult: Equatable {
    case fix(LocateFix)
    case denied                 // no permission (or none asked yet: a request from the brain never prompts), or location services are off for the app
    case unavailable            // no fix came in time, or the system could not give one
}

/// Where a fresh fix comes from; the tests' stand-in is a fake.
@MainActor
protocol LocationSource {
    func fix(wait: Double) async -> LocateResult
}

/// One fix from the system, once: `requestLocation`, with no permission prompt. Made per request, so a late answer cannot land on the next one.
@MainActor
final class OneShotLocation: NSObject, CLLocationManagerDelegate, LocationSource {
    private let manager = CLLocationManager()
    private var pending: CheckedContinuation<LocateResult, Never>?

    func fix(wait: Double) async -> LocateResult {
        guard CLLocationManager.locationServicesEnabled() else { return .unavailable }
        switch manager.authorizationStatus {
        case .authorizedAlways, .authorizedWhenInUse: break
        default: return .denied
        }
        manager.delegate = self
        manager.desiredAccuracy = kCLLocationAccuracyNearestTenMeters
        return await withCheckedContinuation { c in
            pending = c
            manager.requestLocation()
            Task { try? await Task.sleep(nanoseconds: UInt64(wait * 1e9)); finish(.unavailable) }
        }
    }

    private func finish(_ r: LocateResult) { pending?.resume(returning: r); pending = nil }

    nonisolated func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        let best = locations.last { $0.horizontalAccuracy >= 0 }
        Task { @MainActor in
            guard let l = best else { return finish(.unavailable) }
            finish(.fix(LocateFix(lat: l.coordinate.latitude, lng: l.coordinate.longitude, accuracyM: l.horizontalAccuracy, at: l.timestamp)))
        }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        let denied = (error as? CLError)?.code == .denied
        Task { @MainActor in finish(denied ? .denied : .unavailable) }
    }
}

/// `work`'s answer, or nil once `seconds` have gone: the work is let go (not waited for) so the caller is never held longer than that.
func within<T>(_ seconds: Double, _ work: @escaping () async -> T?) async -> T? {
    await withCheckedContinuation { (c: CheckedContinuation<T?, Never>) in
        let gate = OnceGate(c)
        Task { gate.finish(await work()) }
        Task { try? await Task.sleep(nanoseconds: UInt64(seconds * 1e9)); gate.finish(nil) }
    }
}

private final class OnceGate<T>: @unchecked Sendable {
    private let lock = NSLock()
    private var c: CheckedContinuation<T?, Never>?
    init(_ c: CheckedContinuation<T?, Never>) { self.c = c }
    func finish(_ v: T?) {
        lock.lock(); let c = self.c; self.c = nil; lock.unlock()
        c?.resume(returning: v)
    }
}

/// The picture that goes with a fix: a standard map about 300 m across with him in the middle, 600 x 600, uploaded like any attachment.
enum LocateSnapshot {
    static let side = 600.0, spanM = 300.0
    /// the picture is skipped when the map takes longer than this to draw, and the whole step (drawing and upload) is cut off at `totalBudget`, so the answer is never held for it
    static let renderBudget = 1.5, totalBudget = 4.0

    @MainActor static func upload(at c: CLLocationCoordinate2D) async -> String? {
        guard let image = await within(renderBudget, { await render(at: c) }), let jpeg = AttachUpload.jpeg(image) else { return nil }
        let part = AttachUpload.Part(name: "here.jpg", mime: "image/jpeg", data: jpeg, isImage: true)
        guard let r = try? await BrainLink.shared.call({ try await BrainClient.upload(part, bases: $0, token: $1) }), r.status == 200 else { return nil }
        return r.id
    }

    @MainActor static func render(at c: CLLocationCoordinate2D) async -> UIImage? {
        let o = MKMapSnapshotter.Options()
        o.region = MKCoordinateRegion(center: c, latitudinalMeters: spanM, longitudinalMeters: spanM)
        o.size = CGSize(width: side, height: side); o.scale = 1
        o.mapType = .standard
        guard let snap = try? await MKMapSnapshotter(options: o).start() else { return nil }
        let f = UIGraphicsImageRendererFormat(); f.scale = 1
        return UIGraphicsImageRenderer(size: o.size, format: f).image { ctx in
            snap.image.draw(at: .zero)
            let p = snap.point(for: c), g = ctx.cgContext
            g.setFillColor(UIColor.white.cgColor); g.fillEllipse(in: CGRect(x: p.x - 13, y: p.y - 13, width: 26, height: 26))
            g.setFillColor(UIColor.systemBlue.cgColor); g.fillEllipse(in: CGRect(x: p.x - 10, y: p.y - 10, width: 20, height: 20))
        }
    }
}

/// What the phone answers a `locate` with.
@MainActor
struct LocateService {
    var source: () -> LocationSource = { OneShotLocation() }
    var foreground: () -> Bool = { UIApplication.shared.applicationState == .active }
    var snapshot: (CLLocationCoordinate2D) async -> String? = { await LocateSnapshot.upload(at: $0) }
    var fixWait = 8.0
    var snapshotBudget = LocateSnapshot.totalBudget

    func answer(_ requestId: String) async -> PhoneFrame {
        switch await source().fix(wait: fixWait) {
        case .denied: return .located(requestId: requestId, outcome: .error("denied"))
        case .unavailable: return .located(requestId: requestId, outcome: .error("unavailable"))
        case .fix(let f):
            // a picture only when he is looking at the app, and only if it is quick
            var id: String?
            if foreground() {
                let c = CLLocationCoordinate2D(latitude: f.lat, longitude: f.lng), snap = snapshot
                id = await within(snapshotBudget) { await snap(c) }
            }
            return .located(requestId: requestId, outcome: .fix(lat: f.lat, lng: f.lng, accuracyM: f.accuracyM, fixTsEpochMs: Int64((f.at.timeIntervalSince1970 * 1000).rounded()), snapshot: id))
        }
    }
}
