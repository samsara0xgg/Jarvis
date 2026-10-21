// words: legacy
import Foundation
import Observation
import os

/// The phone's link to the brain: whether it is paired, whether the brain answers, and the days it has served.
/// Everything here is read and written on the main thread (the UI and the engine's frame step); the network runs in `BrainClient`.
/// The token is in the Keychain and in memory only; the brain's addresses and this phone's name are in UserDefaults.
@Observable
final class BrainLink {
    static let shared = BrainLink()

    enum State: Equatable {
        case unpaired
        case pairing
        case paired(String)          // the address the brain last answered on
        case error(String)           // a short Chinese message: what the owner can do about it
    }

    private(set) var state: State = .unpaired
    /// A token is held. The app shows the brain's days while this is true, whether or not the brain answers right now.
    private(set) var hasToken = false
    private(set) var deviceName: String?
    private(set) var latencyMs: Int?
    /// the days the brain has served, by their `date`
    private(set) var days: [String: DayReply] = [:]
    /// bumps whenever `days` or the pairing changed, so the engine knows to adopt it
    private(set) var dataVersion = 0
    /// bumps when the app is opened again: the days are asked for once more (docs: refresh on open or on a push, not on a timer)
    private(set) var openTick = 1

    @ObservationIgnored private var token: String?
    @ObservationIgnored private var bases: [URL] = []
    /// the address that answered the claim: the daemon that issued the token. The QR can list others (a Mac on the tailnet) whose guard does not know it. Nil for a pairing made before it was kept, until a day is served.
    @ObservationIgnored private var issuer: URL?
    @ObservationIgnored private var generation = 0
    @ObservationIgnored private var fetched: [String: Int] = [:]      // date -> the generation it was served in
    @ObservationIgnored private var inFlight: Set<String> = []
    @ObservationIgnored private var failedAt: [String: Date] = [:]
    @ObservationIgnored private let defaults: UserDefaults

    private typealias Key = BrainStore.Key

    private convenience init() { self.init(defaults: BrainStore.defaults) }
    /// `pairing` stands in for what the Keychain and the defaults hold; a test gives it, the app reads the stores.
    init(defaults: UserDefaults, pairing: (token: String, bases: [URL], issuer: URL?)? = nil) {
        self.defaults = defaults
        if let p = pairing {
            token = p.token; bases = p.bases; issuer = p.issuer; hasToken = true; state = .paired(Self.host(p.bases[0]))
        } else {
            BrainStore.adopt(into: defaults)
            loadStored()
        }
    }

    /// Take the pairing the Keychain and the defaults hold, if there is one and none is held yet.
    @discardableResult private func loadStored() -> Bool {
        guard !hasToken, let t = Keychain.get(Key.token), let a = defaults.stringArray(forKey: Key.addresses) else { return false }
        let urls = a.compactMap(URL.init(string:))
        if urls.isEmpty { return false }
        token = t; bases = urls; hasToken = true
        issuer = defaults.string(forKey: Key.issuer).flatMap(URL.init(string:))
        deviceName = defaults.string(forKey: Key.device)
        state = .paired(Self.host(urls[0]))
        return true
    }

    /// The app came to the foreground. A process started in the background (a location wake, a push) can run before the Keychain and the shared defaults
    /// are readable and find no pairing; it looks again, so the owner is not left in the prototype until the next launch.
    func reloadIfUnpaired() {
        if state != .pairing, loadStored() { dataVersion += 1; openTick += 1; Waiting.shared.start() }
    }

    var isOnline: Bool { if case .paired = state { return true }; return false }
    /// what the settings list calls the brain: its address, as the phone knows it
    var brainName: String {
        switch state {
        case .paired(let h): h
        case .unpaired: "未配对"
        case .pairing: "正在配对"
        case .error: hasToken ? Self.host(bases[0]) : "未配对"
        }
    }
    var latencyText: String { hasToken && isOnline ? latencyMs.map { "\($0) ms" } ?? "—" : "—" }
    /// Where the brain answered last and the token that proves this phone, for the voice link; nil until paired.
    var voiceEndpoint: (base: URL, token: String)? {
        guard let token, let base = bases.first else { return nil }
        return (base, token)
    }
    var statusLine: String {
        switch state {
        case .unpaired: "还没连上大脑"
        case .pairing: "正在配对…"
        case .paired(let h): "连着 \(h) · 在线"
        case .error(let m): hasToken ? "\(Self.host(bases[0])) · 离线 · \(m)" : m
        }
    }
    /// the message of the last failure, for the pairing page
    var errorMessage: String? { if case .error(let m) = state { return m }; return nil }

    static func host(_ u: URL) -> String { u.port.map { "\(u.host ?? ""):\($0)" } ?? (u.host ?? "") }

    /// A call under this phone's token. When the guard refuses the token, `settle` decides whether the brain really unpaired this phone.
    @MainActor func call<T>(_ body: (_ bases: [URL], _ token: String) async throws -> T) async throws -> T {
        guard hasToken, let tok = token else { throw BrainError.revoked }
        let bases = self.bases
        do { return try await body(bases, tok) } catch { throw await settle(error, token: tok) }
    }

    /// The one place a refused token unpairs the phone. A refusal is not trusted alone (a proxy, a brain mid-restart): the brain is asked once more under the
    /// same token, on the daemon that issued it, and only a second refusal forgets the pairing. Otherwise the pairing stays and the caller sees an ordinary failure.
    @MainActor func settle(_ error: Error, token tok: String) async -> Error {
        guard let e = error as? BrainError, e == .revoked else { return error }
        let probe = issuer.map { [$0] } ?? bases
        if await BrainClient.tokenRefused(bases: probe, token: tok), token == tok { BrainClient.trace("forget: the issuing brain refused the token twice"); forget(e.message); return e }
        return BrainError.refused
    }

    // MARK: pairing

    /// `text` is what the QR holds. The brain is asked for a token once; on any failure nothing that was held before changes.
    func pair(scanned text: String) {
        guard let link = PairingLink(text) else { state = .error(BrainError.badLink.message); return }
        state = .pairing
        Task { @MainActor in
            do {
                let got = try await BrainClient.claim(link)
                guard Keychain.set(got.token, Key.token) else { state = .error(BrainError.refused.message); return }
                // the addresses the brain gave, the one that answered first
                let all = [got.base] + link.brain.filter { $0 != got.base }
                defaults.set(all.map(\.absoluteString), forKey: Key.addresses)
                defaults.set(got.device, forKey: Key.device); defaults.set(got.base.absoluteString, forKey: Key.issuer)
                token = got.token; bases = all; issuer = got.base; hasToken = true; deviceName = got.device
                days = [:]; fetched = [:]; failedAt = [:]; generation += 1
                state = .paired(Self.host(got.base))
                dataVersion += 1; openTick += 1      // the days on screen are asked for under the new token
                Waiting.shared.start()
            } catch let e as BrainError {
                state = .error(e.message)
            } catch {
                state = .error(BrainError.refused.message)
            }
        }
    }

    /// The brain no longer knows this phone: drop what it gave and show the prototype again.
    func forget(_ message: String? = nil) {
        Keychain.delete(Key.token)
        defaults.removeObject(forKey: Key.addresses); defaults.removeObject(forKey: Key.device); defaults.removeObject(forKey: Key.issuer)
        token = nil; bases = []; issuer = nil; hasToken = false; deviceName = nil; latencyMs = nil
        days = [:]; fetched = [:]; failedAt = [:]; inFlight = []
        state = message.map { .error($0) } ?? .unpaired
        dataVersion += 1
        Waiting.shared.stop()
    }

    // MARK: days

    /// The app came back from the background: what it shows is asked for again.
    func opened() { generation += 1; openTick += 1 }

    /// Ask for these days (`YYYY-MM-DD`) unless the brain already served them since the app was opened.
    func ensure(_ dates: [String]) {
        guard hasToken, let tok = token else { return }
        let gen = generation, now = Date()
        let want = dates.filter { fetched[$0] != gen && !inFlight.contains($0) && now.timeIntervalSince(failedAt[$0] ?? .distantPast) > 10 }
        if want.isEmpty { return }
        inFlight.formUnion(want)
        let bases = self.bases
        Task { @MainActor in
            // a few at a time: each request folds a whole day on the brain
            for chunk in stride(from: 0, to: want.count, by: 4).map({ Array(want[$0..<min($0 + 4, want.count)]) }) {
                await withTaskGroup(of: (String, Result<(reply: DayReply, base: URL, ms: Int), Error>).self) { g in
                    for d in chunk {
                        g.addTask { (d, await Result { try await BrainClient.day(d, bases: bases, token: tok) }) }
                    }
                    for await (d, r) in g { await apply(d, r, gen) }
                }
            }
        }
    }

    @MainActor private func apply(_ date: String, _ r: Result<(reply: DayReply, base: URL, ms: Int), Error>, _ gen: Int) async {
        inFlight.remove(date)
        guard hasToken, let tok = token else { return }
        switch r {
        case .success(let ok):
            days[date] = ok.reply; fetched[date] = gen; failedAt[date] = nil
            latencyMs = ok.ms
            if issuer == nil { issuer = ok.base; defaults.set(ok.base.absoluteString, forKey: Key.issuer) }      // a served day proves this daemon knows the token
            if ok.base != bases.first { bases = [ok.base] + bases.filter { $0 != ok.base }; defaults.set(bases.map(\.absoluteString), forKey: Key.addresses) }
            state = .paired(Self.host(ok.base))
            dataVersion += 1
        case .failure(let e):
            failedAt[date] = Date()
            let seen = await settle(e, token: tok)
            if hasToken { state = .error((seen as? BrainError)?.message ?? BrainError.refused.message) }      // a forgotten pairing already set its own message
        }
    }

    /// The liveness probe, for the latency in settings.
    func ping() {
        guard hasToken else { return }
        let bases = self.bases
        Task { @MainActor in
            do {
                let r = try await BrainClient.health(bases: bases)
                latencyMs = r.ms; state = .paired(Self.host(r.base))
            } catch let e as BrainError {
                state = .error(e.message)
            } catch {}
        }
    }

    /// Which sources the brain could not read for this day, as the screen says it ("Mac 没连上"); nil when nothing is missing.
    func missingNote(_ date: String) -> String? { Self.missingText(days[date]?.missing ?? []) }

    /// the same words for a list of the sources a day's answer says it could not read
    static func missingText(_ missing: [String]) -> String? {
        let names = ["phone": "手机", "reminders": "提醒", "mac": "Mac", "talks": "对话", "calendar": "日历", "departures": "公交"]
        let m = missing.compactMap { names[$0] }
        return m.isEmpty ? nil : m.joined(separator: "、") + " 没连上"
    }

    #if DEBUG
    /// `-pairLink '<what the QR holds>'` pairs without a camera.
    func pairFromLaunchArgument() {
        let a = ProcessInfo.processInfo.arguments
        if let i = a.firstIndex(of: "-pairLink"), i + 1 < a.count { pair(scanned: a[i + 1]) }
    }
    #endif
}

private extension Result where Failure == Error {
    init(_ body: () async throws -> Success) async {
        do { self = .success(try await body()) } catch { self = .failure(error) }
    }
}

/// Why the pairing was or was not dropped: one line each to the unified log and to Library/Caches/pairing.log. Never the token.
enum PairingTrace {
    private static let log = Logger(subsystem: "com.yilunshi.jarvis", category: "pairing")
    static func note(_ s: String) {
        log.notice("\(s, privacy: .public)")
        StallLog.append(s, to: "pairing.log")
    }
}
