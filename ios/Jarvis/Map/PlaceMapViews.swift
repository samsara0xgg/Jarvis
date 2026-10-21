// words: legacy
import SwiftUI
import MapKit
import CoreLocation

// The places an answer names, as the talk shows them: a still map under the answer (a snapshot, so a long conversation does not hold a live map per row), and on a tap
// the whole map full screen, over everything like his pictures (Attach/PictureViewer). Apple Maps' own data and place cards; the hand-offs for a place of hers and for directions are Google Maps.

private let amber = Color(red: 1, green: 217 / 255, blue: 176 / 255)
private let navy = Color(red: 8 / 255, green: 10 / 255, blue: 24 / 255)

/// Where he was last known to be, if the phone has said so lately and he allowed it: a fix older than this is not "where he is".
enum MyPlace {
    static let maxAge: TimeInterval = 30 * 60
    #if DEBUG
    static var debug: CLLocationCoordinate2D?       // `-scene map`: a made-up position
    #endif

    @MainActor static func known() -> CLLocationCoordinate2D? {
        #if DEBUG
        if let debug { return debug }
        #endif
        let m = CLLocationManager()
        guard [.authorizedAlways, .authorizedWhenInUse].contains(m.authorizationStatus), let l = m.location, l.horizontalAccuracy >= 0,
              -l.timestamp.timeIntervalSinceNow <= maxAge else { return nil }
        return l.coordinate
    }
}

// MARK: the card under the answer

/// A still map of the places (and of him, when he is near), 156 pt high, with how many places it holds. A tap opens the full map. A row that is about one place (the timeline's stay)
/// asks for a smaller one (`height`, `radius`) without its name over it (`named`), which the row says already.
struct PlaceMapCard: View {
    let places: [AnswerPlace]
    var height = Self.height
    var radius = 14.0
    var named = true
    var width = Self.snapshotWidth          // what the snapshot is taken at: the card's own width, so the map's corner (Apple's mark) is not cut
    @State private var image: UIImage?
    @State private var me: CLLocationCoordinate2D?
    @Environment(\.displayScale) private var scale

    static let height = 156.0, snapshotWidth = 360.0

    var body: some View {
        ZStack(alignment: .topLeading) {
            // the picture fills the card and is cut to it: laid over the ground, so a snapshot wider than the room it is given widens nothing
            Color.white.opacity(0.07).overlay {
                if let image {
                    Image(uiImage: image).resizable().scaledToFill().transition(.opacity)
                } else {
                    Image(systemName: "map").font(.system(size: 22)).foregroundStyle(SetColor.muted)
                }
            }
            .clipped()
            if named {
                Text(places.count == 1 ? places[0].name : "\(places.count) 个地方")
                    .font(.system(size: 12, weight: .medium)).foregroundStyle(SetColor.ink.opacity(0.92)).lineLimit(1)
                    .padding(.horizontal, 10).frame(height: 24).background(navy.opacity(0.62), in: Capsule())
                    .padding(10)
            }
        }
        .frame(maxWidth: .infinity).frame(height: height)
        .clipShape(RoundedRectangle(cornerRadius: radius))
        .overlay(RoundedRectangle(cornerRadius: radius).stroke(Color.white.opacity(0.1), lineWidth: 0.5))
        .overlay(alignment: .topTrailing) {
            Image(systemName: "arrow.up.left.and.arrow.down.right").font(.system(size: 11, weight: .semibold)).foregroundStyle(SetColor.ink.opacity(0.85))
                .frame(width: 26, height: 26).background(navy.opacity(0.62), in: Circle()).padding(8)
        }
        .contentShape(RoundedRectangle(cornerRadius: radius))
        .onTapGesture { PlaceMapViewer.present(places, me: me) }
        .accessibilityElement().accessibilityLabel(places.count == 1 ? "地图，\(places[0].name)" : "地图，\(places.count) 个地方").accessibilityAddTraits(.isButton)
        .animation(.easeOut(duration: 0.2), value: image == nil)
        .task(id: places) {
            me = MyPlace.known()
            image = await PlaceSnapshots.image(places, me: me, size: CGSize(width: width, height: height), scale: scale)
        }
    }
}

/// The still maps, kept for the rows that come back into view.
@MainActor
enum PlaceSnapshots {
    private static let cache = NSCache<NSString, UIImage>()

    static func key(_ places: [AnswerPlace], me: CLLocationCoordinate2D?) -> String {
        places.map(\.id).joined(separator: ";") + (me.map { String(format: "@%.3f,%.3f", $0.latitude, $0.longitude) } ?? "")
    }

    static func image(_ places: [AnswerPlace], me: CLLocationCoordinate2D?, size: CGSize, scale: Double) async -> UIImage? {
        let k = (key(places, me: me) + "#\(Int(size.width))x\(Int(size.height))") as NSString
        if let hit = cache.object(forKey: k) { return hit }
        let o = MKMapSnapshotter.Options()
        o.region = PlaceFit.region(places, me: me)
        o.size = size; o.scale = scale
        o.traitCollection = UITraitCollection(userInterfaceStyle: .dark)
        o.pointOfInterestFilter = .excludingAll
        guard let snap = try? await MKMapSnapshotter(options: o).start() else { return nil }
        let image = UIGraphicsImageRenderer(size: size, format: { let f = UIGraphicsImageRendererFormat(); f.scale = scale; return f }()).image { c in
            snap.image.draw(at: .zero)
            if let me { dot(c.cgContext, snap.point(for: me), fill: UIColor(red: 157 / 255, green: 180 / 255, blue: 1, alpha: 1), halo: true) }
            for p in places { dot(c.cgContext, snap.point(for: p.coordinate), fill: UIColor(red: 1, green: 217 / 255, blue: 176 / 255, alpha: 1), halo: false) }
        }
        cache.setObject(image, forKey: k)
        return image
    }

    private static func dot(_ g: CGContext, _ p: CGPoint, fill: UIColor, halo: Bool) {
        if halo { g.setFillColor(fill.withAlphaComponent(0.25).cgColor); g.fillEllipse(in: CGRect(x: p.x - 13, y: p.y - 13, width: 26, height: 26)) }
        g.setFillColor(UIColor(red: 8 / 255, green: 10 / 255, blue: 24 / 255, alpha: 1).cgColor); g.fillEllipse(in: CGRect(x: p.x - 8, y: p.y - 8, width: 16, height: 16))
        g.setFillColor(fill.cgColor); g.fillEllipse(in: CGRect(x: p.x - 6, y: p.y - 6, width: 12, height: 12))
    }
}

// MARK: full screen

/// Open the full map on the top controller of the app. Nothing happens while one is already open.
enum PlaceMapViewer {
    @MainActor static func present(_ places: [AnswerPlace], me: CLLocationCoordinate2D?) {
        guard !places.isEmpty,
              var top = UIApplication.shared.connectedScenes.compactMap({ ($0 as? UIWindowScene)?.keyWindow }).first?.rootViewController else { return }
        while let next = top.presentedViewController { if next is PlaceMapController { return }; top = next }
        let host = PlaceMapController(rootView: AnyView(EmptyView()))
        host.rootView = AnyView(PlaceMapScreen(places: places, me: me, close: { [weak host] in host?.dismiss(animated: !UIAccessibility.isReduceMotionEnabled) }))
        host.modalPresentationStyle = .fullScreen
        host.overrideUserInterfaceStyle = .dark
        top.present(host, animated: !UIAccessibility.isReduceMotionEnabled)
    }
}

final class PlaceMapController: UIHostingController<AnyView> {}

/// The whole map: pan and zoom, his places marked (a tap on one names it and offers 去这里), every other place on it selectable for Apple's own card.
struct PlaceMapScreen: View {
    let places: [AnswerPlace]
    let me: CLLocationCoordinate2D?
    let close: () -> Void
    @State private var position: MapCameraPosition
    @State private var selection: MapSelection<String>?

    init(places: [AnswerPlace], me: CLLocationCoordinate2D?, close: @escaping () -> Void) {
        self.places = places; self.me = me; self.close = close
        _position = State(initialValue: .region(PlaceFit.region(places, me: me)))
        // one place is the point of the map: its name and the way there are already up
        _selection = State(initialValue: places.count == 1 ? MapSelection(places[0].id) : nil)
    }

    private var selected: AnswerPlace? { selection?.value.flatMap { id in places.first { $0.id == id } } }

    var body: some View {
        Map(position: $position, selection: $selection) {
            if let me {
                Annotation("", coordinate: me, anchor: .center) {
                    Circle().fill(Color(red: 157 / 255, green: 180 / 255, blue: 1)).frame(width: 12, height: 12)
                        .overlay(Circle().stroke(Color.white, lineWidth: 2)).background(Circle().fill(Color(red: 157 / 255, green: 180 / 255, blue: 1).opacity(0.25)).frame(width: 28, height: 28))
                        .accessibilityLabel("你在这里")
                }
                .annotationTitles(.hidden)
            }
            ForEach(places) { p in
                Marker(p.name, systemImage: "mappin", coordinate: p.coordinate).tint(amber).tag(MapSelection(p.id))
            }
        }
        .mapStyle(.standard(pointsOfInterest: .all))
        .mapFeatureSelectionAccessory(.automatic)
        .mapControls { MapCompass(); MapScaleView() }
        .ignoresSafeArea()
        .overlay(alignment: .topLeading) {
            Button(action: close) {
                Image(systemName: "xmark").font(.system(size: 14, weight: .semibold)).foregroundStyle(SetColor.ink).frame(width: 40, height: 40)
                    .background(navy.opacity(0.7), in: Circle()).overlay(Circle().stroke(Color.white.opacity(0.14), lineWidth: 0.5)).contentShape(Circle())
            }
            .buttonStyle(.plain).padding(.leading, 16).padding(.top, 8).accessibilityLabel("关闭地图")
        }
        .overlay(alignment: .bottom) {
            if let p = selected { PlaceInfoBar(place: p).padding(.horizontal, 16).padding(.bottom, 12).transition(.move(edge: .bottom).combined(with: .opacity)) }
        }
        .animation(.easeOut(duration: 0.2), value: selected?.id)
    }
}

/// A place he tapped: its name and address, its page in Google Maps, and the way there in Google Maps.
struct PlaceInfoBar: View {
    let place: AnswerPlace

    var body: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                Text(place.name).font(.system(size: 17, weight: .medium)).foregroundStyle(SetColor.ink).lineLimit(2)
                if let a = place.address { Text(a).font(.system(size: 13)).foregroundStyle(SetColor.muted).lineLimit(2) }
            }
            Spacer(minLength: 0)
            Button { PlaceHandoff.openInGoogle(place) } label: {
                Text("谷歌地图").font(.system(size: 15, weight: .medium)).foregroundStyle(amber)
                    .padding(.horizontal, 14).frame(minHeight: 40).overlay(Capsule().stroke(amber.opacity(0.6), lineWidth: 1)).contentShape(Capsule())
            }
            .buttonStyle(.plain)
            Button { PlaceHandoff.open(place) } label: {
                Text("去这里").font(.system(size: 15, weight: .semibold)).foregroundStyle(navy)
                    .padding(.horizontal, 16).frame(minHeight: 40).background(amber, in: Capsule()).contentShape(Capsule())
            }
            .buttonStyle(.plain)
        }
        .padding(.horizontal, 16).padding(.vertical, 12)
        .background(navy.opacity(0.88), in: RoundedRectangle(cornerRadius: 18))
        .overlay(RoundedRectangle(cornerRadius: 18).stroke(Color.white.opacity(0.1), lineWidth: 0.5))
    }
}

/// Hand-offs go to Google Maps (universal links: the app when it is installed, else the browser). Apple's own cards stay for the other places on the map.
enum PlaceHandoff {
    /// Google Maps transit directions to this place, from where he is
    static func directionsURL(_ place: AnswerPlace) -> URL? {
        var q = "destination=" + target(place)
        if let id = place.placeId { q += "&destination_place_id=" + enc(id) }
        return URL(string: "https://www.google.com/maps/dir/?api=1&" + q + "&travelmode=transit")
    }

    /// Google Maps' page for this place
    static func searchURL(_ place: AnswerPlace) -> URL? {
        var q = "query=" + target(place)
        if let id = place.placeId { q += "&query_place_id=" + enc(id) }
        return URL(string: "https://www.google.com/maps/search/?api=1&" + q)
    }

    @MainActor static func open(_ place: AnswerPlace) { go(directionsURL(place)) }
    @MainActor static func openInGoogle(_ place: AnswerPlace) { go(searchURL(place)) }

    @MainActor private static func go(_ url: URL?) { if let url { UIApplication.shared.open(url) } }

    /// Google wants the name next to a place id; with no id the coordinate says more than a name can
    private static func target(_ p: AnswerPlace) -> String { p.placeId != nil ? enc(p.name) : String(format: "%.6f,%.6f", p.lat, p.lng) }

    private static func enc(_ s: String) -> String { s.addingPercentEncoding(withAllowedCharacters: .urlQueryValue) ?? s }
}

private extension CharacterSet {
    /// unreserved characters only, so `&`, `+`, `=` and `,` in a name cannot cut the query
    static let urlQueryValue = CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
}
