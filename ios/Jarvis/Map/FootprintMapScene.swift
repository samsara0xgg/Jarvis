#if DEBUG
import SwiftUI
import MapKit

/// `-scene footprintMap`: the footprint map full screen on a made-up outing on the Canterbury plains (New Zealand), with a gap, two stops, two places, now and the scrub dot held at 80% of the way.
enum FootprintMapScene {
    @MainActor static func present() {
        guard var top = UIApplication.shared.connectedScenes.compactMap({ ($0 as? UIWindowScene)?.keyWindow }).first?.rootViewController else { return }
        while let next = top.presentedViewController { top = next }
        let host = UIHostingController(rootView: demo.ignoresSafeArea())
        host.modalPresentationStyle = .fullScreen
        host.overrideUserInterfaceStyle = .dark
        top.present(host, animated: false)
    }

    private static let waypoints = [(-43.610, 172.100), (-43.585, 172.170), (-43.550, 172.215), (-43.520, 172.290), (-43.505, 172.380)]

    private static func at(_ t: Double) -> CLLocationCoordinate2D {
        let f = t * Double(waypoints.count - 1), i = min(Int(f), waypoints.count - 2), k = f - Double(i)
        let a = waypoints[i], b = waypoints[i + 1]
        return CLLocationCoordinate2D(latitude: a.0 + (b.0 - a.0) * k + 0.004 * sin(t * 20), longitude: a.1 + (b.1 - a.1) * k)
    }

    static var demo: FootprintMapView {
        let n = 120, t0: Int64 = 1_760_000_000_000
        var pts: [FootprintMapPoint] = []
        for i in 0...n where !(51...55).contains(i) {
            let t = Double(i) / Double(n)
            pts.append(FootprintMapPoint(coord: at(t), ms: t0 + Int64(i) * 30_000, speedMps: 25 * abs(sin(3 * .pi * t))))
        }
        let gap = FootprintMapGap(from: at(50.0 / Double(n)), to: at(56.0 / Double(n)), startMs: t0 + 50 * 30_000, endMs: t0 + 56 * 30_000)
        let route = FootprintMapRoute(id: "demo", points: pts, gaps: [gap])
        let dotAt = 96
        return FootprintMapView(
            routes: [route],
            places: [
                FootprintMapPlace(id: "home", coord: at(0), discRadius: 29, tint: FootprintPaint.home, pin: .home, label: "Home 13 h 7 min"),
                FootprintMapPlace(id: "shop", coord: at(0.5), discRadius: 14, tint: RGB(100, 200, 255), pin: .place, label: "Farm shop 40 min"),
            ],
            tokens: [FootprintMapToken(id: "s1", coord: at(1.0 / 3), number: 1, ms: t0 + 40 * 30_000), FootprintMapToken(id: "s2", coord: at(2.0 / 3), number: 2, ms: t0 + 80 * 30_000)],
            now: FootprintMapNow(coord: at(1), label: "Now 15 min"),
            dot: FootprintMapDot(coord: at(Double(dotAt) / Double(n)), ms: t0 + Int64(dotAt) * 30_000, rgb: FootprintPaint.speed(kmh: 76), label: "76 km/h"),
            fitPadding: UIEdgeInsets(top: 120, left: 20, bottom: 120, right: 20), fitKey: "demo")
    }
}
#endif
