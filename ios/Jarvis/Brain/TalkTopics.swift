import Foundation

// What a talk was about: the first words he said in it, read from the day's records (`GET /inherent/memory/day/{day}/records`, jarvis/state/memory_db.py).
// A talk star on the dial is named by them, and its opened row shows the first exchange.

/// One record of a day's memory: `who` is "user" for his words, anything else is hers.
struct RecordRow: Equatable {
    let ts: String                      // local time as the brain wrote it, `2026-10-09T18:12:03`
    let who: String
    let text: String

    /// the records of an answer of `GET /inherent/memory/day/{day}/records` (oldest first), or nil when it is not one
    static func parse(_ data: Data) -> [RecordRow]? {
        guard let o = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any], let rows = o["records"] as? [[String: Any]] else { return nil }
        return rows.compactMap { r in
            guard let ts = r["ts"] as? String, let who = r["who"] as? String, let text = r["text"] as? String else { return nil }
            return RecordRow(ts: ts, who: who, text: text)
        }
    }
}

extension BrainClient {
    /// `GET /inherent/memory/day/{day}/records?limit=300`: the day's records, text cut at 600 characters.
    static func records(day: String, bases: [URL], token: String) async throws -> [RecordRow] {
        let r = try await send("GET", "/inherent/memory/day/\(day)/records", query: [URLQueryItem(name: "limit", value: "300")], bases: bases, token: token, unauthorized: .revoked)
        guard let rows = RecordRow.parse(r.data) else { throw BrainError.garbled }
        return rows
    }
}

enum TalkTopic {
    /// his first words with a leading wake word ("jarvis", "贾维斯", after "hey" or "hi" too) and the punctuation and spaces after it taken off
    static func clean(_ text: String) -> String {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let r = t.replacingOccurrences(of: #"^(?:(?:hey|hi)[\s,，.。!！]*)?(?:jarvis|贾维斯)[\s\p{P}]*"#, with: "", options: [.regularExpression, .caseInsensitive])
        return r.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// The first exchange of a talk that started at `start` (ms) and ended at `end`: his first utterance is the first of his records within 2 s of the start,
    /// else the first of his inside the range. Hers is the first record after it that is not his. Times are compared in `tz`, the zone the brain wrote them in.
    static func first(_ records: [RecordRow], start: Int64, end: Int64?, tz: TimeZone) -> (his: String, hers: String)? {
        var cal = Calendar(identifier: .gregorian); cal.timeZone = tz
        func ms(_ r: RecordRow) -> Int64? { TalkStream.time(r.ts.replacingOccurrences(of: " ", with: "T"), cal).map { Int64($0.timeIntervalSince1970 * 1000) } }
        let hi = end ?? start + 600_000
        let mine = records.enumerated().compactMap { i, r -> (Int, Int64)? in r.who == "user" ? ms(r).map { (i, $0) } : nil }
        guard let at = mine.first(where: { abs($0.1 - start) <= 2000 }) ?? mine.first(where: { $0.1 >= start && $0.1 <= hi }) else { return nil }
        let his = clean(records[at.0].text)
        if his.isEmpty { return nil }
        let hers = records[(at.0 + 1)...].first { $0.who != "user" }.map { AnswerText.visible($0.text) } ?? ""
        return (his, hers)
    }
}

/// The topics of the talks the brain served, by the talk item's id. A day's records are asked for again when that day's talk items changed (their number or the last end).
final class TalkTopics {
    static let shared = TalkTopics()
    /// bumps when a topic arrived, so the engine rebuilds its stars
    private(set) var version = 0
    private var firsts: [String: (his: String, hers: String)] = [:]
    private var seen: [String: String] = [:]
    private var inFlight: Set<String> = []
    /// the day's records (`YYYY-MM-DD`), or nil when the brain did not give them; a test hands in its own
    var fetch: (String) async -> [RecordRow]? = { day in
        try? await BrainLink.shared.call { try await BrainClient.records(day: day, bases: $0, token: $1) }
    }

    func topic(_ id: String) -> String? { firsts[id]?.his }
    func exchange(_ id: String) -> (his: String, hers: String)? { firsts[id] }

    #if DEBUG
    func debugSet(_ id: String, _ his: String, _ hers: String) { firsts[id] = (his, hers); version += 1 }
    #endif

    /// Ask for the records of every served day that has talks and has not been read for them as they are now.
    @MainActor func ensure(_ days: [String: DayReply]) async {
        for (date, reply) in days {
            let talks = reply.items.filter { $0.kind == "talk" }
            if talks.isEmpty { continue }
            let sig = "\(talks.count)|\(talks.map { $0.end_ms ?? $0.start_ms }.max() ?? 0)"
            if seen[date] == sig || inFlight.contains(date) { continue }
            inFlight.insert(date)
            let records = await fetch(date)
            inFlight.remove(date)
            guard let records else { continue }
            seen[date] = sig
            let tz = TimeZone(identifier: reply.tz) ?? .current
            var changed = false
            for t in talks {
                guard let x = TalkTopic.first(records, start: t.start_ms, end: t.end_ms, tz: tz) else { continue }
                if firsts[t.id]?.his != x.his || firsts[t.id]?.hers != x.hers { firsts[t.id] = x; changed = true }
            }
            if changed { version += 1 }
        }
    }
}
