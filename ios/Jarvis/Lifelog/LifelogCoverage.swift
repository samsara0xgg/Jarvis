// words: legacy
import CoreLocation
import Foundation
import Observation

// The phone keeps its day's audio unless the Mac is recording him and he is home (ADR 0292). The brain gives facts
// (GET /inherent/lifelog/coverage), the phone decides; every doubt keeps the audio. The engine and the session go on as they were; only the
// writer stops keeping (LifelogWriter.setDropping), and closes the piece it had.

/// What the brain says (docs/spec.html, lifelog coverage).
struct LifelogCoverage: Decodable, Equatable {
    var nowMs: Int64
    /// a connected terminal's newest archived piece ended within 15 minutes
    var macRecording: Bool
    var terminal: String?
    var newestPieceEndMs: Int64?
    var home: Home?

    struct Home: Decodable, Equatable {
        var lat: Double
        var lng: Double
        var radiusM: Double
    }

    static func decode(_ data: Data) -> LifelogCoverage? {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return try? d.decode(LifelogCoverage.self, from: data)
    }
}

/// A position of the phone and when it was taken.
struct LifelogFix: Equatable {
    var lat: Double
    var lng: Double
    var accuracyM: Double
    var at: Date
}

/// Keep the audio, or not, and why. `code` goes in lifelog.log, `line` in Settings.
struct LifelogDecision: Equatable {
    var keep: Bool
    var code: String
    var line: String
}

enum LifelogPolicy {
    /// a position older than this does not say where he is
    static let freshFix: TimeInterval = 15 * 60
    /// a position vaguer than this (metres) cannot say he is inside a house's circle
    static let vagueAccuracy: Double = 150
    /// how long a decision to drop holds without being renewed (the gate asks every 2 minutes)
    static let holdFor: TimeInterval = 5 * 60

    /// Not keeping needs all of it: the brain says a terminal records, it knows his home, and a fresh, clear position is inside it.
    static func decide(_ coverage: LifelogCoverage?, fix: LifelogFix?, now: Date = Date()) -> LifelogDecision {
        func keep(_ code: String, _ why: String) -> LifelogDecision { .init(keep: true, code: code, line: why + "，手机在录") }
        guard let c = coverage else { return keep("no-answer", "问不到大脑") }
        guard c.macRecording else { return keep("mac-not-recording", "Mac 没在录") }
        guard let h = c.home else { return keep("no-home", "还没存家的位置") }
        guard let f = fix else { return keep("no-fix", "没有位置") }
        guard now.timeIntervalSince(f.at) <= freshFix else { return keep("stale-fix", "位置太旧") }
        guard f.accuracyM >= 0, f.accuracyM <= vagueAccuracy else { return keep("vague-fix", "位置不够准") }
        let away = CLLocation(latitude: f.lat, longitude: f.lng).distance(from: CLLocation(latitude: h.lat, longitude: h.lng))
        guard away <= h.radiusM else { return keep("away", "不在家") }
        return .init(keep: false, code: "home-mac-recording", line: "在家，Mac 在录，手机暂不留音频")
    }
}

/// The phone's own position, asked for only when the decision needs it: one fix at a time, never a prompt, never reported to the brain
/// (the location sense has its own manager). Nil without permission.
@MainActor
final class LifelogHere: NSObject, CLLocationManagerDelegate {
    static let shared = LifelogHere()
    private let manager = CLLocationManager()
    private var waiting: [CheckedContinuation<LifelogFix?, Never>] = []
    private var round = 0                                       // a timeout belongs to the round that set it

    override init() {
        super.init()
        manager.delegate = self
        manager.desiredAccuracy = kCLLocationAccuracyHundredMeters
    }

    func fix() async -> LifelogFix? {
        switch manager.authorizationStatus {
        case .authorizedAlways, .authorizedWhenInUse: break
        default: return nil
        }
        return await withCheckedContinuation { c in
            waiting.append(c)
            if waiting.count == 1 {
                manager.requestLocation()
                // requestLocation can stay silent in the background: the newest position iOS has is the answer then
                let mine = round
                Task { try? await Task.sleep(for: .seconds(20)); if mine == self.round { self.finish(self.manager.location) } }
            }
        }
    }

    private func finish(_ loc: CLLocation?) {
        let fix = loc.flatMap { $0.horizontalAccuracy >= 0 ? LifelogFix(lat: $0.coordinate.latitude, lng: $0.coordinate.longitude, accuracyM: $0.horizontalAccuracy, at: $0.timestamp) : nil }
        let all = waiting
        waiting = []
        round += 1
        all.forEach { $0.resume(returning: fix) }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        let last = locations.last
        Task { @MainActor in self.finish(last) }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        Task { @MainActor in self.finish(self.manager.location) }
    }
}

/// Asks the brain every 2 minutes while the switch is on (and at each piece cut), decides, and tells the writer.
@MainActor @Observable
final class LifelogGate {
    static let shared = LifelogGate()
    static let every: TimeInterval = 2 * 60

    /// the reason for what the writer does now, for Settings; empty while the switch is off
    private(set) var line = ""
    private(set) var keeping = true

    @ObservationIgnored var fetch: () async -> LifelogCoverage? = { await LifelogGate.askBrain() }
    @ObservationIgnored var locate: () async -> LifelogFix? = { await LifelogHere.shared.fix() }
    @ObservationIgnored private let writer: LifelogWriter
    @ObservationIgnored private var loop: Task<Void, Never>?
    @ObservationIgnored private var asking = false
    @ObservationIgnored private var lastCode: String?

    init(writer: LifelogWriter = .shared) { self.writer = writer }

    /// The switch: on starts asking, off lets the writer keep everything again.
    func run(_ on: Bool) {
        guard on else {
            loop?.cancel(); loop = nil
            writer.setDropping(until: nil)
            keeping = true; line = ""; lastCode = nil
            return
        }
        guard loop == nil else { return }
        loop = Task { [weak self] in
            while !Task.isCancelled {
                await self?.ask()
                try? await Task.sleep(for: .seconds(Self.every))
            }
        }
    }

    /// Now, outside the 2-minute rhythm (a piece was cut).
    func refresh() {
        guard loop != nil else { return }
        Task { await ask() }
    }

    /// One round: coverage, then a position only if the answer could still say "drop" (the fix costs battery), then the decision.
    func ask() async {
        guard !asking else { return }
        asking = true
        defer { asking = false }
        let coverage = await fetch()
        let fix = coverage?.macRecording == true && coverage?.home != nil ? await locate() : nil
        apply(LifelogPolicy.decide(coverage, fix: fix))
    }

    func apply(_ d: LifelogDecision) {
        guard loop != nil else { return }                       // the switch went off while the question was out
        writer.setDropping(until: d.keep ? nil : LifelogClock.nowMs() + Int64(LifelogPolicy.holdFor * 1000))
        line = d.line
        if keeping != d.keep || lastCode == nil {
            lifelogNote("\(d.keep ? "keeping" : "not keeping") the audio: \(d.code)")
        } else if lastCode != d.code {
            lifelogNote("still \(d.keep ? "keeping" : "not keeping") the audio: \(d.code)")
        }
        keeping = d.keep
        lastCode = d.code
    }

    /// GET /inherent/lifelog/coverage; nil on any failure.
    nonisolated static func askBrain() async -> LifelogCoverage? {
        guard let ep = await endpoint() else { return nil }
        var req = URLRequest(url: ep.base.appendingPathComponent("inherent/lifelog/coverage"))
        req.setValue("Bearer \(ep.token)", forHTTPHeaderField: "Authorization")
        req.timeoutInterval = 10
        guard let (data, response) = try? await URLSession(configuration: .ephemeral).data(for: req),
              (response as? HTTPURLResponse)?.statusCode == 200 else { return nil }
        return LifelogCoverage.decode(data)
    }

    @MainActor private static func endpoint() -> (base: URL, token: String)? {
        #if DEBUG
        // `-lifelogStubURL http://127.0.0.1:PORT -lifelogStubToken x` asks a local stub server, never the brain
        let d = UserDefaults.standard
        if let base = d.string(forKey: "lifelogStubURL").flatMap(URL.init(string:)), let token = d.string(forKey: "lifelogStubToken") { return (base, token) }
        #endif
        return BrainLink.shared.voiceEndpoint
    }
}
