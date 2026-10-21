// words: legacy
import Foundation
import UserNotifications

// The bus ride's two pushes (docs/spec.html, `POST /inherent/ride`; jarvis/runtime/rides.py): the question "did you board?" (category `ride_board`) and the warning that his stop is next
// (category `ride_alight`), both thread-id `ride`, with the ids in `userInfo["ride"]`. Each is a button pair on the notification in the background and a waiting card (Brain/Waiting) while the app is open.

/// what a ride push carries: the question or warning in the brain's words, and the id its answer needs
struct RidePush: Equatable {
    enum Kind: String, CaseIterable { case board, alight }
    let kind: Kind
    let id: String                     // the candidate_id of the question, or the ride_id of the ride
    let title: String
    let line: String

    init?(_ content: UNNotificationContent) {
        guard let o = content.userInfo["ride"] as? [String: Any], let kind = (o["kind"] as? String).flatMap(Kind.init),
              let id = o[kind == .board ? "candidate_id" : "ride_id"] as? String, !id.isEmpty else { return nil }
        self.kind = kind; self.id = id
        // the question is the alert's title (the body says where the bus is); a push with only a body is read the other way
        title = content.title.isEmpty ? content.body : content.title
        line = content.title.isEmpty ? "" : content.body
    }

    var card: WaitingCard {
        WaitingCard(id: "ride-\(id)", kind: .ride, brainId: id, short: kind == .board ? "上车" : "下车", title: title, meta: line, body: "",
                    actions: RideAction.allCases.filter { $0.category == kind }.map { CardAction(label: $0.title, answer: $0.answer) })
    }
}

/// The buttons on a ride notification, each the answer its card button makes.
enum RideAction: String, CaseIterable {
    case not = "jarvis.ride.not", board = "jarvis.ride.board", end = "jarvis.ride.end"

    var category: RidePush.Kind { self == .end ? .alight : .board }
    var answer: CardAnswer { .ride(self == .not ? "not" : self == .board ? "board" : "end") }
    var title: String {
        switch self {
        case .not: "不是"
        case .board: "上车了"
        case .end: "下车了"
        }
    }

    static func categories() -> Set<UNNotificationCategory> {
        Set(RidePush.Kind.allCases.map { k in
            UNNotificationCategory(identifier: k == .board ? "ride_board" : "ride_alight",
                                   actions: allCases.filter { $0.category == k }.map { UNNotificationAction(identifier: $0.rawValue, title: $0.title, options: []) },
                                   intentIdentifiers: [])
        })
    }

    /// Answers in the background, by the ids in the push. Returns the Chinese line to tell him when it did not go through; nil when it was sent or the brain no longer has the question.
    @MainActor func perform(_ push: RidePush) async -> String? {
        guard let req = answer.request(for: push.card) else { return nil }
        do {
            let status = try await BrainLink.shared.call { try await BrainClient.answer(req.path, req.body, bases: $0, token: $1) }
            return status == 400 || status == 422 ? "没发出去：" + BrainError.refused.message : nil
        } catch let e as BrainError {
            return "没发出去：" + e.message
        } catch {
            return "没发出去：" + BrainError.refused.message
        }
    }
}

extension BrainClient {
    /// `GET /inherent/ride`: the raw answer, `{"ride": null | {ride_id, ...}}`.
    static func ride(bases: [URL], token: String) async throws -> Data {
        try await send("GET", "/inherent/ride", bases: bases, token: token, unauthorized: .revoked).data
    }
}
