import Foundation
import Observation

// What she is doing in a turn, in one model (ADR 0228). The rows of the talk, and later her orb, read it; nothing here draws.
//
// The brain sends a `step` frame when a tool of the turn starts and again when it ends, with its own display text: the label is shown as it came, never
// built here. The phone keeps its own clock: a step's elapsed time counts from the frame's arrival, not from the host's `started_ms`.

/// One step of an answered turn, as the conversation row (`steps`) or the live frames say it. `ms` is a duration, so the host's clock is fine.
struct StepRecord: Equatable {
    let label: String
    let ms: Int
    let failed: Bool

    var time: String { ms < 1000 ? "不到 1 秒" : "\(Int((Double(ms) / 1000).rounded())) 秒" }
}

/// What the small line under her answer says, and what it opens to.
struct StepsInfo: Equatable {
    /// the brain's past-tense phrase (「查了公交、搜了网页」); empty while only the live steps are known
    var summary: String
    var totalMs: Int
    var list: [StepRecord]

    /// 「查了公交、搜了网页 · 9 秒」; a total under half a second has no time
    var line: String {
        let text = summary.isEmpty ? "\(list.count) 步" : summary
        guard totalMs >= 500 else { return text }
        return text + " · \(max(1, Int((Double(totalMs) / 1000).rounded()))) 秒"
    }

    /// the `steps`, `steps_ms` and `steps_summary` of a conversation row; nil when no tool ran
    static func parse(_ row: [String: Any]) -> StepsInfo? {
        guard let raw = row["steps"] as? [[String: Any]] else { return nil }
        let list = raw.compactMap { s -> StepRecord? in
            guard let label = s["label"] as? String else { return nil }
            return StepRecord(label: label, ms: (s["ms"] as? NSNumber)?.intValue ?? 0, failed: s["failed"] as? Bool ?? false)
        }
        if list.isEmpty { return nil }
        return StepsInfo(summary: row["steps_summary"] as? String ?? "", totalMs: (row["steps_ms"] as? NSNumber)?.intValue ?? list.reduce(0) { $0 + $1.ms }, list: list)
    }
}

/// The line that stands in her place while she works: a step's own text with its elapsed seconds, or (no label) the quiet dots.
struct StatusLine: Equatable {
    var label: String?
    /// phone uptime in seconds from which the step's seconds count
    var since = 0.0
}

/// One turn from the phone's side: sent, received, steps, answer begun, over. Pure: every call takes the phone's clock, in seconds.
struct TurnStatus: Equatable {
    struct Step: Equatable {
        var n: Int
        var label: String
        var state: StepFrame.State
        var arrivedAt: Double
        var endedAt: Double?
        var startedMs: Int64
        var endedMs: Int64?
    }
    /// where the turn is, for whatever shows it
    enum Phase: Equatable { case sent, working, answering, over }

    /// a step label on show stays this long before anything replaces it (it is read in 400 ms or it is flicker)
    static let hold = 0.4

    let utterance: String
    private(set) var turn: String?
    let sentAt: Double
    private(set) var steps: [Int: Step] = [:]
    private(set) var leadInAt: Double?
    private(set) var answerAt: Double?
    private(set) var endedAt: Double?
    /// the host ended the turn with `response.failed`: there will be no answer
    private(set) var failed = false
    private var waitLines = Set<String>()
    private var chunks = Set<String>()          // the event uids of the words already taken: a row sent again is not words twice
    /// her words so far as the rows bring them, as she writes them: the line before the answer (commentary), and the answer
    private(set) var leadText = "", answerText = ""
    private var shown: Int?
    private var shownAt = 0.0

    init(utterance: String, at now: Double) { self.utterance = utterance; sentAt = now }

    var phase: Phase {
        if endedAt != nil { return .over }
        if answerAt != nil { return .answering }
        return steps.values.contains { $0.state == .running } ? .working : .sent
    }

    /// the host's `said`: a resent say is answered with the same turn, so this is the same twice
    mutating func bind(turn: String) { if self.turn == nil { self.turn = turn } }

    /// A `step` frame of this turn. A step is told twice, running then ended; a repeat, or a running after its end, changes nothing.
    mutating func step(_ f: StepFrame, at now: Double) {
        if endedAt != nil { return }
        if var s = steps[f.step] {
            guard s.state == .running, f.state != .running else { return }
            s.state = f.state; s.label = f.label; s.endedAt = now; s.endedMs = f.endedMs
            steps[f.step] = s
        } else {
            steps[f.step] = Step(n: f.step, label: f.label, state: f.state, arrivedAt: now, endedAt: f.state == .running ? nil : now, startedMs: f.startedMs, endedMs: f.endedMs)
        }
    }

    /// A row of this turn: her first words (a lead-in, then the answer), and the end.
    mutating func row(_ r: RowFrame, at now: Double) {
        let lead = r.phase == "commentary"
        switch r.eventType {
        case "surface.response_open": if lead, let id = r.payload["response_id"] as? String { waitLines.insert(id) }
        case "surface.response_chunk":
            if lead { leadInAt = leadInAt ?? now } else { answerAt = answerAt ?? now }
            guard let t = r.text, chunks.insert(r.eventUid).inserted else { return }
            if lead { leadText += t } else { answerText += t }
        case "surface.response_emitted": if !lead { answerAt = answerAt ?? now; end(at: now) }
        case "response.cancelled", "response.failed":
            if let id = r.payload["response_id"] as? String, waitLines.contains(id) { return }
            if r.eventType == "response.failed" { failed = true }
            end(at: now)
        default: break
        }
    }

    /// The turn is over; a step still running did not finish.
    mutating func end(at now: Double) {
        if endedAt != nil { return }
        endedAt = now
        for (n, s) in steps where s.state == .running { steps[n]?.state = .failed; steps[n]?.endedAt = now }
    }

    /// What the row says now. The latest step still running is wanted; it replaces what is on show at once, unless that was put on show less than `hold` ago.
    /// Called often (a frame), so a step that came and went between two calls is not shown at all.
    mutating func line(at now: Double) -> StatusLine {
        let want = answerAt != nil ? nil : steps.values.filter { $0.state == .running }.max { $0.n < $1.n }?.n
        if want != shown, shown == nil || now - shownAt >= Self.hold { shown = want; shownAt = now }
        if let n = shown, let s = steps[n] { return StatusLine(label: s.label, since: s.arrivedAt) }
        return StatusLine(label: nil, since: sentAt)
    }

    /// The steps for the line under her answer; nil when no tool ran
    var info: StepsInfo? {
        let all = steps.values.sorted { $0.n < $1.n }
        if all.isEmpty { return nil }
        func ms(_ s: Step) -> Int { s.endedMs.map { Int($0 - s.startedMs) } ?? Int(((s.endedAt ?? s.arrivedAt) - s.arrivedAt) * 1000) }
        let list = all.map { StepRecord(label: $0.label, ms: max(0, ms($0)), failed: $0.state == .failed) }
        // the span of the host's clock, so steps that ran side by side count once
        let ended = all.compactMap(\.endedMs), total = ended.isEmpty ? list.reduce(0) { $0 + $1.ms } : Int((ended.max() ?? 0) - (all.map(\.startedMs).min() ?? 0))
        return StepsInfo(summary: "", totalMs: max(0, total), list: list)
    }
}

/// The turn on show, kept where the talk and (later) her orb can read it. The link feeds it what the host sends; whoever sends a turn tells it.
@MainActor @Observable
final class StatusBoard {
    static let shared = StatusBoard()

    private(set) var turn: TurnStatus?
    @ObservationIgnored var clock: () -> Double = { Double(uptimeNs()) / 1e9 }
    /// frames of a turn that arrived before the host's `said` bound it (the pump can beat the awaiting caller)
    @ObservationIgnored private var orphans: [(at: Double, event: LinkEvent)] = []
    private static let orphanKeep = 5.0, orphanMax = 32

    /// A turn is being sent: the one on show goes. Sending the same words again (a resend) is the same turn.
    func begin(utterance: String) {
        if let t = turn, t.utterance == utterance, t.phase != .over { return }
        turn = TurnStatus(utterance: utterance, at: clock())
    }

    /// The host took the words as this turn.
    func bind(utterance: String, turn id: String) {
        guard turn?.utterance == utterance else { return }
        turn?.bind(turn: id)
        let held = orphans
        orphans = []
        for o in held { feed(o.event, at: o.at) }
    }

    func feed(_ e: LinkEvent, at: Double? = nil) {
        let now = at ?? clock()
        let id: String?
        switch e {
        case .step(let f): id = f.turnId
        case .row(let r): id = r.turnId
        default: return
        }
        guard let id else { return }
        guard turn?.turn == id else {
            orphans.removeAll { now - $0.at > Self.orphanKeep }
            orphans.append((now, e))
            if orphans.count > Self.orphanMax { orphans.removeFirst() }
            return
        }
        switch e {
        case .step(let f): turn?.step(f, at: now)
        case .row(let r): turn?.row(r, at: now)
        default: break
        }
    }

    /// The turn is over (answered or not): a step still running did not finish. Only the turn of `utterance` when one is named.
    func end(utterance: String? = nil) {
        guard utterance == nil || turn?.utterance == utterance else { return }
        turn?.end(at: clock())
    }

    /// what the status row says now; nil when there is no turn
    func line() -> StatusLine? { turn?.line(at: clock()) }

    /// the steps of the turn on show, for the line under her answer
    var info: StepsInfo? { turn?.info }

    /// Her words the line has brought so far for the turn sent as `utterance` (a lead-in line, then the answer); nil for another turn.
    func words(utterance: String) -> (lead: String, answer: String)? {
        guard let t = turn, t.utterance == utterance else { return nil }
        return (t.leadText, t.answerText)
    }

    /// The host has ended the turn sent as `utterance`: its record is in the conversation, or about to be.
    func over(utterance: String) -> Bool { turn?.utterance == utterance && turn?.phase == .over }

    /// The host failed the turn sent as `utterance`.
    func failed(utterance: String) -> Bool { turn?.utterance == utterance && turn?.failed == true }
}
