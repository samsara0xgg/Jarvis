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
/// map keeps the heading it has) and how far out the camera stands, in metres, which the caller decides for a whole outing. The camera eases to all of it on one spring of `response` seconds;
/// `cut` takes it at once (Reduce Motion, the first frame).
struct FootprintMapFollow: Equatable {
    var coord: CLLocationCoordinate2D, heading: Double?, height: Double, response = TLMotion.mapResponse, cut = false
    static func == (a: Self, b: Self) -> Bool {
        a.coord.same(b.coord) && a.heading == b.heading && a.height == b.height && a.response == b.response && a.cut == b.cut
    }

    /// tilted forward as turn-by-turn navigation is
    static let pitch = 60.0
    /// How far out the camera stands on an outing, by what it was: on foot 500 m, awheel 800 m, driving 1.2 km (what the brain does not know is walked).
    static func height(activity: String?) -> Double {
        switch activity { case "cycling": 800; case "automotive": 1200; default: 500 }
    }
}

extension TLMotion {
    /// following him, and letting go (the camera goes back to the day's whole): the camera's one spring, the period in seconds of its undamped swing, while a finger scrubs
    static let mapResponse = 0.25
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
        if let f = v.follow { track(f, on: m) } else if cam != nil, !returning { release(m) }
    }

    private func fit(_ m: MKMapView, animated: Bool) {
        guard let v = view, !v.fitCoordinates.isEmpty else { return }
        m.setVisibleMapRect(Self.rect(PlaceFit.region(v.fitCoordinates)), edgePadding: v.fitPadding, animated: animated)
    }

    // MARK: first person

    /// Where the camera is while it follows him, and on its way back to the day's whole after: one spring for all of it (`FootprintCamera`), stepped by a display link that runs only while
    /// something moves. `cam` is nil while the map is his own.
    private var cam: FootprintCamera?
    private var returning = false
    private var link: CADisplayLink?
    private var lastTick = 0.0

    private func track(_ f: FootprintMapFollow, on m: FootprintMKMapView) {
        guard !m.bounds.isEmpty else {          // no size yet: the camera is taken once the map has one, and the first fit with it
            m.pendingFit = { [weak self, weak m] in
                guard let self, let m else { return }
                if let f = self.view?.follow { self.track(f, on: m) } else { self.fit(m, animated: false) }
            }
            return
        }
        if cam == nil {
            let c = m.camera, p = MKMapPoint(c.centerCoordinate)
            cam = FootprintCamera(x: p.x, y: p.y, heading: c.heading, pitch: c.pitch, height: c.centerCoordinateDistance)
        }
        let p = MKMapPoint(f.coord)
        returning = false
        cam?.aim(x: p.x, y: p.y, heading: f.heading, pitch: FootprintMapFollow.pitch, height: f.height, response: f.response)
        move(m, cut: f.cut)
    }

    /// the aims are in: taken at once, or the display link carries the camera there
    private func move(_ m: MKMapView, cut: Bool) {
        if cut || TLMotion.reduced { cam?.cut(); place(m); if returning { cam = nil; returning = false }; stop() }
        else if cam?.settled == false, link == nil {
            lastTick = 0
            link = CADisplayLink(target: self, selector: #selector(step))
            link?.add(to: .main, forMode: .common)
        }
    }

    @objc private func step(_ l: CADisplayLink) {
        guard let m = map, cam != nil else { return stop() }
        cam?.step(dt: lastTick == 0 ? l.targetTimestamp - l.timestamp : min(l.timestamp - lastTick, 0.05))
        lastTick = l.timestamp
        let done = cam?.settled ?? true
        if done { cam?.cut() }
        place(m)
        if done { stop(); if returning { cam = nil; returning = false } }
    }

    private func place(_ m: MKMapView) {
        guard let c = cam else { return }
        m.camera = MKMapCamera(lookingAtCenter: MKMapPoint(x: c.x.value, y: c.y.value).coordinate, fromDistance: c.height, pitch: c.pitch.value, heading: c.headingDegrees)
    }

    func stop() { link?.invalidate(); link = nil }

    /// the finger lifted: the camera goes back to the day's whole, north up and flat, on the same spring (the fit is found first on the flat map, then the camera is put back where it stands)
    private func release(_ m: MKMapView) {
        let from = m.camera
        m.camera = MKMapCamera(lookingAtCenter: from.centerCoordinate, fromDistance: from.centerCoordinateDistance, pitch: 0, heading: 0)
        fit(m, animated: false)
        let to = m.camera, p = MKMapPoint(to.centerCoordinate)
        returning = true
        cam?.aim(x: p.x, y: p.y, heading: 0, pitch: 0, height: to.centerCoordinateDistance, response: TLMotion.mapResponse)
        if TLMotion.reduced { move(m, cut: true) } else { place(m); move(m, cut: false) }
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
