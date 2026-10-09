// jarvis-where: one CoreLocation fix (and a short place label) written as one JSON line to argv[1].
// Must run as the LSUIElement .app bundle (see Info.plist): a bare binary gets no location prompt.
import CoreLocation
import Foundation

let outPath = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "/dev/stdout"
func emit(_ object: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: object),
          let text = String(data: data, encoding: .utf8) else { return }
    try? (text + "\n").write(toFile: outPath, atomically: true, encoding: .utf8)
}

// "Ring Rd, Saanich": street (or the place's name), then the neighbourhood or city.
func label(_ p: CLPlacemark) -> String? {
    let first = p.thoroughfare ?? p.name
    let second = p.subLocality ?? p.locality
    let parts = [first, second].compactMap { $0 }.filter { !$0.isEmpty }
    return parts.isEmpty ? nil : parts.joined(separator: ", ")
}

final class Probe: NSObject, CLLocationManagerDelegate {
    let manager = CLLocationManager()
    override init() {
        super.init()
        manager.delegate = self
        manager.desiredAccuracy = kCLLocationAccuracyHundredMeters
    }
    func locationManagerDidChangeAuthorization(_ m: CLLocationManager) {
        switch m.authorizationStatus {
        case .notDetermined: m.requestWhenInUseAuthorization()
        case .denied, .restricted: emit(["error": "denied"]); exit(2)
        default: m.requestLocation()
        }
    }
    func locationManager(_ m: CLLocationManager, didUpdateLocations locs: [CLLocation]) {
        guard let l = locs.last else { return }
        var out: [String: Any] = [
            "lat": (l.coordinate.latitude * 1000).rounded() / 1000,
            "lng": (l.coordinate.longitude * 1000).rounded() / 1000,
            "accuracy_m": l.horizontalAccuracy.rounded(),
            "age_s": max(0, -l.timestamp.timeIntervalSinceNow).rounded(),
        ]
        // A failed or slow geocode still returns the fix, just without "place".
        DispatchQueue.main.asyncAfter(deadline: .now() + 4) { emit(out); exit(0) }
        CLGeocoder().reverseGeocodeLocation(l) { marks, _ in
            if let name = marks?.first.flatMap(label) { out["place"] = name }
            emit(out); exit(0)
        }
    }
    func locationManager(_ m: CLLocationManager, didFailWithError e: Error) {
        emit(["error": e.localizedDescription]); exit(1)
    }
}
let probe = Probe()
DispatchQueue.main.asyncAfter(deadline: .now() + 10) { emit(["error": "timeout"]); exit(3) }
RunLoop.main.run()
