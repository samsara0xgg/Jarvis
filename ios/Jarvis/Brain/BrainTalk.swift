import Foundation
import Observation

// Talking to the brain from the phone: pictures and files up (`POST /inherent/attachments`), words with them (`POST /inherent/submit`),
// the conversation back (`GET /inherent/conversation`). The shapes are docs/spec.html (ADR 0211) and jarvis/surface/inherent_server.py.

/// One row of the conversation of record: `source` is "allen" for his words and anything else (the brain's own, mail she read to him) for hers.
struct ConversationRow: Equatable {
    let seq: Int
    let source: String
    let ts: String                     // local time as the brain wrote it, `2026-10-09T23:52:02`
    let text: String                   // with the voice and document markup: `AnswerText` reads it
    var steps: StepsInfo?              // what she did to answer, from the brain (ADR 0228); nil when no tool ran
    var isHis: Bool { source == "allen" }
    /// a line she says before the answer comes (only a voice part): more is on its way
    var isLeadIn: Bool { !isHis && AnswerText.isLeadIn(text) }

    /// the rows of an answer of `GET /inherent/conversation`, oldest first, or nil when it is not one
    static func parse(_ data: Data) -> [ConversationRow]? {
        guard let o = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any], let rows = o["rows"] as? [[String: Any]] else { return nil }
        return rows.compactMap { r in
            guard let seq = r["seq"] as? Int, let source = r["source"] as? String, let text = r["text"] as? String else { return nil }
            return ConversationRow(seq: seq, source: source, ts: (r["ts"] as? String) ?? "", text: text, steps: StepsInfo.parse(r))
        }
    }
}

/// One page of `GET /inherent/conversation`: its rows oldest first, and whether older rows exist beyond the first (below the history floor too, ADR 0215).
struct ConversationPage: Equatable {
    var rows: [ConversationRow]
    var hasMore: Bool

    static func parse(_ data: Data) -> ConversationPage? {
        guard let rows = ConversationRow.parse(data), let o = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return nil }
        return ConversationPage(rows: rows, hasMore: o["has_more"] as? Bool ?? false)
    }
}

/// What the panel holds of the conversation as pages come in: the newest page read again keeps the older rows he has already scrolled to, and an older page goes in front.
enum ConversationPaging {
    /// The newest page replaces the newest rows. The rows older than it stay when it reaches them, else (more than a page arrived since) they would leave a hole and go.
    static func merge(held: [ConversationRow], hasMore: Bool, newest: ConversationPage) -> (rows: [ConversationRow], hasMore: Bool) {
        guard let first = newest.rows.first?.seq else { return ([], newest.hasMore) }
        guard let last = held.last?.seq, last >= first - 1 else { return (newest.rows, newest.hasMore) }
        let older = held.filter { $0.seq < first }
        return (older + newest.rows, older.isEmpty ? newest.hasMore : hasMore)
    }

    /// An older page in front of what is held. A page that brings nothing ends it, so the panel does not ask again.
    static func prepend(held: [ConversationRow], older: ConversationPage) -> (rows: [ConversationRow], hasMore: Bool) {
        let rows = older.rows.filter { $0.seq < (held.first?.seq ?? .max) }
        return (rows + held, !rows.isEmpty && older.hasMore)
    }
}

extension BrainClient {
    /// One file up. `id` is the brain's name for it; without one, `status` is why the brain refused it (413, 415, 422, 501, 507, ...).
    static func upload(_ part: AttachUpload.Part, bases: [URL], token: String) async throws -> (id: String?, status: Int) {
        let boundary = "jarvis-" + UUID().uuidString
        let r = try await send("POST", "/inherent/attachments", body: AttachUpload.multipart(part, boundary: boundary), contentType: "multipart/form-data; boundary=\(boundary)", slow: true,
                               also: [411, 413, 415, 422, 501, 507], bases: bases, token: token, unauthorized: .revoked)
        guard r.status == 200 else { return (nil, r.status) }
        guard let o = try? JSONSerialization.jsonObject(with: r.data) as? [String: Any], let id = o["id"] as? String else { throw BrainError.garbled }
        return (id, 200)
    }

    /// The body of `POST /inherent/submit`: the words, the files, and the item he has open (ADR 0214) while its chip shows.
    static func submitBody(_ text: String, attachments: [String], about: About?) throws -> Data {
        var o: [String: Any] = ["text": text, "attachments": attachments]
        if let about { o["about"] = about.json }
        return try JSONSerialization.data(withJSONObject: o)
    }

    /// The words of a turn and the ids of the files that go with them (the text may be empty then). 200 it was taken; 404 a file is gone, 422 over the turn's limits or the `about` refused, 501 no files here.
    static func submit(_ text: String, attachments: [String], about: About? = nil, bases: [URL], token: String) async throws -> Int {
        let body = try submitBody(text, attachments: attachments, about: about)
        return try await send("POST", "/inherent/submit", body: body, also: [404, 422, 501], bases: bases, token: token, unauthorized: .revoked).status
    }

    static func conversationQuery(after: Int, before: Int, limit: Int) -> [URLQueryItem] {
        [URLQueryItem(name: "limit", value: String(limit)), before > 0 ? URLQueryItem(name: "before", value: String(before)) : URLQueryItem(name: "after", value: String(after))]
    }

    /// `GET /inherent/conversation`: with neither cursor the newest `limit` rows, with `after` the rows past that `seq`, with `before` the newest rows older than it (never both: the brain refuses that).
    static func conversation(after: Int = 0, before: Int = 0, limit: Int, bases: [URL], token: String) async throws -> ConversationPage {
        let r = try await send("GET", "/inherent/conversation", query: conversationQuery(after: after, before: before, limit: limit), bases: bases, token: token, unauthorized: .revoked)
        guard let page = ConversationPage.parse(r.data) else { throw BrainError.garbled }
        return page
    }
}

/// The history in the talk panel: the newest rows of the conversation, read when the panel opens and after each answer, and the older pages he has scrolled back to.
@Observable
final class Conversation {
    static let shared = Conversation()
    private(set) var rows: [ConversationRow] = []
    /// older rows exist than the first one held
    private(set) var hasMore = false
    private(set) var loadingOlder = false
    /// bumps when `rows` changed
    private(set) var version = 0

    @MainActor func refresh() async {
        guard let got = try? await BrainLink.shared.call({ try await BrainClient.conversation(limit: 200, bases: $0, token: $1) }) else { return }
        let m = ConversationPaging.merge(held: rows, hasMore: hasMore, newest: got)
        guard m.rows != rows || m.hasMore != hasMore else { return }
        rows = m.rows; hasMore = m.hasMore; version += 1
    }

    /// The page before the oldest row held, when he has scrolled to the top. True when rows came in.
    @MainActor func loadOlder() async -> Bool {
        guard hasMore, !loadingOlder, let first = rows.first?.seq else { return false }
        loadingOlder = true
        defer { loadingOlder = false }
        #if DEBUG
        var page = fakeOlder?(first)
        #else
        var page: ConversationPage?
        #endif
        if page == nil { page = try? await BrainLink.shared.call({ try await BrainClient.conversation(before: first, limit: 200, bases: $0, token: $1) }) }
        guard let got = page else { return false }
        let m = ConversationPaging.prepend(held: rows, older: got)
        let added = m.rows.count > rows.count
        rows = m.rows; hasMore = m.hasMore; version += 1
        return added
    }

    #if DEBUG
    /// `-scene history` and the like: made-up rows, as if the brain had served them, and the made-up page before them
    var fakeOlder: ((Int) -> ConversationPage)?
    @MainActor func debugSet(_ r: [ConversationRow], hasMore more: Bool = false) { rows = r; hasMore = more; version += 1 }
    #endif

    /// the newest row's number, or nil when the brain keeps no conversation: what an answer is waited for after
    @MainActor func latestSeq() async -> Int? {
        (try? await BrainLink.shared.call { try await BrainClient.conversation(limit: 1, bases: $0, token: $1) }).map { $0.rows.last?.seq ?? 0 }
    }

    /// the rows past `seq`, or nothing when the brain does not answer right now
    @MainActor func rows(after seq: Int) async -> [ConversationRow] {
        (try? await BrainLink.shared.call { try await BrainClient.conversation(after: seq, limit: 20, bases: $0, token: $1) })?.rows ?? []
    }
}

/// The answer of `POST /inherent/ask` (docs/spec.html): `spoken` is what she says, `written` the part she leaves to the screen (empty when there is none).
enum AskReply: Equatable {
    case answered(spoken: String, written: String)
    case working            // 504: no final answer in 25 s; the turn goes on and is in the conversation
    case failed             // 502: the turn failed or was cancelled

    /// The request body, or nil when the brain would refuse it: no words, more than 2000 characters, or a body over 4 KiB (413).
    static func body(_ text: String) -> Data? {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty, t.count <= 2000, let d = try? JSONSerialization.data(withJSONObject: ["text": t]), d.count <= 4096 else { return nil }
        return d
    }

    static func parse(status: Int, data: Data) -> AskReply? {
        switch status {
        case 200:
            guard let o = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any], let spoken = o["spoken"] as? String else { return nil }
            return .answered(spoken: spoken, written: (o["written"] as? String) ?? "")
        case 504: return .working
        case 502: return .failed
        default: return nil
        }
    }
}

extension BrainClient {
    /// `POST /inherent/ask`: a question and the answer to it, waiting up to 25 s on the brain's side.
    static func ask(_ body: Data, bases: [URL], token: String) async throws -> AskReply {
        let r = try await send("POST", "/inherent/ask", body: body, slow: true, also: [413, 502, 504], bases: bases, token: token, unauthorized: .revoked)
        guard let reply = AskReply.parse(status: r.status, data: r.data) else { throw BrainError.refused }
        return reply
    }
}
