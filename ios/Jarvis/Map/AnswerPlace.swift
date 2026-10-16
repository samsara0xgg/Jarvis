import CoreLocation
import MapKit

// The places an answer names (jarvis/surface, `surface.response_emitted` payload `places`, also on the conversation's rows): up to eight, each with a
// name and a coordinate. The talk shows them on a small map under the answer (Map/PlaceMapViews).

struct AnswerPlace: Equatable, Hashable, Identifiable {
    let name: String
    let lat: Double
    let lng: Double
    let address: String?
    let placeId: String?
    var id: String { placeId ?? "\(name)|\(lat),\(lng)" }
    var coordinate: CLLocationCoordinate2D { CLLocationCoordinate2D(latitude: lat, longitude: lng) }

    static let maxCount = 8

    /// The places of a payload or a conversation row, in the order the brain gave them. An entry without a name or with a coordinate off the globe is dropped,
    /// so is one of a kind this phone does not know (only `place` is sent now); a place named twice shows once.
    static func parse(_ raw: Any?) -> [AnswerPlace] {
        var out: [AnswerPlace] = []
        for p in (raw as? [[String: Any]]) ?? [] {
            guard (p["kind"] as? String ?? "place") == "place",
                  let name = (p["name"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines), !name.isEmpty,
                  let lat = (p["lat"] as? NSNumber)?.doubleValue, let lng = (p["lng"] as? NSNumber)?.doubleValue,
                  lat.isFinite, lng.isFinite, (-90...90).contains(lat), (-180...180).contains(lng) else { continue }
            let address = (p["address"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines)
            let place = AnswerPlace(name: name, lat: lat, lng: lng, address: address.flatMap { $0.isEmpty ? nil : $0 },
                                    placeId: (p["place_id"] as? String).flatMap { $0.isEmpty ? nil : $0 })
            if !out.contains(where: { $0.id == place.id }) { out.append(place) }
            if out.count == maxCount { break }
        }
        return out
    }
}

/// Where the camera looks so the places (and he, when he is near them) are all in view.
enum PlaceFit {
    /// no tighter than a few streets, however close the places are
    static let minSpan = 0.006
    /// room around the outermost points, as a multiple of their spread
    static let pad = 1.6
    /// he is left out of the fit when he is farther than this (m) from the places, which would otherwise shrink them to dots
    static let meReach = 10_000.0

    static func region(_ places: [AnswerPlace], me: CLLocationCoordinate2D?) -> MKCoordinateRegion {
        var pts = places.map(\.coordinate)
        if let me, let c = center(pts), CLLocation(latitude: c.latitude, longitude: c.longitude).distance(from: CLLocation(latitude: me.latitude, longitude: me.longitude)) <= meReach {
            pts.append(me)
        }
        guard let c = center(pts) else { return MKCoordinateRegion(center: CLLocationCoordinate2D(latitude: 0, longitude: 0), span: MKCoordinateSpan(latitudeDelta: 100, longitudeDelta: 100)) }
        let lats = pts.map(\.latitude), lngs = pts.map(\.longitude)
        let dLat = max(((lats.max() ?? 0) - (lats.min() ?? 0)) * pad, minSpan), dLng = max(((lngs.max() ?? 0) - (lngs.min() ?? 0)) * pad, minSpan)
        return MKCoordinateRegion(center: c, span: MKCoordinateSpan(latitudeDelta: dLat, longitudeDelta: dLng))
    }

    /// the middle of the box around the points
    private static func center(_ pts: [CLLocationCoordinate2D]) -> CLLocationCoordinate2D? {
        guard !pts.isEmpty else { return nil }
        let lats = pts.map(\.latitude), lngs = pts.map(\.longitude)
        return CLLocationCoordinate2D(latitude: ((lats.min() ?? 0) + (lats.max() ?? 0)) / 2, longitude: ((lngs.min() ?? 0) + (lngs.max() ?? 0)) / 2)
    }
}
