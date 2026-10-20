import Foundation

// What he can do to a row of the day on the brain (ADR 0296, jarvis/surface/inherent_server.py): mark an item as what mattered (`POST /inherent/day/highlight`), say that an event his mail
// made is his or is not (`POST /inherent/commitments/{hex}`), keep a place he was at under a name of his (`POST /inherent/memory/places`), say what he was doing in a stretch of the
// day (`POST /inherent/day/answer`, ADR 0302) and have the day tidied (`POST /inherent/day/tidy`, ADR 0310). Each answers by its status. A 404 that
// is the route's own (no such item, no such commitment) is told from the framework's, which a brain without the route gives (`{"detail": "Not Found"}`): that one is no answer.

/// the brain's answer to his mark on an item of a day
enum HighlightAnswer: Equatable {
    case done([Highlight]?)         // taken: the day's highlights as they stand now (none when the answer could not be read, which leaves the phone's own as they are)
    case full                       // 409: five of his marks stand already
    case gone                       // 404: no item has that id on that day
    case failed                     // 422, a brain without the route, or no answer at all

    static func read(status: Int, data: Data) -> HighlightAnswer {
        switch status {
        case 200...299: return .done(((try? JSONDecoder().decode(Marks.self, from: data))?.highlights)?.compactMap(\.value))
        case 404: return BrainRoute.missing(data) ? .failed : .gone
        case 409: return .full
        default: return .failed
        }
    }
    private struct Marks: Decodable { let highlights: [Lossy<Highlight>]? }
}

/// what he says of a commitment
enum CommitAction: String { case confirm, cancel }

/// the brain's answer to what he said of a commitment
enum CommitAnswer: Equatable {
    case done                       // taken
    case already                    // 409: it is confirmed already
    case gone                       // 404: it is not there, or was dropped
    case failed                     // 422, a brain without the route, or no answer at all

    static func read(status: Int, data: Data) -> CommitAnswer {
        switch status {
        case 200...299: return .done
        case 404: return BrainRoute.missing(data) ? .failed : .gone
        case 409: return .already
        default: return .failed
        }
    }
}

/// the brain's answer to a place he named
enum PlaceAdd: Equatable {
    case done(KeptPlace?, change: String?)      // kept: the place as the brain has it (none when the answer could not be read) and the change that holds it (none when it was kept already)
    case refused(String)                        // not kept, and why, in words for him to read

    static func read(status: Int, data: Data) -> PlaceAdd {
        switch status {
        case 200...299:
            let o = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            let p = o?["place"] as? [String: Any]
            let place = (p?["id"] as? String).flatMap { id in (p?["name"] as? String).map { KeptPlace(id: id, name: $0, role: p?["role"] as? String) } }
            return .done(place, change: o?["change"] as? String)
        case 400, 409: return .refused("这个名字或这个地方已经记过了")
        case 404: return .refused(BrainRoute.missing(data) ? BrainError.notOpen.message : BrainError.refused.message)
        case 422: return .refused("这个名字用不了")
        default: return .refused(BrainError.refused.message)
        }
    }
}

/// the brain's answer to what he said he was doing in a stretch
enum SpanAnswer: Equatable {
    case done                       // 200: the brain wrote it as a span of the doing lane; the days are asked for again, and its reply (not the phone) says how it stands
    case failed                     // 422 (a time out of the day, a stretch no span touches, words empty or too long), 502 (it could not be written), a brain without the route, or no answer at all

    static func read(status: Int, data: Data) -> SpanAnswer { (200...299).contains(status) ? .done : .failed }
}

/// the brain's answer to 理一下 (`POST /inherent/day/tidy`, ADR 0310): a model writes today's doing lane afresh while the request waits, and the brain answers `{ok, written, outcome}`
enum TidyAnswer: Equatable {
    case written                    // ok and written: today has a new run, to be asked for again
    case unchanged                  // ok and not written: there was nothing in it to change
    case failed                     // not ok, an error status, a brain without the route, no answer in 90 s, or none at all

    /// The brain says `ok` whenever the writer ran, and in `outcome` how it went: `landed (...)` is a run written; `unchanged`, `waiting`, `no waking window` and `no inputs` are nothing to change; the writer's
    /// own `failed` and `rejected (...)` (a day that did not pass its gates) are a day not tidied, which the button says as a failure.
    static func read(status: Int, data: Data) -> TidyAnswer {
        guard (200...299).contains(status), let o = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any], o["ok"] as? Bool == true else { return .failed }
        if o["written"] as? Bool == true { return .written }
        let outcome = o["outcome"] as? String ?? ""
        return outcome == "failed" || outcome.hasPrefix("rejected") ? .failed : .unchanged
    }
}

enum BrainRoute {
    /// A 404 that says `Not Found` and nothing else is the framework's: this brain has no such route. A route's own 404 says what it did not find.
    static func missing(_ body: Data) -> Bool {
        ((try? JSONSerialization.jsonObject(with: body)) as? [String: Any])?["detail"] as? String == "Not Found"
    }
}

extension BrainClient {
    /// His mark on one item of a day (`day` is `YYYY-MM-DD`, `id` the item's, whole), or taking it off. No label: the item keeps the name the brain gives it.
    static func markItem(day: String, id: String, on: Bool, bases: [URL], token: String) async throws -> HighlightAnswer {
        let body = try JSONSerialization.data(withJSONObject: ["day": day, "id": id, "on": on])
        let r = try await send("POST", "/inherent/day/highlight", body: body, also: [404, 409, 422], bases: bases, token: token, unauthorized: .revoked)
        return HighlightAnswer.read(status: r.status, data: r.data)
    }

    /// He says the commitment is his (`confirm`) or is not (`cancel`). `hex` is the id between `commitment-` and `@` of the event's id.
    static func actOnCommitment(hex: String, action: CommitAction, bases: [URL], token: String) async throws -> CommitAnswer {
        let body = try JSONSerialization.data(withJSONObject: ["action": action.rawValue])
        let r = try await send("POST", "/inherent/commitments/" + hex, body: body, also: [404, 409, 422], bases: bases, token: token, unauthorized: .revoked)
        return CommitAnswer.read(status: r.status, data: r.data)
    }

    /// What he was doing from `startMs` to `endMs` (epoch milliseconds inside `day`, `YYYY-MM-DD`): his words, 1 to 200 characters, as they are to stand.
    static func stateSpan(day: String, startMs: Int64, endMs: Int64, text: String, bases: [URL], token: String) async throws -> SpanAnswer {
        let body = try JSONSerialization.data(withJSONObject: ["day": day, "start_ms": startMs, "end_ms": endMs, "text": text])
        let r = try await send("POST", "/inherent/day/answer", body: body, also: [422, 502], bases: bases, token: token, unauthorized: .revoked)
        return SpanAnswer.read(status: r.status, data: r.data)
    }

    /// Have the brain tidy today's doing lane (no body). It answers when the model is done, so this one waits up to 90 s.
    static func tidyDay(bases: [URL], token: String) async throws -> TidyAnswer {
        let r = try await send("POST", "/inherent/day/tidy", patient: true, also: [422, 502], bases: bases, token: token, unauthorized: .revoked)
        return TidyAnswer.read(status: r.status, data: r.data)
    }

    /// Keep the point (`lat`, `lng`) of a stay under his name for it; `role` is `home`, `school` or `other`.
    static func addPlace(name: String, lat: Double, lng: Double, role: String, bases: [URL], token: String) async throws -> PlaceAdd {
        let body = try JSONSerialization.data(withJSONObject: ["name": name, "lat": lat, "lng": lng, "role": role])
        let r = try await send("POST", "/inherent/memory/places", body: body, also: [400, 404, 409, 422], bases: bases, token: token, unauthorized: .revoked)
        return PlaceAdd.read(status: r.status, data: r.data)
    }
}
