// words: legacy
import Foundation

// A place he keeps, under another name (ADR 0234): `POST /inherent/memory/places/{id}` with `{name}`, and taking that change back, `POST /inherent/memory/places/undo` with `{change_id}`.
// The brain answers 200 with `{ok, place, change}` (`change` is none when the name was the same), 400 when the name is taken or not a name, 404 when there is no such place or change,
// 409 when a later change touched the same place, 422 when the body is not the shape.

/// what the brain said to a change of a place he keeps
enum PlaceChange: Equatable {
    case done(change: String?)      // taken: the change that holds it (to take it back), none when nothing changed
    case refused(String)            // not taken, and why, in words for him to read

    /// the answer by its status: `undo` says whether it was to a rename or to taking one back
    static func read(status: Int, data: Data, undo: Bool) -> PlaceChange {
        switch status {
        case 200...299: return .done(change: ((try? JSONSerialization.jsonObject(with: data)) as? [String: Any])?["change"] as? String)
        case 404: return .refused(undo ? "找不到那次改动" : "这个地方已经不在了")
        case 409: return .refused("那之后又改过这个地方，撤销不了")
        case 400, 422: return .refused(undo ? "撤销不了" : "这个名字用不了，可能已经有别的地方叫它")
        default: return .refused(BrainError.refused.message)
        }
    }
}

extension BrainClient {
    /// Rename a place he keeps. The id is the one the brain gave it (`kept_place.id` of a stay).
    static func renamePlace(id: String, name: String, bases: [URL], token: String) async throws -> PlaceChange {
        let body = try JSONSerialization.data(withJSONObject: ["name": name])
        let r = try await send("POST", "/inherent/memory/places/" + id, body: body, also: [400, 404, 409, 422], bases: bases, token: token, unauthorized: .revoked)
        return PlaceChange.read(status: r.status, data: r.data, undo: false)
    }

    /// Take a rename back, by the change it made.
    static func undoPlaceChange(_ change: String, bases: [URL], token: String) async throws -> PlaceChange {
        let body = try JSONSerialization.data(withJSONObject: ["change_id": change])
        let r = try await send("POST", "/inherent/memory/places/undo", body: body, also: [400, 404, 409, 422], bases: bases, token: token, unauthorized: .revoked)
        return PlaceChange.read(status: r.status, data: r.data, undo: true)
    }
}
