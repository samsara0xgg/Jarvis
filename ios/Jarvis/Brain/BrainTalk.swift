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
    var isHis: Bool { source == "allen" }
    /// a line she says before the answer comes (only a voice part): more is on its way
    var isLeadIn: Bool { !isHis && AnswerText.isLeadIn(text) }

    /// the rows of an answer of `GET /inherent/conversation`, oldest first, or nil when it is not one
    static func parse(_ data: Data) -> [ConversationRow]? {
        guard let o = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any], let rows = o["rows"] as? [[String: Any]] else { return nil }
        return rows.compactMap { r in
            guard let seq = r["seq"] as? Int, let source = r["source"] as? String, let text = r["text"] as? String else { return nil }
            return ConversationRow(seq: seq, source: source, ts: (r["ts"] as? String) ?? "", text: text)
        }
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

    /// The words of a turn and the ids of the files that go with them (the text may be empty then). 200 it was taken; 404 a file is gone, 422 over the turn's limits, 501 no files here.
    static func submit(_ text: String, attachments: [String], bases: [URL], token: String) async throws -> Int {
        let body = try JSONSerialization.data(withJSONObject: ["text": text, "attachments": attachments] as [String: Any])
        return try await send("POST", "/inherent/submit", body: body, also: [404, 422, 501], bases: bases, token: token, unauthorized: .revoked).status
    }

    /// `GET /inherent/conversation`: with `after` 0 the newest `limit` rows, else the rows past that `seq`.
    static func conversation(after: Int, limit: Int, bases: [URL], token: String) async throws -> [ConversationRow] {
        let q = [URLQueryItem(name: "after", value: String(after)), URLQueryItem(name: "limit", value: String(limit))]
        let r = try await send("GET", "/inherent/conversation", query: q, bases: bases, token: token, unauthorized: .revoked)
        guard let rows = ConversationRow.parse(r.data) else { throw BrainError.garbled }
        return rows
    }
}

/// The history in the drawer: the newest rows of the conversation, read when the drawer opens and after each answer.
@Observable
final class Conversation {
    static let shared = Conversation()
    private(set) var rows: [ConversationRow] = []
    /// bumps when `rows` changed
    private(set) var version = 0

    @MainActor func refresh() async {
        guard let got = try? await BrainLink.shared.call({ try await BrainClient.conversation(after: 0, limit: 40, bases: $0, token: $1) }), got != rows else { return }
        rows = got; version += 1
    }

    /// the newest row's number, or nil when the brain keeps no conversation: what an answer is waited for after
    @MainActor func latestSeq() async -> Int? {
        (try? await BrainLink.shared.call { try await BrainClient.conversation(after: 0, limit: 1, bases: $0, token: $1) }).map { $0.last?.seq ?? 0 }
    }

    /// the rows past `seq`, or nothing when the brain does not answer right now
    @MainActor func rows(after seq: Int) async -> [ConversationRow] {
        (try? await BrainLink.shared.call { try await BrainClient.conversation(after: seq, limit: 20, bases: $0, token: $1) }) ?? []
    }
}
