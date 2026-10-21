import CoreLocation
import Observation

/// The street a stop with no name stood in, from Apple's reverse geocoder on the phone (the stop's coordinate goes to Apple for it, as the location reporter's does). Asked once for each place
/// and kept for the run, one at a time; until it answers, or when it cannot, a stop has no name and its row stays as it is. The pages ask (`ask`), the rows read (`street`).
@MainActor @Observable final class FootprintStreets {
    static let shared = FootprintStreets()

    private var names: [String: String] = [:]
    @ObservationIgnored private var asked: Set<String> = []
    @ObservationIgnored private var queue: [(key: String, at: CLLocation)] = []
    @ObservationIgnored private var running = false
    /// what answers for a place: the phone's geocoder, or a stand-in
    @ObservationIgnored var lookup: (CLLocation) async -> String? = FootprintStreets.reverse

    /// the cached street of a place about the size of a house lot, else nil
    func street(at c: CLLocationCoordinate2D) -> String? { names[Self.key(c)] }

    /// ask for the streets of places not asked for yet
    func ask(_ coords: [CLLocationCoordinate2D]) {
        for c in coords where asked.insert(Self.key(c)).inserted { queue.append((Self.key(c), CLLocation(latitude: c.latitude, longitude: c.longitude))) }
        guard !running, !queue.isEmpty else { return }
        running = true
        Task {
            while !queue.isEmpty {
                let next = queue.removeFirst()
                if let name = await lookup(next.at) { names[next.key] = name }
            }
            running = false
        }
    }

    /// about 11 m
    static func key(_ c: CLLocationCoordinate2D) -> String { String(format: "%.4f,%.4f", c.latitude, c.longitude) }

    /// the street of a place: its thoroughfare, else the quarter; nil when the geocoder has none or is not answered in six seconds
    private static func reverse(_ loc: CLLocation) async -> String? {
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
        guard let mark = marks?.first else { return nil }
        return [mark.thoroughfare, mark.subLocality, mark.locality].lazy.compactMap { $0 }.first { !$0.isEmpty }
    }
}
