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
/// `still`: he stands at a long stay; the words of the marks there give way to the dot's own.
struct FootprintMapDot: Equatable {
    var coord: CLLocationCoordinate2D, ms: Int64, rgb: RGB, label: String?, held = true, still = false
    static func == (a: Self, b: Self) -> Bool { a.coord.same(b.coord) && a.ms == b.ms && a.rgb == b.rgb && a.label == b.label && a.held == b.held && a.still == b.still }
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

    /// the most the camera tilts at a height: all of `pitch` up to 4 km, none from 15 km, between by the log of the height. Going up to the day's whole, it is flat before the ground it would show to
    /// the horizon gets wide (a tilted view from high up asks for so many tiles that the map stops drawing for seconds); coming down, it tilts as it nears him.
    static func pitch(at height: Double) -> Double {
        pitch * min(max((log(15_000) - log(height)) / (log(15_000) - log(4_000)), 0), 1)
    }
}

extension TLMotion {
    /// following him: the camera's spring for heading and height, the period in seconds of its undamped swing, while a finger scrubs
    static let mapResponse = 0.25
    /// the camera going down to him when a finger lands or ▶ starts, and back up to the day's whole after: every part of it on this spring (most of the way in about 0.7 s)
    static let mapFlight = 0.9
    /// the camera while the ▶ replay plays: the replay gives the exact place a frame at a time, so the spring only has to smooth the turns and the beats' flights
    static let replayResponse = 0.2
    /// the held bar turning into its trips when a finger lands, and back when it lifts
    static let tripMorph = 0.2
    static func tripMorph(reduce: Bool) -> Animation { reduce ? .easeOut(duration: reducedFade) : .smooth(duration: tripMorph) }
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
    /// a finger lands on the map (the replay stops there)
    var touched: (() -> Void)?

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
        let touch = UILongPressGestureRecognizer(target: context.coordinator, action: #selector(FootprintMapCoordinator.touch(_:)))
        touch.minimumPressDuration = 0
        touch.cancelsTouchesInView = false
        touch.delegate = context.coordinator
        m.addGestureRecognizer(touch)
        return m
    }

    func updateUIView(_ m: FootprintMKMapView, context: Context) {
        m.layoutMargins.bottom = legalInset
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

final class FootprintMapCoordinator: NSObject, MKMapViewDelegate, UIGestureRecognizerDelegate {
    private var view: FootprintMapView?
    private var fitKey: String?
    private var staticMarks: [FootprintMark.Note] = []
    private var nowNote: FootprintMark.Note?
    private var dotNote: FootprintMark.Note?
    private var renderers: [FootprintRouteRenderer] = []
    private weak var map: FootprintMKMapView?
    #if DEBUG
    fileprivate var probeView: FootprintCameraProbe?
    #endif

    @objc func touch(_ g: UILongPressGestureRecognizer) { if g.state == .began { view?.touched?() } }
    func gestureRecognizer(_ g: UIGestureRecognizer, shouldRecognizeSimultaneouslyWith other: UIGestureRecognizer) -> Bool { true }

    func apply(_ v: FootprintMapView, to m: FootprintMKMapView) {
        let old = view
        view = v
        map = m
        if old?.routes != v.routes {
            m.removeOverlays(m.overlays)
            renderers.removeAll()
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
            if dotNote != nil {
                pendingDot = d
            } else {
                let n = FootprintMark.Note(d.coord, .dot(d.rgb), label: d.label, rank: 4)
                dotNote = n; m.addAnnotation(n)
            }
        } else if let n = dotNote { m.removeAnnotation(n); dotNote = nil; pendingDot = nil }
        if old?.heldMs != v.heldMs { pendingLit = true }
        for n in staticMarks + [nowNote].compactMap({ $0 }) { (m.view(for: n) as? FootprintMark)?.mute = muted(n) }
        if fitKey != v.fitKey {
            let first = fitKey == nil
            fitKey = v.fitKey
            m.pendingFit = { [weak m, weak self] in if let m { self?.fit(m, animated: !first) } }
            if !m.bounds.isEmpty { m.setNeedsLayout() }
        }
        if let f = v.follow { track(f, on: m) } else if cam != nil, !returning { release(m) }
        if link == nil { flush(m) }
        putOwnDot(m)
        #if DEBUG
        probe(m)
        #endif
    }

    /// a mark's words give way to the dot's while he stands at its place
    private func muted(_ n: FootprintMark.Note) -> Bool {
        guard n !== dotNote, n.label != nil, let d = view?.dot, d.still else { return false }
        return FootprintTrack.metres(n.coordinate, d.coord).distance < 60
    }

    /// The dot and the lit end of the line move with the camera: one writer, the display link's step, puts all three from the same moment, the camera first, so that a quick drag never
    /// shows the dot, the lit end and the ground at different moments.
    private var pendingDot: FootprintMapDot?, pendingLit = false
    private func flush(_ m: MKMapView) {
        if let d = pendingDot, let n = dotNote {
            pendingDot = nil
            n.coordinate = d.coord
            if n.glyph != .dot(d.rgb) || n.label != d.label { n.set(.dot(d.rgb), label: d.label); (m.view(for: n) as? FootprintMark)?.configure(n); ownDot?.configure(n) }
        }
        // the map draws its line in tiles a frame or two after it is asked, so while the camera follows, the line is lit as far as the held minute will be by then
        let t = view?.heldMs
        if t != heldBefore { pace = cam != nil && !returning ? t.flatMap { t in heldBefore.map { t - $0 } } ?? 0 : 0; heldBefore = t; unmoved = 0 } else { unmoved += 1; if unmoved > 1 { pace = 0 } }
        let lit = t.map { $0 + Self.litAhead * min(max(pace, -5_000), 5_000) }          // not across a gap's minutes in one go
        if lit != litNow {
            litNow = lit
            let pad = max(20, m.camera.centerCoordinateDistance * 0.015)          // metres round what changed: the line's width and casing at this zoom, so that few tiles are drawn again
            renderers.forEach { $0.light(lit, padMetres: pad) }
        }
        guard pendingLit else { return }
        pendingLit = false
        for n in staticMarks { (m.view(for: n) as? FootprintMark)?.alpha = alpha(n) }
    }
    /// the held minute of the step before and how far it moved then, and what the line is lit to
    private var heldBefore: Int64?, pace: Int64 = 0, unmoved = 0, litNow: Int64?
    /// frames the line's tiles come late by
    static let litAhead: Int64 = 2

    /// The dot while the camera is ours (a follow, and its flight back): a view of its own over the map, put by the camera just written. MapKit moves an annotation's view a frame or more
    /// after the camera, so on a quick drag it stood ahead of him and jumped. Its annotation stands hidden meanwhile and shows again once the map is his.
    private var ownDot: FootprintMark?
    private func putOwnDot(_ m: MKMapView) {
        let n = dotNote, ours = cam != nil && n != nil
        if let n { m.view(for: n)?.isHidden = ours }
        guard ours, let n else { ownDot?.isHidden = true; return }
        let v: FootprintMark
        if let o = ownDot { v = o } else {
            v = FootprintMark(annotation: nil, reuseIdentifier: nil)
            v.isUserInteractionEnabled = false
            m.addSubview(v)
            ownDot = v
        }
        if v.isHidden { v.configure(n); v.isHidden = false }
        let p = m.convert(n.coordinate, toPointTo: m)
        v.center = CGPoint(x: p.x + v.centerOffset.x, y: p.y + v.centerOffset.y)
    }

    private func fit(_ m: MKMapView, animated: Bool) {
        guard let v = view, !v.fitCoordinates.isEmpty else { return }
        m.setVisibleMapRect(Self.rect(PlaceFit.region(v.fitCoordinates)), edgePadding: v.fitPadding, animated: animated)
    }

    // MARK: first person

    /// Where the camera is while it follows him, and on its way back to the day's whole after (`FootprintCamera`), stepped by a display link that runs while it follows and while it lands.
    /// `cam` is nil while the map is his own; `moved`: an aim has come since the camera was last put.
    private var cam: FootprintCamera?
    private var returning = false
    private var moved = false
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
        var jump = returning          // a hold again while it goes back: from where it stands
        if cam == nil {
            let c = m.camera, p = MKMapPoint(c.centerCoordinate)
            cam = FootprintCamera(x: p.x, y: p.y, heading: c.heading, pitch: c.pitch, height: c.centerCoordinateDistance)
            cam?.top = FootprintCamera.Spring(Double(m.layoutMargins.top))
            jump = true
        }
        let p = MKMapPoint(f.coord)
        returning = false
        cam?.aim(x: p.x, y: p.y, heading: f.heading, pitch: FootprintMapFollow.pitch, height: f.height, response: f.response, top: view?.fitPadding.top ?? 0, jump: jump)          // the camera's centre is the middle of what the margins leave: he stands in the clear of the fade
        moved = true
        move(m, cut: f.cut)
    }

    /// the aims are in: taken at once, or the display link carries the camera there
    private func move(_ m: MKMapView, cut: Bool) {
        if cut || TLMotion.reduced { cam?.cut(); place(m); flush(m); moved = false; if returning { cam = nil; returning = false }; putOwnDot(m); stop() }
        else if link == nil {
            lastTick = 0
            link = CADisplayLink(target: self, selector: #selector(step))
            link?.add(to: .main, forMode: .common)
        }
    }

    /// a frame: the camera steps and is put, then the dot and the lit end of the same moment (he is at the camera's centre); the link stops once a let-go has landed, or when nothing
    /// moves and no follow is held
    @objc private func step(_ l: CADisplayLink) {
        guard let m = map, cam != nil else { return stop() }
        let dt = lastTick == 0 ? l.targetTimestamp - l.timestamp : min(l.timestamp - lastTick, 0.05)
        lastTick = l.timestamp
        let still = cam?.settled ?? true
        if !still || moved {
            cam?.step(dt: dt)
            if cam?.settled == true { cam?.cut() }
            place(m)
            flush(m)
            putOwnDot(m)
            moved = false
        }
        #if DEBUG
        probe(m, dt: dt)
        #endif
        guard cam?.settled ?? true else { return }
        if returning { cam = nil; returning = false; putOwnDot(m); stop() } else if view?.follow == nil { stop() }
    }

    private func place(_ m: MKMapView) {
        guard let c = cam else { return }
        m.layoutMargins.top = CGFloat(c.top.value)
        m.camera = MKMapCamera(lookingAtCenter: MKMapPoint(x: c.x, y: c.y).coordinate, fromDistance: c.height, pitch: min(c.pitch.value, FootprintMapFollow.pitch(at: c.height)), heading: c.headingDegrees)
    }

    func stop() { link?.invalidate(); link = nil }

    /// the finger lifted: the camera goes back to the day's whole, north up and flat, on the same spring (the fit is found first on the flat map, then the camera is put back where it stands)
    private func release(_ m: MKMapView) {
        let from = m.camera
        m.layoutMargins.top = 0          // the fit is found with none of the follow's margin; `place` puts it back as it stands
        m.camera = MKMapCamera(lookingAtCenter: from.centerCoordinate, fromDistance: from.centerCoordinateDistance, pitch: 0, heading: 0)
        fit(m, animated: false)
        let to = m.camera, p = MKMapPoint(to.centerCoordinate)
        returning = true
        cam?.aim(x: p.x, y: p.y, heading: 0, pitch: 0, height: to.centerCoordinateDistance, response: TLMotion.mapResponse, top: 0, jump: true)
        moved = true
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
        let r = FootprintRouteRenderer(o, width: view?.lineWidth ?? 4.2, litMs: view?.heldMs)
        renderers.append(r)
        return r
    }

    func mapView(_ mapView: MKMapView, viewFor annotation: MKAnnotation) -> MKAnnotationView? {
        guard let n = annotation as? FootprintMark.Note else { return nil }
        let v = (mapView.dequeueReusableAnnotationView(withIdentifier: "mark") as? FootprintMark) ?? FootprintMark(annotation: n, reuseIdentifier: "mark")
        v.annotation = n
        v.configure(n)
        v.alpha = alpha(n)
        v.mute = muted(n)
        v.isHidden = n === dotNote && cam != nil          // the dot of its own stands over it
        return v
    }
}

#if DEBUG
// MARK: the probe

/// `-footprintDebug`, for the recordings: over the map a cross at the camera's centre and a ring where the dot is, and the camera's numbers as they are and as they are aimed, so that a
/// heading's jump or a height's pumping can be read off a frame by number.
final class FootprintCameraProbe {
    static let on = ProcessInfo.processInfo.arguments.contains("-footprintDebug")
    private let label = UILabel(), cross = CAShapeLayer(), ring = CAShapeLayer()

    init(on m: UIView) {
        label.numberOfLines = 0
        label.font = .monospacedDigitSystemFont(ofSize: 11, weight: .semibold)
        label.textColor = .yellow
        label.backgroundColor = UIColor.black.withAlphaComponent(0.55)
        label.frame = CGRect(x: 8, y: 74, width: 320, height: 58)
        let plus = UIBezierPath()
        plus.move(to: CGPoint(x: -12, y: 0)); plus.addLine(to: CGPoint(x: 12, y: 0)); plus.move(to: CGPoint(x: 0, y: -12)); plus.addLine(to: CGPoint(x: 0, y: 12))
        cross.path = plus.cgPath; cross.strokeColor = UIColor.yellow.cgColor; cross.lineWidth = 1.5
        ring.path = UIBezierPath(ovalIn: CGRect(x: -9, y: -9, width: 18, height: 18)).cgPath; ring.strokeColor = UIColor.cyan.cgColor; ring.fillColor = nil; ring.lineWidth = 1.5
        m.layer.addSublayer(cross); m.layer.addSublayer(ring); m.addSubview(label)
    }

    /// `drawn`: where the dot's view stands on the map, which MapKit places itself; `dot` is where its coordinate falls by the map's camera
    func show(_ c: FootprintCamera?, map m: MKMapView, dot: CLLocationCoordinate2D?, drawn: CGPoint?, dt: Double?) {
        CATransaction.begin(); CATransaction.setDisableActions(true)
        if m.layer.sublayers?.last !== ring { m.layer.addSublayer(cross); m.layer.addSublayer(ring) }          // over the dot's own view
        let centre = m.convert(m.camera.centerCoordinate, toPointTo: m)
        cross.position = centre
        if let dot { ring.isHidden = false; ring.position = m.convert(dot, toPointTo: m) } else { ring.isHidden = true }
        CATransaction.commit()
        let dp = dot.map { m.convert($0, toPointTo: m) }, d = dp.map { String(format: "dot %+5.1f %+5.1f", $0.x - centre.x, $0.y - centre.y) } ?? "dot -"
        let v = drawn.map { String(format: "  drawn %+5.1f %+5.1f pt", $0.x - centre.x, $0.y - centre.y) } ?? ""
        if let dt { lastDt = dt }
        let draw = FootprintRouteRenderer.drawClock.take()
        if let c {
            let k = MKMapPointsPerMeterAtLatitude(m.camera.centerCoordinate.latitude), off = hypot(c.x - c.aimX, c.y - c.aimY) / k
            let back = MKMapPoint(m.camera.centerCoordinate)          // the camera MapKit reports, read back after the write
            if dt != nil {          // a line a frame, for reading the recordings by number (`log stream`, FPCAM)
                NSLog("FPCAM %.4f hdg %.3f %.3f dist %.1f %.1f pitch %.2f dot %.2f %.2f cx %.1f cy %.1f fly %d drawn %.2f %.2f back %.2f %.2f", CACurrentMediaTime(), c.headingDegrees, m.camera.heading, m.camera.centerCoordinateDistance,
                      exp(c.logHeight.target), m.camera.pitch, (dp?.x ?? .nan) - centre.x, (dp?.y ?? .nan) - centre.y, centre.x, centre.y, c.flying ? 1 : 0,
                      (drawn?.x ?? .nan) - centre.x, (drawn?.y ?? .nan) - centre.y, (back.x - c.x) / k, (back.y - c.y) / k)
            }
            label.text = String(format: " hdg %6.1f -> %6.1f%@\n dist %6.0f -> %6.0f m  pitch %4.1f\n %@%@  off %4.0f m\n dt %4.1f ms  route draw max %4.1f ms",
                                c.headingDegrees, (c.heading.target.truncatingRemainder(dividingBy: 360) + 360).truncatingRemainder(dividingBy: 360), c.flying ? "  fly" : "",
                                m.camera.centerCoordinateDistance, exp(c.logHeight.target), m.camera.pitch, d, v, off, lastDt * 1000, draw)
        } else {
            label.text = String(format: " free  hdg %6.1f\n dist %6.0f m  pitch %4.1f\n %@%@\n dt %4.1f ms  route draw max %4.1f ms", m.camera.heading, m.camera.centerCoordinateDistance, m.camera.pitch, d, v, lastDt * 1000, draw)
        }
    }
    private var lastDt = 0.0
}

extension FootprintMapCoordinator {
    fileprivate func probe(_ m: MKMapView, dt: Double? = nil) {
        guard FootprintCameraProbe.on else { return }
        if probeView == nil { probeView = FootprintCameraProbe(on: m) }
        let shown = ownDot?.isHidden == false ? ownDot : dotNote.flatMap { m.view(for: $0) as? FootprintMark }
        let drawn = shown.map { $0.convert(CGPoint(x: FootprintMark.Glyph.dot(RGB(0, 0, 0)).extent, y: $0.bounds.midY), to: m) }
        probeView?.show(cam, map: m, dot: dotNote?.coordinate, drawn: drawn, dt: dt)
    }
}
#endif
