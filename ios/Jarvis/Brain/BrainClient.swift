import Foundation
import Security

// The phone's side of the brain's HTTP contract: claim a pairing code (ADR 0196), ask for a day (ADR 0199), probe liveness.
// docs/spec.html: the pairing routes and `GET /inherent/day` paragraphs of the daemon chapter. The brain's own words are in
// jarvis/surface/device_pairing.py (claim) and jarvis/runtime/inherent_loop.py `_read_day` (day).

/// What the QR on the owner's screen holds: `{"code": "...", "brain": ["http://host:port", ...]}`, the one-time code and the base URLs the brain answers on.
struct PairingLink {
    let code: String
    let brain: [URL]

    init?(_ text: String) {
        guard let data = text.trimmingCharacters(in: .whitespacesAndNewlines).data(using: .utf8),
              let o = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let code = o["code"] as? String, !code.isEmpty else { return nil }
        let raw = (o["brain"] as? [String]) ?? (o["brain"] as? String).map { [$0] } ?? []
        let urls = raw.compactMap { s -> URL? in
            guard let u = URL(string: s.trimmingCharacters(in: .whitespaces)), ["http", "https"].contains(u.scheme), u.host != nil else { return nil }
            return u
        }
        if urls.isEmpty { return nil }
        self.code = code; self.brain = urls
    }
}

enum BrainError: Error, Equatable {
    case badLink                  // the QR is not a brain's
    case expired                  // 401 on the claim: wrong, used or expired, all the same refusal
    case revoked                  // 401 with a token: this phone was unpaired on the brain
    case unreachable(tailnet: Bool)
    case notOpen                  // the brain answers but has no phone routes (404), or its Host check refuses this address (400)
    case refused                  // any other status
    case garbled                  // a 200 that is not the shape in the contract

    var message: String {
        switch self {
        case .badLink: "这不是大脑的二维码"
        case .expired: "配对码已过期"
        case .revoked: "这台手机已被大脑取消配对"
        case .unreachable(let tailnet): tailnet ? "需要在 Tailscale 里" : "连不上大脑"
        case .notOpen: "这个大脑还没开放给手机"
        case .refused: "大脑没有答应，稍后再试"
        case .garbled: "大脑的回答看不懂"
        }
    }
}

struct DayReply: Decodable {
    let date: String
    let tz: String
    let start_ms: Int64
    let end_ms: Int64
    let now_ms: Int64
    let items: [DayItem]
    let missing: [String]
    /// her one sentence about the day, and the items she or he marked as what mattered; absent from a brain that does not serve them yet
    let line: DayLine?
    let highlights: [Highlight]
    /// the all-day events of the day
    let labels: [DayLabel]

    init(from d: Decoder) throws {
        let c = try d.container(keyedBy: Keys.self)
        date = try c.decode(String.self, forKey: .date)
        tz = try c.decode(String.self, forKey: .tz)
        start_ms = try c.decode(Int64.self, forKey: .start_ms)
        end_ms = try c.decode(Int64.self, forKey: .end_ms)
        now_ms = try c.decode(Int64.self, forKey: .now_ms)
        missing = try c.decode([String].self, forKey: .missing)
        // one item the phone cannot read costs that item, not the day
        items = try c.decode([Lossy<DayItem>].self, forKey: .items).compactMap(\.value)
        // what is new is optional, and one value that cannot be read costs only itself
        line = (try? c.decodeIfPresent(DayLine.self, forKey: .line)) ?? nil
        labels = ((try? c.decodeIfPresent([Lossy<DayLabel>].self, forKey: .labels)) ?? nil)?.compactMap(\.value) ?? []
        highlights = ((try? c.decodeIfPresent([Lossy<Highlight>].self, forKey: .highlights)) ?? nil)?.compactMap(\.value) ?? []
    }
    private enum Keys: String, CodingKey { case date, tz, start_ms, end_ms, now_ms, items, missing, line, highlights, labels }
    private struct Lossy<T: Decodable>: Decodable {
        let value: T?
        init(from d: Decoder) { value = try? T(from: d) }
    }
}

/// Her sentence to him about the day (`line` of `GET /inherent/day`). `draft` is true while the day is still going; the screen shows both alike.
struct DayLine: Decodable, Equatable {
    let text: String
    let draft: Bool?
    let written_at: String?
}

/// An all-day event, which is not an item but a name for the day
struct DayLabel: Decodable, Equatable {
    let title: String
    let source: String?
}

/// An item that mattered that day: `id` is an item of the day, `by` is "her" or "him"
struct Highlight: Decodable, Equatable {
    let id: String, label: String
    let by: String?
}

/// One item of a day (docs/spec.html, `GET /inherent/day`): the common fields plus whichever the kind carries.
struct DayItem: Decodable {
    struct Top: Decodable { let name: String; let seconds: Double }
    let id: String, lane: String, kind: String, state: String
    let start_ms: Int64
    let end_ms: Int64?
    let place: String?, activity: String?, text: String?, app: String?
    let minutes: Double?, active_s: Double?, turns: Int?
    let fired: Bool?, acknowledged: Bool?
    let projects: [Top]?, apps: [Top]?
    /// a stay inside a place he keeps (spec: `kept_place`); `place` stays the phone's own words
    let kept_place: KeptPlace?
    /// an Outlook event or a pinned bus departure (kinds `event` and `departure`): the title ("route · stop" for a bus), and where and when the bus goes
    let title: String?, route: String?, board_stop: String?, to: String?
    let leave_at_ms: Int64?
    /// an event he has to go to, from his mail or his own words (ADR 0274): where it came from (`mail`, `said` or `plan`, none for a calendar event), whether he confirmed it, whether it is private (counselling, legal, health), and `place` as above
    let source: String?, confirmed: Bool?, `private`: Bool?
    /// what a picture says (kind `picture`) and the stop a bus ride ended at (kind `ride`). Newer than the rest: one that cannot be read costs only itself, not the item.
    @Soft var note: String?
    @Soft var alight_stop: String?
    /// what he handed over: a payment (`amount`, `currency`, `direction` out or in, `counterparty`; `text` is what it was for) and the Claude chats (`messages` in a chat, `chats` in a day's letter)
    @Soft var amount: Amount?
    @Soft var currency: String?
    @Soft var direction: String?
    @Soft var counterparty: String?
    @Soft var messages: Int?
    @Soft var chats: Int?
}

/// An amount of money: the brain writes `"12.40"` (a string, two decimals); a number is read too. Kept in hundredths, so a sum stays exact.
struct Amount: Decodable, Equatable {
    let cents: Int
    init(cents: Int) { self.cents = cents }
    init(from d: Decoder) throws {
        let c = try d.singleValueContainer()
        // an amount of ten billion or more is not a payment: it is a field that is not what it says
        if let s = try? c.decode(String.self), let v = Decimal(string: s.trimmingCharacters(in: .whitespaces)), v.magnitude < 10_000_000_000 {
            var x = v * 100, r = Decimal()
            NSDecimalRound(&r, &x, 0, .plain)
            cents = Int(truncating: NSDecimalNumber(decimal: r))
        } else if let x = try? c.decode(Double.self), x.isFinite, abs(x) < 1e10 {
            cents = Int((x * 100).rounded())
        } else {
            throw DecodingError.dataCorruptedError(in: c, debugDescription: "not an amount")
        }
    }
}

/// A field that is read when it can be and is nil when it is missing or has a shape this phone does not know.
@propertyWrapper struct Soft<T: Decodable>: Decodable {
    var wrappedValue: T?
    init(wrappedValue: T?) { self.wrappedValue = wrappedValue }
    init(from d: Decoder) throws { wrappedValue = try? T(from: d) }
}
extension KeyedDecodingContainer {
    func decode<T>(_ type: Soft<T>.Type, forKey key: Key) throws -> Soft<T> { (try? decodeIfPresent(type, forKey: key)) ?? Soft(wrappedValue: nil) }
}

struct KeptPlace: Decodable, Equatable {
    let id: String, name: String
    let role: String?           // home, school or other
}

/// Every call tries the brain's addresses in order with a short timeout, and says what went wrong in the owner's words.
enum BrainClient {
    private static func makeSession(request: TimeInterval, resource: TimeInterval) -> URLSession {
        let c = URLSessionConfiguration.ephemeral
        c.timeoutIntervalForRequest = request
        c.timeoutIntervalForResource = resource
        c.waitsForConnectivity = false
        c.httpCookieStorage = nil
        return URLSession(configuration: c)
    }
    nonisolated(unsafe) static var session = makeSession(request: 4, resource: 8)      // var so a test can put a stub under it
    /// for the calls that wait on the brain: a picture going up, a question whose answer takes up to 25 s
    nonisolated(unsafe) static var slowSession = makeSession(request: 40, resource: 60)

    /// A tailnet address: the CGNAT range Tailscale hands out, a MagicDNS name, or a bare name that only MagicDNS can resolve.
    static func isTailnet(_ u: URL) -> Bool {
        guard let h = u.host?.lowercased() else { return false }
        let p = h.split(separator: ".").compactMap { Int($0) }
        if p.count == 4 { return p[0] == 100 && (64...127).contains(p[1]) }
        return h.hasSuffix(".ts.net") || !h.contains(".")
    }

    struct Reply { let data: Data; let base: URL; let ms: Int; var status = 200 }

    /// `path` on the first base that answers. A refusal that means "this address is wrong" (400, 404) moves on; one that means "this answer is final" (401) stops.
    /// `also` lists statuses that are the route's own answer (a card no longer waiting, a file refused): they come back as a `Reply` with that status.
    static func send(_ method: String, _ path: String, query: [URLQueryItem] = [], body: Data? = nil, contentType: String = "application/json", slow: Bool = false, also: Set<Int> = [],
                     bases: [URL], token: String?, unauthorized: BrainError) async throws -> Reply {
        var worst: BrainError?
        func note(_ e: BrainError) { if worst == nil || (worst!.rank < e.rank) { worst = e } }
        for base in bases {
            guard var comps = URLComponents(url: base, resolvingAgainstBaseURL: false) else { continue }
            comps.path = path; comps.queryItems = query.isEmpty ? nil : query
            guard let url = comps.url else { continue }
            var req = URLRequest(url: url)
            req.httpMethod = method
            if let b = body { req.httpBody = b; req.setValue(contentType, forHTTPHeaderField: "Content-Type") }
            if let t = token { req.setValue("Bearer \(t)", forHTTPHeaderField: "Authorization") }
            let t0 = Date()
            do {
                let (data, resp) = try await (slow ? slowSession : session).data(for: req)
                let status = (resp as? HTTPURLResponse)?.statusCode ?? 0
                let ms = Int(Date().timeIntervalSince(t0) * 1000)
                switch status {
                case 200...299: return Reply(data: data, base: base, ms: ms)
                case _ where also.contains(status): return Reply(data: data, base: base, ms: ms, status: status)
                case 401:
                    let guarded = token == nil || isGuardRefusal(data)
                    trace("401 \(method) \(path) from \(base.host ?? "?"):\(base.port ?? 0) detail=\(detail(data) ?? "none") auth=\(token != nil ? "yes" : "no") -> \(guarded ? "token refused" : "route refusal")")
                    throw guarded ? unauthorized : .refused
                case 400, 404: note(.notOpen)
                default: note(.refused)
                }
            } catch let e as BrainError {
                throw e
            } catch {
                note(.unreachable(tailnet: isTailnet(base)))
            }
        }
        throw worst ?? .unreachable(tailnet: false)
    }

    /// The brain's auth guard answers an unknown token `{"detail":"unauthorized"}`. A route that has its own rule (`"desktop authorization required"`)
    /// also answers 401 to a good token, which says nothing about the pairing. A body that is missing or unreadable counts as the guard's.
    static func isGuardRefusal(_ body: Data) -> Bool { detail(body).map { $0 == "unauthorized" } ?? true }

    private static func detail(_ body: Data) -> String? {
        ((try? JSONSerialization.jsonObject(with: body)) as? [String: Any])?["detail"] as? String
    }

    /// One line to the app's on-device log (set by the app; the share extension logs nothing). Never the token.
    nonisolated(unsafe) static var trace: (String) -> Void = { _ in }

    /// True only if the brain refuses this token at its guard: `GET /inherent/waiting`, open to every paired device, asked across the bases.
    /// A failed probe (unreachable, a route refusal, a 200) is not a refusal of the token.
    static func tokenRefused(bases: [URL], token: String) async -> Bool {
        let refused: Bool
        do { _ = try await send("GET", "/inherent/waiting", bases: bases, token: token, unauthorized: .revoked); refused = false } catch { refused = (error as? BrainError) == .revoked }
        trace("probe GET /inherent/waiting on \(bases.map { "\($0.host ?? "?"):\($0.port ?? 0)" }.joined(separator: ",")) -> \(refused ? "token refused again" : "token not refused")")
        return refused
    }

    /// Trade the code for this phone's token (`POST /inherent/devices/claim`, open without a token). The first base that answers fixes the address.
    static func claim(_ link: PairingLink) async throws -> (device: String, token: String, base: URL) {
        let body = try JSONSerialization.data(withJSONObject: ["code": link.code])
        let r = try await send("POST", "/inherent/devices/claim", body: body, bases: link.brain, token: nil, unauthorized: .expired)
        guard let o = try? JSONSerialization.jsonObject(with: r.data) as? [String: Any],
              let device = o["device"] as? String, let token = o["token"] as? String, !token.isEmpty else { throw BrainError.garbled }
        return (device, token, r.base)
    }

    /// `GET /inherent/day?date=YYYY-MM-DD`, under this phone's token.
    static func day(_ date: String, bases: [URL], token: String) async throws -> (reply: DayReply, base: URL, ms: Int) {
        let r = try await send("GET", "/inherent/day", query: [URLQueryItem(name: "date", value: date)], bases: bases, token: token, unauthorized: .revoked)
        guard let reply = try? JSONDecoder().decode(DayReply.self, from: r.data) else { throw BrainError.garbled }
        return (reply, r.base, r.ms)
    }

    /// `GET /api/health`: the liveness probe, the one route that needs no token. The time it takes is the latency shown in settings.
    static func health(bases: [URL]) async throws -> (base: URL, ms: Int) {
        let r = try await send("GET", "/api/health", bases: bases, token: nil, unauthorized: .refused)
        return (r.base, r.ms)
    }
}

private extension BrainError {
    /// which failure is the one worth telling: an answer that came back beats silence
    var rank: Int {
        switch self {
        case .unreachable: 0
        case .refused: 1
        case .notOpen: 2
        default: 3
        }
    }
}

/// The phone's token lives only in the Keychain, in the access group the app and its share extension share: both list it first in their entitlements,
/// and an item written without a group goes to the first one.
enum Keychain {
    private static let service = "com.allen.jarvis.brain"
    private static let sharedGroupName = "com.yilunshi.jarvis.shared"
    private static func query(_ account: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service, kSecAttrAccount as String: account]
    }
    private static func add(_ value: Data, _ account: String) -> Bool {
        var q = query(account)
        q[kSecValueData as String] = value
        q[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly   // readable when a background wake reports (ADR 0197)
        return SecItemAdd(q as CFDictionary, nil) == errSecSuccess
    }
    static func get(_ account: String) -> String? {
        var q = query(account)
        q[kSecReturnData as String] = true; q[kSecReturnAttributes as String] = true; q[kSecMatchLimit as String] = kSecMatchLimitOne
        var out: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess, let row = out as? [String: Any],
              let d = row[kSecValueData as String] as? Data, let value = String(data: d, encoding: .utf8) else { return nil }
        // An item written before the shared group existed sits in the app's own group, which the extension cannot read: it is copied into the shared one, then the old one goes.
        if let old = row[kSecAttrAccessGroup as String] as? String, !old.hasSuffix(sharedGroupName), add(d, account) {
            var gone = query(account)
            gone[kSecAttrAccessGroup as String] = old
            SecItemDelete(gone as CFDictionary)
        }
        return value
    }
    @discardableResult static func set(_ value: String, _ account: String) -> Bool {
        SecItemDelete(query(account) as CFDictionary)
        return add(Data(value.utf8), account)
    }
    static func delete(_ account: String) { SecItemDelete(query(account) as CFDictionary) }
}
