// words: legacy
import Foundation
import Security

// The phone's side of the brain's HTTP contract: claim a pairing code (ADR 0196), ask for a day (ADR 0199), probe liveness.
// docs/spec.html: the pairing routes and `GET /inherent/day` paragraphs of the daemon chapter. The brain's own words are in
// jarvis/surface/device_pairing.py (claim) and jarvis/runtime/daemon/routes.py `read_day` (day).

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
    /// which reading of the day this is, so that what is made of it can be kept for as long as it is the one on show (each decoding is a new one)
    let serial = UUID()
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
    /// the day's weather and what Health reported for it (null from a brain that does not serve them, or when it could not read them)
    let weather: DayWeather?
    let summary: DaySummary?
    /// what he was doing, as spans of the day's doing lane, and how much of his waking time they explain (ADR 0302); absent from a brain that does not serve them yet
    let spans: [DaySpan]
    let coverage: DayCoverage?

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
        weather = (try? c.decodeIfPresent(DayWeather.self, forKey: .weather)) ?? nil
        summary = (try? c.decodeIfPresent(DaySummary.self, forKey: .summary)) ?? nil
        spans = ((try? c.decodeIfPresent([Lossy<DaySpan>].self, forKey: .spans)) ?? nil)?.compactMap(\.value) ?? []
        coverage = (try? c.decodeIfPresent(DayCoverage.self, forKey: .coverage)) ?? nil
    }
    private enum Keys: String, CodingKey { case date, tz, start_ms, end_ms, now_ms, items, missing, line, highlights, labels, weather, summary, spans, coverage }
}

/// A value that is read when it can be and is nil when it cannot: one element of a list that has a shape this phone does not know costs that element only.
struct Lossy<T: Decodable>: Decodable {
    let value: T?
    init(from d: Decoder) { value = try? T(from: d) }
}

/// The day's weather in °C (`weather` of `GET /inherent/day`): WeatherKit's condition as the brain words it (`mostly cloudy`), the high and the low, and the sun's rise and set in
/// epoch milliseconds. Any of them may be missing.
struct DayWeather: Decodable {
    @Soft var condition: String?
    @Soft var high: Double?
    @Soft var low: Double?
    @Soft var sunrise_ms: Int64?
    @Soft var sunset_ms: Int64?
}

/// One span of the doing lane (`spans` of `GET /inherent/day`): what he was doing for a stretch of the day. Only what the phone reads is here. `title` may be nil; `basis` says who wrote it
/// (`observed` the program, from facts; `inferred` a model; `stated` his own answer); `end_ms` nil is still going. `detail` is the model's one line about the stretch, `place_id` the stay he
/// was at, `item_ids` the day's items that overlap it (ADR 0307: a lasting item in every stretch it overlaps, a point in the one it falls in), `clues` what the model went by. One that is
/// not this shape costs only itself.
struct DaySpan: Decodable {
    let id: String, lane: String, kind: String
    let start_ms: Int64
    let end_ms: Int64?
    @Soft var title: String?
    @Soft var category: String?
    @Soft var basis: String?
    @Soft var detail: String?
    @Soft var confidence: String?
    @Soft var place_id: String?
    @SoftList var item_ids: [String]?
    @SoftList var clues: [DayClue]?
}

/// one thing the model went by for a stretch of the day; its words
struct DayClue: Decodable {
    @Soft var text: String?
}

/// How much of his waking time the doing lane explains (`coverage`), in epoch milliseconds
struct DayCoverage: Decodable {
    @Soft var waking_start_ms: Int64?
    @Soft var waking_end_ms: Int64?
    @Soft var waking_ms: Int64?
    @Soft var filled_ms: Int64?
}

/// What Health reported for the day (`summary`); a value it did not have is nil.
struct DaySummary: Decodable {
    @Soft var steps: Double?
    @Soft var resting_hr: Double?
    @Soft var hrv: Double?
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

/// One item of a day (docs/spec.html, `GET /inherent/day`): the common fields plus whichever the kind carries. A field that is not the shape this phone knows is nil, so an item
/// with one bad value is still the item; only one without an id, a kind or a start (or with an end that is not a time) is not one, and is left out with the list's others.
struct DayItem: Decodable {
    struct Top: Decodable {
        let name: String
        let seconds: Double
        init(from d: Decoder) throws {
            let c = try d.container(keyedBy: Keys.self)
            name = try c.decode(String.self, forKey: .name)
            // the apps of a block of phone use are counted in minutes, the Mac's in seconds
            seconds = (try? c.decodeIfPresent(Double.self, forKey: .seconds)) ?? (try? c.decodeIfPresent(Double.self, forKey: .minutes)).map { $0 * 60 } ?? 0
        }
        private enum Keys: String, CodingKey { case name, seconds, minutes }
    }
    let id: String, lane: String, kind: String, state: String
    let start_ms: Int64
    let end_ms: Int64?
    @Soft var place: String?
    @Soft var activity: String?
    @Soft var text: String?
    @Soft var app: String?
    @Soft var minutes: Double?
    @Soft var active_s: Double?
    @Soft var turns: Int?
    @Soft var fired: Bool?
    @Soft var acknowledged: Bool?
    @SoftList var projects: [Top]?
    @SoftList var apps: [Top]?
    /// a stay inside a place he keeps (spec: `kept_place`); `place` stays the phone's own words
    @Soft var kept_place: KeptPlace?
    /// where a stay was (the picture of a stay carries its covering stay's too)
    @Soft var lat: Double?
    @Soft var lng: Double?
    @Soft var accuracy_m: Double?
    /// an Outlook event or a pinned bus departure (kinds `event` and `departure`): the title ("route · stop" for a bus), and where and when the bus goes
    @Soft var title: String?
    @Soft var route: String?
    @Soft var board_stop: String?
    @Soft var to: String?
    @Soft var leave_at_ms: Int64?
    /// an event he has to go to, from his mail or his own words (ADR 0274): where it came from (`mail`, `said` or `plan`, none for a calendar event), whether he confirmed it, whether it is private (counselling, legal, health), and `place` as above
    @Soft var source: String?
    @Soft var confirmed: Bool?
    @Soft var `private`: Bool?
    /// what a picture says (kind `picture`) and the stop a bus ride ended at (kind `ride`), and how a ride ended and whether its bus is followed live. Newer than the rest.
    @Soft var note: String?
    @Soft var attachment: String?
    @Soft var alight_stop: String?
    @Soft var ended_reason: String?
    @Soft var credit: String?
    /// what he handed over: a payment (`amount`, `currency`, `direction` out or in, `counterparty`; `text` is what it was for) and the Claude chats (`messages` in a chat, `chats` in a day's letter)
    @Soft var amount: Amount?
    @Soft var currency: String?
    @Soft var direction: String?
    @Soft var counterparty: String?
    @Soft var messages: Int?
    @Soft var chats: Int?
    /// what Health knew of a night (every stage sample that overlaps it) and of a workout
    @SoftList var stages: [SleepStage]?
    @Soft var energy_kcal: Double?
    @Soft var distance_m: Double?
    @Soft var avg_hr: Double?
    /// a Claude Code session (`title` above, its latest recap as `detail`)
    @Soft var project: String?
    @Soft var account: String?
    @Soft var session_id: String?
    @Soft var detail: String?
    /// what his devices sync to the Mac (ADR 0305): a block of phone or tablet use (`device`, `active_s`, `apps` with their minutes), music or video (`app`, `active_s`, `titles`), his messages in
    /// one conversation (`conversation`, `sent`, `received`) and a call on the phone (`person`, `direction` sent or received, `duration_s`)
    @Soft var device: String?
    @SoftList var titles: [MediaTitle]?
    @Soft var conversation: String?
    @Soft var sent: Int?
    @Soft var received: Int?
    @Soft var person: String?
    @Soft var duration_s: Double?
}

/// a track or a video of a block of playing
struct MediaTitle: Decodable {
    @Soft var title: String?
    @Soft var artist: String?
}

/// One stretch of a night as Health recorded it: `core`, `deep`, `rem`, `awake`, `in_bed` or `unspecified`, from and to in epoch milliseconds
struct SleepStage: Decodable, Equatable {
    let stage: String
    let start_ms: Int64
    let end_ms: Int64
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
/// A list that is read element by element, so an element with a shape this phone does not know costs that element only; a value that is not a list at all is nil.
@propertyWrapper struct SoftList<T: Decodable>: Decodable {
    var wrappedValue: [T]?
    init(wrappedValue: [T]?) { self.wrappedValue = wrappedValue }
    init(from d: Decoder) throws { wrappedValue = (try? [Lossy<T>](from: d))?.compactMap(\.value) }
}
extension KeyedDecodingContainer {
    func decode<T>(_ type: Soft<T>.Type, forKey key: Key) throws -> Soft<T> { (try? decodeIfPresent(type, forKey: key)) ?? Soft(wrappedValue: nil) }
    func decode<T>(_ type: SoftList<T>.Type, forKey key: Key) throws -> SoftList<T> { (try? decodeIfPresent(type, forKey: key)) ?? SoftList(wrappedValue: nil) }
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
    /// for 理一下 alone: the brain holds the answer while a model writes the day (up to about a minute)
    nonisolated(unsafe) static var tidySession = makeSession(request: 90, resource: 100)

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
    static func send(_ method: String, _ path: String, query: [URLQueryItem] = [], body: Data? = nil, contentType: String = "application/json", slow: Bool = false, patient: Bool = false, also: Set<Int> = [],
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
                let (data, resp) = try await (patient ? tidySession : slow ? slowSession : session).data(for: req)
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
