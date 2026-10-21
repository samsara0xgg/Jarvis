// words: legacy
import Foundation
import UIKit
import UserNotifications

// What the brain pushes (docs/spec.html, the three shapes; jarvis/surface/apns.py `alert_payload`): an alert with an optional `url` beside `aps`, and the things that wait on him,
// which are alerts with thread-id `waiting` and category `confirmation`, `question` or `claude`. Beside `aps` they carry `{"card": {"kind", "id"}}` (ADR 0232), the card the push is about. The Live Activity's content is handled by the system (JarvisActivity).

enum PushShape {
    static let waitingThread = "waiting"
    static let rideThread = "ride"
    static let talkThread = "talk"

    /// the categories of what waits on him
    enum Waiting: String, CaseIterable {
        case confirmation, question, claude
        var cardKind: WaitingCard.Kind {
            switch self {
            case .confirmation: .confirmation
            case .question: .clarification
            case .claude: .claude
            }
        }
    }

    /// what a tap opens, when the push names one (an Uber link, say): a link with a scheme, up to the 1024 characters the brain allows
    static func url(_ userInfo: [AnyHashable: Any]) -> URL? {
        guard let s = userInfo["url"] as? String, s.count <= 1024, let u = URL(string: s), let scheme = u.scheme, !scheme.isEmpty else { return nil }
        return u
    }

    /// The waiting card a push names (`{"card": {"kind", "id"}}` beside `aps`), as `WaitingCard.id` writes it; nil for a push without one, with a kind the app does not know, or without an id.
    static func cardId(_ userInfo: [AnyHashable: Any]) -> String? {
        guard let o = userInfo["card"] as? [String: Any], let kind = (o["kind"] as? String).flatMap(Waiting.init), let id = o["id"] as? String, !id.isEmpty else { return nil }
        return "\(kind.cardKind.rawValue)-\(id)"
    }
}

/// The buttons on a waiting notification, each an answer the app already makes on a card.
enum PushAction: String, CaseIterable {
    case confirm = "jarvis.confirm", cancel = "jarvis.cancel", allow = "jarvis.allow", deny = "jarvis.deny"

    var category: PushShape.Waiting { self == .confirm || self == .cancel ? .confirmation : .claude }
    var answer: CardAnswer {
        switch self {
        case .confirm: .decide(accept: true)
        case .cancel: .decide(accept: false)
        case .allow: .claude("allow")
        case .deny: .claude("deny")
        }
    }
    var title: String {
        switch self {
        case .confirm: "确认"
        case .cancel: "取消"
        case .allow: "允许"
        case .deny: "拒绝"
        }
    }
    /// saying yes needs the phone unlocked, so one picked up from a table cannot approve a command
    var needsUnlock: Bool { self == .confirm || self == .allow }

    static func categories() -> Set<UNNotificationCategory> {
        Set(PushShape.Waiting.allCases.map { w in
            let actions = allCases.filter { $0.category == w }.map { UNNotificationAction(identifier: $0.rawValue, title: $0.title, options: $0.needsUnlock ? [.authenticationRequired] : []) }
            return UNNotificationCategory(identifier: w.rawValue, actions: actions, intentIdentifiers: [])
        })
    }
}

enum PushAnswer {
    enum Target: Equatable { case gone, ambiguous, card(WaitingCard) }

    /// The card a button answers: the one the push names. A push without a card id (sent before ADR 0232) is matched by what it says and answered only when it is the one: a Claude request
    /// whose "tool · folder" is the notification's text and no other waiting request says the same; the one confirmation or question that waits. Nothing waiting any more is `.gone`; anything unclear is `.ambiguous`.
    static func target(_ action: PushAction, body: String, cardId: String? = nil, in cards: [WaitingCard]) -> Target {
        var mine = cards.filter { $0.kind == action.category.cardKind }
        if let cardId { mine = mine.filter { $0.id == cardId } }
        else if action.category == .claude { mine = mine.filter { $0.pushLine == body } }
        guard let one = mine.first else { return .gone }
        guard mine.count == 1, one.actions.contains(where: { $0.answer == action.answer }) else { return .ambiguous }
        return .card(one)
    }

    /// Answers in the background. Returns the Chinese line to tell him when it did not go through; nil when it was sent or there was nothing left to answer.
    @MainActor static func perform(_ action: PushAction, body: String, cardId: String?) async -> String? {
        do {
            let data = try await BrainLink.shared.call { try await BrainClient.waiting(bases: $0, token: $1) }
            guard let cards = WaitingCards.parse(data) else { return BrainError.garbled.message }
            switch target(action, body: body, cardId: cardId, in: cards) {
            case .gone: return nil
            case .ambiguous: return "有几件事在等你，打开 Jarvis 看一下"
            case .card(let card):
                guard let req = action.answer.request(for: card) else { return nil }
                let status = try await BrainLink.shared.call { try await BrainClient.answer(req.path, req.body, bases: $0, token: $1) }
                return status == 400 || status == 422 ? "没发出去：" + BrainError.refused.message : nil
            }
        } catch let e as BrainError {
            return "没发出去：" + e.message
        } catch {
            return "没发出去：" + BrainError.refused.message
        }
    }
}

/// A tap on a notification that waits on him: the cards are opened, now or when the screen is up; `card` is the one the push names, if it names one.
enum CardsRequest {
    static let notification = Notification.Name("com.yilunshi.jarvis.cards")
    nonisolated(unsafe) static var pending = false
    nonisolated(unsafe) static var card: String?
}

@MainActor
final class PushNotifications: NSObject, UNUserNotificationCenterDelegate {
    static let shared = PushNotifications()

    /// Set at every launch, before it returns: a button pressed on a notification can launch the app in the background.
    func install() {
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        center.setNotificationCategories(PushAction.categories().union(RideAction.categories()))
    }

    /// While the app is open it polls what waits, so a waiting item already on screen as a card is not shown again as a banner.
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification) async -> UNNotificationPresentationOptions {
        let content = notification.request.content
        // a ride push while the app is open is a card, not a banner
        if content.threadIdentifier == PushShape.rideThread, let ride = RidePush(content) { Waiting.shared.showRide(ride); return [] }
        guard content.threadIdentifier == PushShape.waitingThread, let w = PushShape.Waiting(rawValue: content.categoryIdentifier) else { return [.banner, .list, .sound] }
        await Waiting.shared.poll()      // looks once more, so an item asked this second is seen
        let named = PushShape.cardId(content.userInfo)
        return Waiting.shared.cards.contains { c in named.map { c.id == $0 } ?? (c.kind == w.cardKind) } ? [] : [.banner, .list, .sound]
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse) async {
        let content = response.notification.request.content
        if let action = PushAction(rawValue: response.actionIdentifier) {
            if let failure = await PushAnswer.perform(action, body: content.body, cardId: PushShape.cardId(content.userInfo)) { await Self.tell(failure) }
        } else if let action = RideAction(rawValue: response.actionIdentifier), let ride = RidePush(content) {
            if let failure = await action.perform(ride) { await Self.tell(failure) }
        } else if response.actionIdentifier == UNNotificationDefaultActionIdentifier {
            await Self.tapped(content)
        }
    }

    /// A tap on the notification itself.
    static func tapped(_ content: UNNotificationContent) async {
        if let url = PushShape.url(content.userInfo) { await UIApplication.shared.open(url) }
        else if content.threadIdentifier == PushShape.waitingThread { openCards(card: PushShape.cardId(content.userInfo)) }
        else if content.threadIdentifier == PushShape.rideThread, let ride = RidePush(content) { Waiting.shared.showRide(ride); openCards() }      // the card is there to answer
        else if content.threadIdentifier == PushShape.talkThread { openCards() }      // she spoke first: the conversation, refreshed
    }

    private static func openCards(card: String? = nil) {
        CardsRequest.card = card
        CardsRequest.pending = true
        NotificationCenter.default.post(name: CardsRequest.notification, object: nil)
    }

    /// A local notification for a button that could not do what it said, so the press is not lost silently; tapping it opens the cards.
    private static func tell(_ line: String) async {
        let c = UNMutableNotificationContent()
        c.title = "Jarvis"; c.body = line; c.threadIdentifier = PushShape.waitingThread
        try? await UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: UUID().uuidString, content: c, trigger: nil))
    }
}
