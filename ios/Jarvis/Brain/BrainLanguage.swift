import Foundation

// Its own file: BrainClient.swift is compiled into the share extension, which has no words table.
extension BrainClient {
    /// `GET /inherent/language`: the language the brain's settings name.
    static func language(bases: [URL], token: String) async throws -> Words.Lang {
        let r = try await send("GET", "/inherent/language", bases: bases, token: token, unauthorized: .revoked)
        guard let l = Words.Lang(reply: r.data) else { throw BrainError.garbled }
        return l
    }
}
