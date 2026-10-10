import Foundation
import Observation

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
    @ObservationIgnored private var generation = 0
    @ObservationIgnored private var fetched: [String: Int] = [:]      // date -> the generation it was served in
    @ObservationIgnored private var inFlight: Set<String> = []
    @ObservationIgnored private var failedAt: [String: Date] = [:]
    @ObservationIgnored private let defaults = UserDefaults.standard

    private enum Key { static let addresses = "brain.addresses", device = "brain.device", token = "brain.token" }

    private init() {
        if let t = Keychain.get(Key.token), let a = defaults.stringArray(forKey: Key.addresses) {
            let urls = a.compactMap(URL.init(string:))
            if !urls.isEmpty {
                token = t; bases = urls; hasToken = true
                deviceName = defaults.string(forKey: Key.device)
                state = .paired(Self.host(urls[0]))
            }
        }
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
                defaults.set(got.device, forKey: Key.device)
                token = got.token; bases = all; hasToken = true; deviceName = got.device
                days = [:]; fetched = [:]; failedAt = [:]; generation += 1
                state = .paired(Self.host(got.base))
                dataVersion += 1; openTick += 1      // the days on screen are asked for under the new token
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
        defaults.removeObject(forKey: Key.addresses); defaults.removeObject(forKey: Key.device)
        token = nil; bases = []; hasToken = false; deviceName = nil; latencyMs = nil
        days = [:]; fetched = [:]; failedAt = [:]; inFlight = []
        state = message.map { .error($0) } ?? .unpaired
        dataVersion += 1
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
                    for await (d, r) in g { apply(d, r, gen) }
                }
            }
        }
    }

    private func apply(_ date: String, _ r: Result<(reply: DayReply, base: URL, ms: Int), Error>, _ gen: Int) {
        inFlight.remove(date)
        guard hasToken else { return }
        switch r {
        case .success(let ok):
            days[date] = ok.reply; fetched[date] = gen; failedAt[date] = nil
            latencyMs = ok.ms
            if ok.base != bases.first { bases = [ok.base] + bases.filter { $0 != ok.base }; defaults.set(bases.map(\.absoluteString), forKey: Key.addresses) }
            state = .paired(Self.host(ok.base))
            dataVersion += 1
        case .failure(let e):
            failedAt[date] = Date()
            if let b = e as? BrainError, b == .revoked { forget(b.message) } else { state = .error((e as? BrainError)?.message ?? BrainError.refused.message) }
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
    func missingNote(_ date: String) -> String? {
        let names = ["phone": "手机", "reminders": "提醒", "mac": "Mac", "talks": "对话"]
        let m = (days[date]?.missing ?? []).compactMap { names[$0] }
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
