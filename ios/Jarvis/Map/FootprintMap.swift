import SwiftUI
import MapKit
import CoreLocation

// The 我的足迹 page's map (ADR 0323): a live MKMapView in the map card's look with the day's routes drawn by hand (colour by speed), and flat marks that keep their size in points
// as the map zooms. It takes plain values, so it knows nothing of the brain's reply. The look's colours are `FootprintPaint` and `TLPaint`; none is written here.

// MARK: values

struct FootprintMapPoint: Equatable {
    var coord: CLLocationCoordinate2D, ms: Int64, speedMps: Double?
    static func == (a: Self, b: Self) -> Bool { a.coord.same(b.coord) && a.ms == b.ms && a.speedMps == b.speedMps }
}

/// A stretch with no fixes: dotted, straight.
struct FootprintMapGap: Equatable {
    var from: CLLocationCoordinate2D, to: CLLocationCoordinate2D, startMs: Int64, endMs: Int64
    static func == (a: Self, b: Self) -> Bool { a.from.same(b.from) && a.to.same(b.to) && a.startMs == b.startMs && a.endMs == b.endMs }
}

struct FootprintMapRoute: Equatable {
    var id: String, points: [FootprintMapPoint], gaps: [FootprintMapGap]
}

/// A long stay: a flat disc (`discRadius` in points, 0 for none), a pin and a capsule word by it.
struct FootprintMapPlace: Equatable {
    enum Pin: Equatable { case home, place, end }
    var id: String, coord: CLLocationCoordinate2D, discRadius: CGFloat, tint: RGB, pin: Pin, label: String?
    static func == (a: Self, b: Self) -> Bool {
        a.id == b.id && a.coord.same(b.coord) && a.discRadius == b.discRadius && a.tint == b.tint && a.pin == b.pin && a.label == b.label
    }
}

/// A numbered stop.
struct FootprintMapToken: Equatable {
    var id: String, coord: CLLocationCoordinate2D, number: Int, ms: Int64
    static func == (a: Self, b: Self) -> Bool { a.id == b.id && a.coord.same(b.coord) && a.number == b.number && a.ms == b.ms }
}

struct FootprintMapNow: Equatable {
    var coord: CLLocationCoordinate2D, label: String
    static func == (a: Self, b: Self) -> Bool { a.coord.same(b.coord) && a.label == b.label }
}

/// The scrub dot: where he was at the held minute. One that is not held (the outing page's, at its fastest fix, while no finger is on a curve) only marks the place: the route stays lit, and the stops.
struct FootprintMapDot: Equatable {
    var coord: CLLocationCoordinate2D, ms: Int64, rgb: RGB, label: String, held = true
    static func == (a: Self, b: Self) -> Bool { a.coord.same(b.coord) && a.ms == b.ms && a.rgb == b.rgb && a.label == b.label && a.held == b.held }
}

/// The first-person camera while a finger holds a time: where he was (the scrub dot, kept at the map's centre), the way he was going in degrees clockwise from north (nil while still: the
/// map keeps the heading it has) and how fast, which sets how far out the camera stands.
struct FootprintMapFollow: Equatable {
    var coord: CLLocationCoordinate2D, heading: Double?, speedMps: Double
    static func == (a: Self, b: Self) -> Bool { a.coord.same(b.coord) && a.heading == b.heading && a.speedMps == b.speedMps }

    /// tilted forward as turn-by-turn navigation is
    static let pitch = 60.0
    /// How far the camera stands from him: 600 m while he stands or walks, out to 2 km from 80 km/h, so that the road ahead can be read at any pace.
    static func distance(speedMps v: Double) -> Double { 600 + 1400 * min(1, max(0, (v - 2) / 20)) }
}

extension TLMotion {
    /// following him: the way he goes, the camera's distance and its tilt are taken up with this time constant in seconds (about the follow spring's response)
    static let mapTurn = 0.2
    /// letting go: the camera flies back to the day's whole in this many seconds, easing at both ends; under Reduce Motion it is there at once
    static let mapReturn = 0.5
}

private extension CLLocationCoordinate2D {
    func same(_ o: CLLocationCoordinate2D) -> Bool { latitude == o.latitude && longitude == o.longitude }
}

// MARK: pure pieces

enum FootprintMapMath {
    /// The ramp colour at each point; a point with no speed takes the one before it (the first ones take the first known), none at all reads as standing still.
    static func pointColors(_ pts: [FootprintMapPoint]) -> [RGB] {
        var last = pts.compactMap(\.speedMps).first ?? 0
        return pts.map { p in last = p.speedMps ?? last; return FootprintPaint.speed(mps: last) }
    }

    /// How many points are lit at a time: those at or before it (points are by time).
    static func litCount(_ pts: [FootprintMapPoint], ms: Int64) -> Int { pts.prefix { $0.ms <= ms }.count }
}

// MARK: the map

struct FootprintMapView: UIViewRepresentable {
    var routes: [FootprintMapRoute] = []
    var places: [FootprintMapPlace] = []
    var tokens: [FootprintMapToken] = []
    var now: FootprintMapNow?
    var dot: FootprintMapDot?
    /// while set, the camera is his: centred on him, turned to his way, tilted and close, and when it goes the map eases back to the fit
    var follow: FootprintMapFollow?
    var lineWidth: CGFloat = 4.2
    /// room kept free at the edges when the map frames its content (the fade and the controls over it)
    var fitPadding: UIEdgeInsets = .zero
    /// the map frames its content when this changes, not on every update, so his pan and pinch survive a scrub
    var fitKey = ""
    /// room kept under the map's own marks (Apple's logo and legal label) so that a fade over its foot does not hide them
    var legalInset: CGFloat = 0

    /// the minute a finger holds, which lights the route before it
    var heldMs: Int64? { dot.flatMap { $0.held ? $0.ms : nil } }

    /// everything the frame must cover
    var fitCoordinates: [CLLocationCoordinate2D] {
        routes.flatMap { $0.points.map(\.coord) } + places.map(\.coord) + (now.map { [$0.coord] } ?? [])
    }

    func makeCoordinator() -> FootprintMapCoordinator { FootprintMapCoordinator() }

    func makeUIView(context: Context) -> FootprintMKMapView {
        let m = FootprintMKMapView()
        m.overrideUserInterfaceStyle = .dark
        m.pointOfInterestFilter = .excludingAll
        m.isRotateEnabled = false
        m.isPitchEnabled = false
        m.showsCompass = false
        m.delegate = context.coordinator
        return m
    }

    func updateUIView(_ m: FootprintMKMapView, context: Context) {
        m.layoutMargins.bottom = legalInset
        m.layoutMargins.top = follow == nil ? 0 : fitPadding.top          // the camera's centre is the middle of what the margins leave: he stands in the clear of the fade
        context.coordinator.apply(self, to: m)
    }

    static func dismantleUIView(_ m: FootprintMKMapView, coordinator: FootprintMapCoordinator) { coordinator.stop() }
}

final class FootprintMKMapView: MKMapView {
    /// the frame to take once the map has a size (the first update comes before layout)
    var pendingFit: (() -> Void)?
    override func layoutSubviews() {
        super.layoutSubviews()
        if !bounds.isEmpty, let fit = pendingFit { pendingFit = nil; fit() }
    }
}

final class FootprintRoute: NSObject, MKOverlay {
    let route: FootprintMapRoute
    let coordinate: CLLocationCoordinate2D
    let boundingMapRect: MKMapRect
    init(_ r: FootprintMapRoute) {
        route = r
        let pts = r.points.map(\.coord) + r.gaps.flatMap { [$0.from, $0.to] }
        boundingMapRect = pts.reduce(MKMapRect.null) { $0.union(MKMapRect(origin: MKMapPoint($1), size: MKMapSize(width: 0, height: 0))) }
        coordinate = pts.first ?? CLLocationCoordinate2D()
    }
}

final class FootprintMapCoordinator: NSObject, MKMapViewDelegate {
    private var view: FootprintMapView?
    private var fitKey: String?
    private var staticMarks: [FootprintMark.Note] = []
    private var nowNote: FootprintMark.Note?
    private var dotNote: FootprintMark.Note?
    private var renderers: [FootprintRouteRenderer] = []
    private weak var map: FootprintMKMapView?
    private var eye = Eye()

    func apply(_ v: FootprintMapView, to m: FootprintMKMapView) {
        let old = view
        view = v
        map = m
        if old?.routes != v.routes {
            m.removeOverlays(m.overlays)
            m.addOverlays(v.routes.map(FootprintRoute.init))
        } else if old?.lineWidth != v.lineWidth {
            renderers.forEach { $0.width = v.lineWidth; $0.setNeedsDisplay() }
        }
        if old?.places != v.places || old?.tokens != v.tokens {
            m.removeAnnotations(staticMarks)
            staticMarks = v.places.flatMap { p -> [FootprintMark.Note] in
                [p.discRadius > 0 ? FootprintMark.Note(p.coord, .disc(p.discRadius, p.tint), rank: 0) : nil,
                 FootprintMark.Note(p.coord, .pin(p.pin, p.tint), label: p.label, rank: 1)].compactMap { $0 }
            } + v.tokens.map { FootprintMark.Note($0.coord, .token($0.number), rank: 2, ms: $0.ms) }
            m.addAnnotations(staticMarks)
        }
        if old?.now != v.now {
            if let n = nowNote { m.removeAnnotation(n) }
            nowNote = v.now.map { FootprintMark.Note($0.coord, .now, label: $0.label, rank: 3) }
            if let n = nowNote { m.addAnnotation(n) }
        }
        if let d = v.dot {
            if let n = dotNote {
                n.coordinate = d.coord
                n.set(.dot(d.rgb), label: d.label)
                (m.view(for: n) as? FootprintMark)?.configure(n)
            } else {
                let n = FootprintMark.Note(d.coord, .dot(d.rgb), label: d.label, rank: 4)
                dotNote = n; m.addAnnotation(n)
            }
        } else if let n = dotNote { m.removeAnnotation(n); dotNote = nil }
        if old?.heldMs != v.heldMs {
            renderers.forEach { $0.litMs = v.heldMs; $0.setNeedsDisplay() }
            for n in staticMarks { (m.view(for: n) as? FootprintMark)?.alpha = alpha(n) }
        }
        if fitKey != v.fitKey {
            let first = fitKey == nil
            fitKey = v.fitKey
            m.pendingFit = { [weak m, weak self] in if let m { self?.fit(m, animated: !first) } }
            if !m.bounds.isEmpty { m.setNeedsLayout() }
        }
        if let f = v.follow { track(f, on: m) } else if eye.on { release(m) }
    }

    private func fit(_ m: MKMapView, animated: Bool) {
        guard let v = view, !v.fitCoordinates.isEmpty else { return }
        m.setVisibleMapRect(Self.rect(PlaceFit.region(v.fitCoordinates)), edgePadding: v.fitPadding, animated: animated)
    }

    // MARK: first person

    /// Where the camera is while it follows him: the centre is always his, at once; the heading, the tilt and the distance (its log, so that the way in from the whole day is even) go to
    /// their aims at `TLMotion.mapTurn`. A still him leaves the heading alone.
    private struct Eye {
        var on = false, heading = 0.0, pitch = 0.0, logDistance = 0.0
        var centre = CLLocationCoordinate2D()
    }
    private var aim = Eye()
    private var link: CADisplayLink?

    private func track(_ f: FootprintMapFollow, on m: FootprintMKMapView) {
        guard !m.bounds.isEmpty else {          // no size yet: the camera is taken once the map has one, and the first fit with it
            m.pendingFit = { [weak self, weak m] in
                guard let self, let m else { return }
                if let f = self.view?.follow { self.track(f, on: m) } else { self.fit(m, animated: false) }
            }
            return
        }
        if !eye.on {
            let c = m.camera
            eye = Eye(on: true, heading: c.heading, pitch: c.pitch, logDistance: log(c.centerCoordinateDistance))
            aim = eye
        }
        eye.centre = f.coord
        aim.heading = f.heading ?? aim.heading
        aim.pitch = FootprintMapFollow.pitch
        aim.logDistance = log(FootprintMapFollow.distance(speedMps: f.speedMps))
        if TLMotion.reduced { eye.heading = aim.heading; eye.pitch = aim.pitch; eye.logDistance = aim.logDistance }
        else if link == nil {
            link = CADisplayLink(target: self, selector: #selector(step))
            link?.add(to: .main, forMode: .common)
        }
        place(m)
    }

    @objc private func step(_ l: CADisplayLink) {
        guard let m = map else { return stop() }
        let k = 1 - exp(-(l.targetTimestamp - l.timestamp) / TLMotion.mapTurn)
        var turn = (aim.heading - eye.heading).truncatingRemainder(dividingBy: 360)          // the short way round
        if turn > 180 { turn -= 360 } else if turn < -180 { turn += 360 }
        eye.heading = (eye.heading + turn * k + 360).truncatingRemainder(dividingBy: 360)
        eye.pitch += (aim.pitch - eye.pitch) * k
        eye.logDistance += (aim.logDistance - eye.logDistance) * k
        if abs(turn) < 0.05, abs(aim.pitch - eye.pitch) < 0.05, abs(aim.logDistance - eye.logDistance) < 0.001 {
            eye.heading = aim.heading; eye.pitch = aim.pitch; eye.logDistance = aim.logDistance; link?.invalidate(); link = nil
        }
        place(m)
    }

    private func place(_ m: MKMapView) {
        m.camera = MKMapCamera(lookingAtCenter: eye.centre, fromDistance: exp(eye.logDistance), pitch: eye.pitch, heading: eye.heading)
    }

    func stop() { link?.invalidate(); link = nil }

    /// the finger lifted: the camera goes back to the day's whole, north up and flat, from where it stands (the fit is found first on the flat map, then the camera is put back to fly there)
    private func release(_ m: MKMapView) {
        stop(); eye.on = false
        let from = m.camera
        m.camera = MKMapCamera(lookingAtCenter: from.centerCoordinate, fromDistance: from.centerCoordinateDistance, pitch: 0, heading: 0)
        fit(m, animated: false)
        guard !TLMotion.reduced else { return }
        let to = m.camera
        m.camera = from
        UIView.animate(withDuration: TLMotion.mapReturn, delay: 0, options: [.curveEaseInOut, .allowUserInteraction]) { m.camera = to }
    }

    /// a stop after the held minute is faint
    private func alpha(_ n: FootprintMark.Note) -> CGFloat {
        guard let t = view?.heldMs, let ms = n.ms else { return 1 }
        return ms > t ? 0.4 : 1
    }

    private static func rect(_ r: MKCoordinateRegion) -> MKMapRect {
        let a = MKMapPoint(CLLocationCoordinate2D(latitude: r.center.latitude + r.span.latitudeDelta / 2, longitude: r.center.longitude - r.span.longitudeDelta / 2))
        let b = MKMapPoint(CLLocationCoordinate2D(latitude: r.center.latitude - r.span.latitudeDelta / 2, longitude: r.center.longitude + r.span.longitudeDelta / 2))
        return MKMapRect(x: min(a.x, b.x), y: min(a.y, b.y), width: abs(a.x - b.x), height: abs(a.y - b.y))
    }

    func mapView(_ mapView: MKMapView, rendererFor overlay: MKOverlay) -> MKOverlayRenderer {
        guard let o = overlay as? FootprintRoute else { return MKOverlayRenderer(overlay: overlay) }
        let r = FootprintRouteRenderer(o, width: view?.lineWidth ?? 4.2)
        r.litMs = view?.heldMs
        renderers.append(r)
        return r
    }

    func mapView(_ mapView: MKMapView, viewFor annotation: MKAnnotation) -> MKAnnotationView? {
        guard let n = annotation as? FootprintMark.Note else { return nil }
        let v = (mapView.dequeueReusableAnnotationView(withIdentifier: "mark") as? FootprintMark) ?? FootprintMark(annotation: n, reuseIdentifier: "mark")
        v.annotation = n
        v.configure(n)
        v.alpha = alpha(n)
        return v
    }
}
