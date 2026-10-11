import Foundation
import Observation
import UIKit

// What waits on the owner (docs/spec.html `GET /inherent/waiting`, jarvis/surface/inherent_server.py `waiting`): a confirmation card, an ask card,
// reminders and job-mail alerts, a plugin's sign-in, and the prompts Claude Code holds. Each is a card on the day screen with its own buttons;
// answering goes to that card's own route. While the app is open the brain is asked every few seconds, which also keeps Claude Code's prompts held for the phone.

struct CardField: Equatable {
    let label: String
    let choices: [String]
}

/// What a button does. The request each one makes is `request(for:)`, the only place the routes and their bodies are written down.
enum CardAnswer: Equatable {
    case decide(accept: Bool)          // a confirmation: POST /inherent/confirmation
    case fill, dismiss                 // an ask card: the typed or picked answers, or its close button
    case claude(String)                // a held Claude Code prompt: allow, always or deny
    case acknowledge                   // a notice: dismissed
    case plugin(String)                // a plugin sign-in: connect or cancel
    case ride(String)                  // a bus ride's question or warning (Ride.swift): board, not or end
    case openLogin                     // the plugin's sign-in link, opened on this phone: no request

    func request(for card: WaitingCard, filled: [String: String] = [:]) -> (path: String, body: [String: Any])? {
        let id = card.brainId
        switch self {
        case .decide(let accept): return ("/inherent/confirmation", ["confirmation_id": id, "decision": accept ? "accept" : "reject"])
        case .fill:
            let answers = filled.filter { !$0.value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
            return ("/inherent/clarification", ["clarification_id": id, "answers": answers])
        case .dismiss: return ("/inherent/clarification", ["clarification_id": id, "dismiss": true])
        case .claude(let decision): return ("/inherent/claude-requests/\(id)", ["decision": decision])
        case .acknowledge: return ("/inherent/notices/\(id)", ["action": "dismissed"])
        case .plugin(let operation): return ("/inherent/plugins/action", ["operation": operation, "data": ["request_id": id]])
        case .ride(let action): return ("/inherent/ride", ["action": action, action == "end" ? "ride_id" : "candidate_id": id])
        case .openLogin: return nil
        }
    }
}

struct CardAction: Equatable {
    let label: String
    let answer: CardAnswer
}

struct WaitingCard: Identifiable, Equatable {
    enum Kind: String { case confirmation, clarification, notice, plugin, claude, ride }
    let id: String                     // kind and the brain's id: ids of different kinds may coincide
    let kind: Kind
    let brainId: String
    var short: String                  // the label on the dial
    var title: String
    var meta: String
    var body: String
    var hint = ""                      // what "always" would allow, in the brain's own words
    var pushLine = ""                  // a Claude request as its notification words it: "tool · folder"
    var fields: [CardField] = []
    var trip: [String] = []            // a bus card's options, one line each (ADR 0205): the card has no fields
    var actions: [CardAction]          // the last one is the main button
    var loginURL: URL?
}

enum WaitingCards {
    private static func str(_ o: [String: Any], _ k: String) -> String { (o[k] as? String) ?? "" }
    private static func clip(_ s: String, _ n: Int) -> String { s.count > n ? String(s.prefix(n - 1)) + "…" : s }

    /// every card in an answer of `GET /inherent/waiting`, or nil when it is not one
    static func parse(_ data: Data) -> [WaitingCard]? {
        guard let o = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return nil }
        var cards: [WaitingCard] = []
        if let c = o["confirmation"] as? [String: Any], let id = c["id"] as? String { cards.append(confirmation(c, id)) }
        if let c = o["clarification"] as? [String: Any], let id = c["id"] as? String { cards.append(clarification(c, id)) }
        for r in (o["claude_requests"] as? [[String: Any]]) ?? [] { if let id = r["id"] as? String { cards.append(claude(r, id)) } }
        if let p = o["plugin_request"] as? [String: Any], let id = p["id"] as? String, let c = plugin(p, id) { cards.append(c) }
        // job mail and reminders ("mail", "digest"); any other kind (the terminal-settings card, ADR 0227) has choices a phone does not draw, so it is skipped, not shown as mail
        for n in ((o["notices"] as? [String: Any])?["notices"] as? [[String: Any]]) ?? [] where ["mail", "digest", nil].contains(n["kind"] as? String) {
            if let id = n["id"] as? String { cards.append(notice(n, id)) }
        }
        return cards
    }

    private static func recipients(_ v: Any?) -> String {
        if let l = v as? [String] { return l.joined(separator: ", ") }
        return (v as? String) ?? ""
    }

    /// a tool's arguments as lines of the card: what it is about to do, without the big content the brain keeps by reference
    private static func lines(_ args: [String: Any], skip: Set<String> = []) -> String {
        args.keys.sorted().compactMap { k -> String? in
            if k.hasPrefix("content_") || skip.contains(k) { return nil }
            switch args[k] {
            case let s as String: return "\(k): \(clip(s, 80))"
            case let n as NSNumber: return "\(k): \(n)"
            case let l as [String]: return "\(k): \(clip(l.joined(separator: ", "), 80))"
            default: return nil
            }
        }.prefix(6).joined(separator: "\n")
    }

    private static func confirmation(_ c: [String: Any], _ id: String) -> WaitingCard {
        let args = c["args"] as? [String: Any] ?? [:]
        let letter = c["letter"] as? Bool == true, action = str(c, "action"), source = str(c, "source")
        let title = action.isEmpty ? "等你点头" : action + "？"
        if letter {
            let to = recipients(args["to"]), subject = str(args, "subject")
            return WaitingCard(id: "confirmation-\(id)", kind: .confirmation, brainId: id, short: "邮件", title: title,
                               meta: "给 \(to)" + (subject.isEmpty ? "" : " · \(subject)"), body: str(args, "body"),
                               actions: [CardAction(label: "取消", answer: .decide(accept: false)), CardAction(label: "发送", answer: .decide(accept: true))])
        }
        return WaitingCard(id: "confirmation-\(id)", kind: .confirmation, brainId: id, short: "确认", title: title,
                           meta: source.isEmpty ? "等你点头" : "来自 \(source)", body: lines(args),
                           actions: [CardAction(label: "拒绝", answer: .decide(accept: false)), CardAction(label: "允许", answer: .decide(accept: true))])
    }

    /// the ask card of `GET /inherent/waiting` and of a `clarification.requested` row: a question and its fields, or a bus card (no fields, `trip` rows to read)
    static func clarification(_ c: [String: Any], _ id: String) -> WaitingCard {
        let fields = ((c["fields"] as? [[String: Any]]) ?? []).compactMap { f -> CardField? in
            guard let label = f["label"] as? String else { return nil }
            return CardField(label: label, choices: (f["choices"] as? [String]) ?? [])
        }
        let question = str(c, "question")
        let trip = tripLines(c["trip"] as? [String: Any])
        if !trip.isEmpty {
            return WaitingCard(id: "clarification-\(id)", kind: .clarification, brainId: id, short: "出行", title: question.isEmpty ? "怎么去" : question,
                               meta: "她查了出行", body: "", trip: trip, actions: [CardAction(label: "知道了", answer: .dismiss)])
        }
        return WaitingCard(id: "clarification-\(id)", kind: .clarification, brainId: id, short: "问你", title: question.isEmpty ? "她想问你" : question,
                           meta: fields.isEmpty ? "她想问你" : "要你补充 \(fields.count) 项", body: "", fields: fields,
                           actions: [CardAction(label: "取消", answer: .dismiss), CardAction(label: "发送", answer: .fill)])
    }

    /// up to three of a bus card's options: when to leave, the route and the stop to board at, when it arrives
    private static func tripLines(_ trip: [String: Any]?) -> [String] {
        ((trip?["options"] as? [[String: Any]]) ?? []).prefix(3).map { o in
            let arrive = str(o, "arrive_at")
            return [str(o, "leave_at") + " 出发", str(o, "route"), str(o, "board_stop")].filter { $0 != " 出发" && !$0.isEmpty }.joined(separator: " · ") + (arrive.isEmpty ? "" : " → " + arrive)
        }
    }

    private static func claude(_ r: [String: Any], _ id: String) -> WaitingCard {
        let tool = str(r, "tool"), input = r["input"] as? [String: Any] ?? [:]
        let project = str(r, "project").isEmpty ? (str(r, "cwd") as NSString).lastPathComponent : str(r, "project")
        let always = str(r, "always")
        let folder = (str(r, "cwd") as NSString).lastPathComponent
        var actions = [CardAction(label: "拒绝", answer: .claude("deny"))]
        var body: String
        if tool == "AskUserQuestion" {
            // this prompt only takes an allow that carries the answers back; the phone cannot give them yet
            body = ((input["questions"] as? [[String: Any]])?.first.map { str($0, "question") } ?? "") + "\n请回到 Mac 上回答。"
        } else {
            // what it is about to do: the command, the file, the address, else the first plain arguments
            body = ["command", "file_path", "path", "url", "pattern", "description"].lazy.compactMap { input[$0] as? String }.first { !$0.isEmpty }.map { clip($0, 300) } ?? lines(input)
            if !always.isEmpty { actions.append(CardAction(label: "一直允许", answer: .claude("always"))) }
            actions.append(CardAction(label: "允许", answer: .claude("allow")))
        }
        return WaitingCard(id: "claude-\(id)", kind: .claude, brainId: id, short: "Claude", title: "Claude Code 想用 \(tool)",
                           meta: [project, str(r, "title")].filter { !$0.isEmpty }.joined(separator: " · "),
                           body: body, hint: tool == "AskUserQuestion" ? "" : always, pushLine: folder.isEmpty ? tool : "\(tool) · \(folder)", actions: actions)
    }

    private static func plugin(_ p: [String: Any], _ id: String) -> WaitingCard? {
        let name = str(p, "plugin_id"), state = str(p, "state"), purpose = str(p, "purpose")
        func card(_ meta: String, _ body: String, _ actions: [CardAction], login: URL? = nil) -> WaitingCard {
            WaitingCard(id: "plugin-\(id)", kind: .plugin, brainId: id, short: "插件", title: "连接 \(name)", meta: meta, body: body, actions: actions, loginURL: login)
        }
        let cancel = CardAction(label: "取消", answer: .plugin("cancel"))
        switch state {
        case "offered": return card(purpose.isEmpty ? "等你点头" : purpose, "要登录这个应用，她才能用它。", [cancel, CardAction(label: "连接", answer: .plugin("connect"))])
        case "connecting": return card("正在连接…", "", [cancel])
        case "authorizing":
            let url = (p["auth_url"] as? String).flatMap(URL.init(string:))
            return card("等你登录", "在手机上登录后，她会接着做。", [cancel] + (url == nil ? [] : [CardAction(label: "登录", answer: .openLogin)]), login: url)
        case "error": return card("没连上", str(p, "error"), [CardAction(label: "知道了", answer: .plugin("cancel"))])
        default: return nil          // ready or cancelled: nothing waits
        }
    }

    private static func notice(_ n: [String: Any], _ id: String) -> WaitingCard {
        let title = str(n, "title"), line = str(n, "line")
        return WaitingCard(id: "notice-\(id)", kind: .notice, brainId: id, short: str(n, "mail_kind") == "reminder" ? "提醒" : "邮件",
                           title: title.isEmpty ? str(n, "text") : title, meta: line, body: "", actions: [CardAction(label: "知道了", answer: .acknowledge)])
    }
}

extension BrainClient {
    /// `GET /inherent/waiting`: the raw answer, for `WaitingCards.parse`.
    static func waiting(bases: [URL], token: String) async throws -> Data {
        try await send("GET", "/inherent/waiting", bases: bases, token: token, unauthorized: .revoked).data
    }

    /// One answer to a card, on the route of its kind. 200 it was taken; 404 or 409 it was not waiting any more; 400 or 422 the brain refused the body.
    static func answer(_ path: String, _ body: [String: Any], bases: [URL], token: String) async throws -> Int {
        let data = try JSONSerialization.data(withJSONObject: body)
        return try await send("POST", path, body: data, also: [400, 404, 409, 422], bases: bases, token: token, unauthorized: .revoked).status
    }
}

@Observable
final class Waiting {
    static let shared = Waiting()

    private(set) var cards: [WaitingCard] = []
    /// bumps when the cards changed, so the engine knows to put them on the dial
    private(set) var version = 0
    /// what has been typed or picked on each ask card so far, by card id and field label; kept here because a card's view is rebuilt whenever the list is
    var inputs: [String: [String: String]] = [:]
    private(set) var sending: Set<String> = []
    private(set) var failures: [String: String] = [:]

    @ObservationIgnored private var task: Task<Void, Never>?
    @ObservationIgnored private var round = 0
    @ObservationIgnored private var firstSeen: [String: Date] = [:]
    @ObservationIgnored private var awaitingLogin: Set<String> = []
    /// The ride push shown as a card while the app is open. The brain's waiting list does not hold it, so it is added to every look until answered or gone.
    @ObservationIgnored private var ride: RidePush?

    // MARK: asking

    /// While the app is open and paired the brain is asked every few seconds; `stop` (the app left the screen) ends it, and nothing else runs on a timer.
    func start() {
        guard task == nil, BrainLink.shared.hasToken else { return }
        round += 1
        let mine = round
        task = Task { @MainActor [weak self] in
            while !Task.isCancelled, let self, BrainLink.shared.hasToken, UIApplication.shared.applicationState != .background {
                await self.poll()
                try? await Task.sleep(nanoseconds: 4_000_000_000)
            }
            if let self, self.round == mine { self.task = nil; if !BrainLink.shared.hasToken { self.apply([]) } }
        }
    }

    func stop() {
        task?.cancel(); task = nil
        if !BrainLink.shared.hasToken { apply([]) }
    }

    /// One look at what waits. A failure leaves what is on screen as it was; the next round asks again.
    @MainActor func poll() async {
        guard let data = try? await BrainLink.shared.call({ try await BrainClient.waiting(bases: $0, token: $1) }), let new = WaitingCards.parse(data) else { return }
        await dropStaleRide()
        apply(new)
    }

    /// A ride push arrived while the app is open: it is a card like the others, with the same two buttons.
    func showRide(_ push: RidePush) {
        ride = push
        apply(cards.filter { $0.kind != .ride })
    }

    /// The question or warning may have been answered elsewhere (the Live Activity, the other phone, the Mac): the brain is asked what ride there is before the card stays.
    private func dropStaleRide() async {
        guard let r = ride, let data = try? await BrainLink.shared.call({ try await BrainClient.ride(bases: $0, token: $1) }),
              let o = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return }
        let riding = (o["ride"] as? [String: Any])?["ride_id"] as? String
        // a warning is for the ride it names; a question is moot once he is on any ride
        if r.kind == .alight ? riding != r.id : riding != nil { ride = nil }
    }

    #if DEBUG
    /// a made-up list of what waits, for a scene on a phone that is not paired
    func debugSet(_ cards: [WaitingCard]) { apply(cards) }
    #endif

    private func apply(_ fetched: [WaitingCard]) {
        let new = fetched + (ride.map { [$0.card] } ?? [])
        let ids = Set(new.map(\.id)), now = Cal.now()
        for id in ids where firstSeen[id] == nil { firstSeen[id] = now }
        firstSeen = firstSeen.filter { ids.contains($0.key) }
        inputs = inputs.filter { ids.contains($0.key) }
        failures = failures.filter { ids.contains($0.key) }
        awaitingLogin.formIntersection(ids)
        // the login link he asked for is opened once, the moment the brain has one
        for c in new where awaitingLogin.contains(c.id) {
            if let url = c.loginURL { awaitingLogin.remove(c.id); UIApplication.shared.open(url) }
        }
        if new != cards { cards = new; version += 1 }
    }

    // MARK: answering

    func answer(_ card: WaitingCard, _ answer: CardAnswer, filled: [String: String] = [:]) {
        if answer == .openLogin { if let url = card.loginURL { UIApplication.shared.open(url) }; return }
        guard let req = answer.request(for: card, filled: filled), !sending.contains(card.id) else { return }
        sending.insert(card.id); failures[card.id] = nil
        Task { @MainActor in
            defer { sending.remove(card.id) }
            do {
                let status = try await BrainLink.shared.call { try await BrainClient.answer(req.path, req.body, bases: $0, token: $1) }
                if status == 400 || status == 422 { failures[card.id] = "没发出去：" + BrainError.refused.message; return }
                if status == 200 { Haptic.notify(.success) }
                // taken, or it was not waiting any more: either way it is not waiting. A plugin's card goes on in its next state, which the next look shows.
                if card.kind == .plugin {
                    if answer == .plugin("connect") { awaitingLogin.insert(card.id) }
                } else {
                    if card.kind == .ride { ride = nil }
                    cards.removeAll { $0.id == card.id }; version += 1
                }
                if card.kind == .notice { BrainLink.shared.opened() }      // the day's reminder is acknowledged now: ask for the days again
                await poll()
            } catch let e as BrainError {
                failures[card.id] = "没发出去：" + e.message
            } catch {
                failures[card.id] = "没发出去：" + BrainError.refused.message
            }
        }
    }

    // MARK: on the dial

    /// The cards as stars. A reminder that is also in the day (same id) is that star with its card, not a second one.
    func events(merging day: [Ev]) -> [Ev] {
        let cal = Cal.cur
        var out: [Ev] = []
        for c in cards {
            if c.kind == .notice, let e = day.first(where: { $0.id == c.brainId }) { e.card = c; e.st = .wait; continue }
            let e = Ev("wait-" + c.id, cal.t(firstSeen[c.id] ?? Cal.now()), .her, .wait, c.title, c.meta, s: c.short)
            e.live = true; e.card = c
            out.append(e)
        }
        return out
    }
}
