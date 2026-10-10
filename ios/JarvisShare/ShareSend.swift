import Foundation

extension BrainClient {
    /// `POST /inherent/share`: 200 the brain took it (`saved`, or `accepted` as a turn); 400, 413 and 422 come back as their status. A turn can take a while to be accepted.
    static func share(_ body: [String: Any], bases: [URL], token: String) async throws -> Int {
        let data = try JSONSerialization.data(withJSONObject: body)
        return try await send("POST", "/inherent/share", body: data, slow: true, also: [400, 413, 422], bases: bases, token: token, unauthorized: .revoked).status
    }
}
