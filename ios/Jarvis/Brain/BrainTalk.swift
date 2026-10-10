import Foundation

// Talking to the brain from the phone: pictures and files up (`POST /inherent/attachments`), words with them (`POST /inherent/submit`).
// The shapes are docs/spec.html (ADR 0211) and jarvis/surface/inherent_server.py.

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
}
