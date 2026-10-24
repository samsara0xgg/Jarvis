import Foundation
import Observation

// His body over a window beside his usual level (ADR 0411, contracts/body/window.json): `GET /inherent/body?start_ms=&end_ms=`, asked when a stretch or a night is opened and
// kept per window. The brain judges nothing beyond below, within or above his usual middle half; a measure it could not set beside his usual (`vs_usual` null) is not shown.

/// a window of his day, in epoch ms, at most 25 hours
struct BodyWindow: Hashable {
    let startMs: Int64
    let endMs: Int64
}

/// one measure of the answer: a name the phone does not know, or a value that is not a number, is kept out
struct BodyMeasure: Decodable {
    let unit: String
    @Soft var value: Double?
    @Soft var vs_usual: String?
}

/// the answer: the measures by name (`steps`, `heart_rate`, `resting_heart_rate`, `hrv`, `breathing_rate`, `blood_oxygen`, `sleep`; more may come)
struct BodyReply: Decodable {
    let measures: [String: BodyMeasure]

    init(from d: Decoder) throws {
        let c = try d.container(keyedBy: Keys.self)
        let raw = (try? c.decode([String: Lossy<BodyMeasure>].self, forKey: .measures)) ?? [:]
        measures = raw.compactMapValues(\.value)
    }
    private enum Keys: String, CodingKey { case measures }
}

/// One line of 跟平时比: the measure's name, its value with its unit, and where it stands against his usual level.
struct BodyLine: Equatable, Identifiable {
    enum Mark: String { case below, within, above }
    let id: String                      // the measure's name
    let label: String
    let value: String
    let mark: Mark
}

extension BrainClient {
    static func body(_ w: BodyWindow, bases: [URL], token: String) async throws -> BodyReply {
        let r = try await send("GET", "/inherent/body", query: [URLQueryItem(name: "start_ms", value: String(w.startMs)), URLQueryItem(name: "end_ms", value: String(w.endMs))],
                               bases: bases, token: token, unauthorized: .revoked)
        guard let reply = try? JSONDecoder().decode(BodyReply.self, from: r.data) else { throw BrainError.garbled }
        return reply
    }
}

/// The answers by window: asked once the app has been opened since (`BrainLink.openTick`), kept while a new one comes. A window that fails (an older brain, no answer) has none.
@Observable @MainActor
final class BodyStore {
    static let shared = BodyStore()
    private var replies: [BodyWindow: BodyReply] = [:]
    @ObservationIgnored private var tickOf: [BodyWindow: Int] = [:]
    @ObservationIgnored private var inFlight: Set<BodyWindow> = []
    /// where the brain is asked: a test, or the simulator's made-up day, hands in its own
    @ObservationIgnored var fetch: (BodyWindow) async -> BodyReply? = { w in try? await BrainLink.shared.call { try await BrainClient.body(w, bases: $0, token: $1) } }

    func reply(_ w: BodyWindow) -> BodyReply? { replies[w] }

    func ensure(_ w: BodyWindow) {
        let tick = BrainLink.shared.openTick
        guard w.endMs > w.startMs, !inFlight.contains(w), tickOf[w] != tick else { return }
        inFlight.insert(w)
        Task {
            let got = await fetch(w)
            inFlight.remove(w)
            tickOf[w] = tick
            if let got { replies[w] = got }
        }
    }

    /// The lines of `names`, in that order: each that has a value and a place against his usual level.
    func lines(_ w: BodyWindow, _ names: [String]) -> [BodyLine] { replies[w].map { Self.lines($0, names) } ?? [] }

    static func lines(_ r: BodyReply, _ names: [String]) -> [BodyLine] {
        names.compactMap { n in
            guard let m = r.measures[n], let v = m.value, v.isFinite, let mark = m.vs_usual.flatMap(BodyLine.Mark.init(rawValue:)), let label = Words.PhoneRow.measure(n) else { return nil }
            return BodyLine(id: n, label: label, value: value(v, m.unit), mark: mark)
        }
    }

    /// 2,500 步, 55 次/分, 42 ms, 14.5 次/分, 97%, 7 小时: the value as the page writes it
    static func value(_ v: Double, _ unit: String) -> String {
        switch unit {
        case "steps": return tlGroup(tlInt(v)) + Words.PhoneRow.stepsUnit
        case "bpm": return "\(tlInt(v))" + Words.PhoneRow.perMinute
        case "/min": return (v.rounded() == v ? "\(tlInt(v))" : String(format: "%.1f", v)) + Words.PhoneRow.perMinute
        case "%": return "\(tlInt(v))%"
        case "min": return tlDur(tlInt(v))
        default: return "\(tlInt(v)) \(unit)"
        }
    }
}
