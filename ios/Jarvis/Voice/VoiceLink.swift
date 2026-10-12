import Foundation

// The phone's line to the host. The session talks to a VoiceLink; which one is behind it is decided by whether a pairing exists.

/// The host's answer to a say: the turn id (nil unless the verdict is "turn"), the verdict, and the record id of typed words (ADR 0256)
typealias SayResult = (turnId: String?, verdict: String, recordId: String?)

enum LinkEvent: Sendable {
    case ready(device: String, voice: Bool, sampleRate: Int, judges: Bool)
    case row(RowFrame)
    /// one tool call of a turn this device opened, started or ended (ADR 0228)
    case step(StepFrame)
    /// a reaction set or taken back on a message, by him or by her (ADR 0256)
    case reaction(ReactionFrame)
    /// a frame for the player (from the host's binary messages)
    case audio(PlayerIn)
    /// an `error` frame the link does not handle itself (bad_ping, bad_frame, no_voice, interrupt_failed)
    case error(code: String, message: String)
    /// the host refused the token (HTTP 403 on upgrade); nothing is retried until the phone is paired again
    case unpaired
    /// a newer connection of this device took over (close code 4000); not reconnected
    case replaced
    /// the connection dropped; it is being retried
    case closed
}

enum LinkError: Error, LocalizedError {
    case notConnected, timeout, unpaired, host(String)
    /// a typed say's files were refused by the host's store (`attachment_refused`): its own sentence
    case refused(String)
    /// the host takes no files with a say (`no_attachments`)
    case noAttachments
    var errorDescription: String? {
        switch self {
        case .refused(let m): return m
        case .noAttachments: return "这个大脑不收文件"
        case .notConnected: return "没有连上 Jarvis"
        case .timeout: return "Jarvis 没有回应"
        case .unpaired: return "还没有配对"
        case .host(let m): return m
        }
    }
}

@MainActor
protocol VoiceLink: AnyObject {
    var events: AsyncStream<LinkEvent> { get }
    func connect()
    func disconnect()
    /// The host's answer to this utterance: the turn id (nil unless the verdict is "turn") and the verdict. `timeout` seconds without it throws `.timeout`.
    func say(utteranceId: String, text: String, language: VoiceLang?, confidence: Double?, spoken: Bool, overHer: Bool, conversation: Bool,
             about: [String: Any]?, attachments: [String], timeout: Double) async throws -> SayResult
    func barge()
    func cancel()
    /// what the player reports back (READY, REPORT, STATUS, DISCARD_ACK)
    func send(_ frames: [PlayerOut])
}

extension VoiceLink {
    /// a say with no files, as the voice sends it
    func say(utteranceId: String, text: String, language: VoiceLang?, confidence: Double?, spoken: Bool, overHer: Bool, conversation: Bool,
             about: [String: Any]?, timeout: Double) async throws -> SayResult {
        try await say(utteranceId: utteranceId, text: text, language: language, confidence: confidence, spoken: spoken, overHer: overHer,
                      conversation: conversation, about: about, attachments: [], timeout: timeout)
    }
}

/// One URLSessionWebSocketTask to /phone/ws on the host. Reconnects after 1 s, doubling to 30 s; HTTP 403 on upgrade is "unpaired" and close code 4000
/// is "replaced by a newer connection of this device"; neither is retried.
@MainActor
final class WebSocketVoiceLink: VoiceLink {
    static let path = "/phone/ws"
    static let pingEvery: UInt64 = 3_000_000_000
    /// how long a say waits for the connection before it is given up, unsent
    static let connectWait = 20.0

    let events: AsyncStream<LinkEvent>
    private let out: AsyncStream<LinkEvent>.Continuation
    private let endpoint: () -> (base: URL, token: String)?
    private let urlSession = URLSession(configuration: .default)
    private var task: URLSessionWebSocketTask?
    private var wanted = false
    private var backoff = 1.0
    private var generation = 0                       // which connection a late callback belongs to
    private var pending: [String: (c: CheckedContinuation<SayResult, Error>, frame: PhoneFrame)] = [:]
    private var pingTask: Task<Void, Never>?
    private var ready = false                        // the host answered hello: a say sent now is read at once
    private var openedAt = 0.0
    private var stopped = 0                          // barges and cancels so far: a say still waiting for the connection is dropped by one

    init(endpoint: @escaping () -> (base: URL, token: String)?) {
        self.endpoint = endpoint
        (events, out) = AsyncStream.makeStream(of: LinkEvent.self)
    }

    func connect() {
        wanted = true
        if task == nil { open() }
    }

    func disconnect() {
        wanted = false
        close(.notConnected)
    }

    private func open() {
        guard let ep = endpoint() else { return }
        guard var c = URLComponents(url: ep.base, resolvingAgainstBaseURL: false) else { return }
        c.scheme = c.scheme == "http" ? "ws" : c.scheme == "https" ? "wss" : c.scheme
        c.path = Self.path
        guard let url = c.url else { return }
        var req = URLRequest(url: url)
        req.setValue("Bearer \(ep.token)", forHTTPHeaderField: "Authorization")
        let t = urlSession.webSocketTask(with: req)
        VoiceTrace.note("ws opening \(url.scheme ?? "")://\(url.host ?? ""):\(url.port.map(String.init) ?? "")\(url.path)")
        task = t
        openedAt = Date().timeIntervalSince1970
        generation += 1
        let mine = generation
        t.resume()
        sendText(.hello)                                  // the first frame, well inside the host's 5 s
        Task { await receive(t, mine) }
        pingTask = Task { [weak self] in                  // the host maps our clock to its own from these
            while !Task.isCancelled {
                self?.sendText(.ping(tNs: uptimeNs()))
                try? await Task.sleep(nanoseconds: Self.pingEvery)
            }
        }
    }

    private func close(_ why: LinkError) {
        pingTask?.cancel(); pingTask = nil
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
        ready = false
        generation += 1
        for (_, p) in pending { p.c.resume(throwing: why) }
        pending = [:]
    }

    private func receive(_ t: URLSessionWebSocketTask, _ mine: Int) async {
        do {
            while true {
                let m = try await t.receive()
                guard mine == generation else { return }
                backoff = 1
                switch m {
                case .data(let d): if let f = PlayerIn(d) { out.yield(.audio(f)) }
                case .string(let s): if let f = HostFrame(Data(s.utf8)) { handle(f) }
                @unknown default: break
                }
            }
        } catch {
            guard mine == generation else { return }
            let status = (t.response as? HTTPURLResponse)?.statusCode
            let replaced = t.closeCode.rawValue == 4000
            VoiceTrace.note("ws closed: http \(status.map(String.init) ?? "none"), close code \(t.closeCode.rawValue), \((error as NSError).domain) \((error as NSError).code)")
            close(status == 403 ? .unpaired : .notConnected)
            if status == 403 { wanted = false; out.yield(.unpaired); return }
            if replaced { wanted = false; out.yield(.replaced); return }
            out.yield(.closed)
            guard wanted else { return }
            let wait = backoff
            backoff = min(backoff * 2, 30)
            try? await Task.sleep(nanoseconds: UInt64(wait * 1e9))
            if wanted && task == nil { open() }
        }
    }

    private func handle(_ f: HostFrame) {
        switch f {
        case .ready(let d, let v, let r, let j):
            ready = true
            VoiceTrace.note("ws ready \(Int((Date().timeIntervalSince1970 - openedAt) * 1000)) ms after opening")
            out.yield(.ready(device: d, voice: v, sampleRate: r, judges: j))
        case .said(let u, let turn, let verdict, let rec): VoiceTrace.note("ws said frame: \(verdict)"); pending.removeValue(forKey: u)?.c.resume(returning: (turn, verdict, rec))
        case .row(let r): out.yield(.row(r))
        case .step(let f): out.yield(.step(f))
        case .reaction(let f): out.yield(.reaction(f))
        case .pong: break
        case .error("retry", _):                      // the host could not write it down: send the same say again
            Task { [weak self, mine = generation] in
                try? await Task.sleep(nanoseconds: 500_000_000)
                guard let self, mine == self.generation else { return }
                for (_, p) in self.pending { self.sendText(p.frame) }
            }
        case .error("bad_say", let m):
            for (_, p) in pending { p.c.resume(throwing: LinkError.host(m)) }
            pending = [:]
        case .error("attachment_refused", let m): failPending(withFiles: .refused(m))
        case .error("no_attachments", _): failPending(withFiles: .noAttachments)
        case .error(let c, let m): out.yield(.error(code: c, message: m))
        }
    }

    /// The host answers a refused file with an `error` frame that names no utterance: it is the say that carried files.
    private func failPending(withFiles why: LinkError) {
        for (id, p) in pending {
            guard case .say(_, _, _, _, _, _, _, _, let files) = p.frame, !files.isEmpty else { continue }
            pending.removeValue(forKey: id)
            p.c.resume(throwing: why)
        }
    }

    private func sendText(_ f: PhoneFrame) {
        guard let task, let s = String(data: f.json, encoding: .utf8) else { return }
        task.send(.string(s)) { _ in }
    }

    func say(utteranceId: String, text: String, language: VoiceLang?, confidence: Double?, spoken: Bool, overHer: Bool, conversation: Bool,
             about: [String: Any]?, attachments: [String], timeout: Double) async throws -> SayResult {
        if task == nil { connect() }
        // Not sent before the host is there: a say queued on a socket still connecting reached the host after the phone had given
        // up on it, and opened a turn nobody was waiting for. The answer's clock starts once it is sent.
        let waitUntil = Date().timeIntervalSince1970 + Self.connectWait, stoppedAt = stopped
        while !ready {
            guard task != nil, stopped == stoppedAt, Date().timeIntervalSince1970 < waitUntil else { throw LinkError.notConnected }
            try await Task.sleep(nanoseconds: 100_000_000)
        }
        let frame = PhoneFrame.say(utteranceId: utteranceId, text: text, language: language?.code, confidence: confidence, spoken: spoken,
                                   overHer: overHer, conversation: conversation,
                                   about: about.flatMap { try? JSONSerialization.data(withJSONObject: $0, options: [.sortedKeys]) }, attachments: attachments)
        return try await withCheckedThrowingContinuation { c in
            pending[utteranceId] = (c, frame)
            sendText(frame)
            Task { [weak self] in
                try? await Task.sleep(nanoseconds: UInt64(timeout * 1e9))
                self?.pending.removeValue(forKey: utteranceId)?.c.resume(throwing: LinkError.timeout)
            }
        }
    }

    func barge() { stopped += 1; sendText(.barge) }
    func cancel() { stopped += 1; sendText(.cancel) }

    func send(_ frames: [PlayerOut]) {
        guard let task else { return }
        for f in frames { task.send(.data(f.bytes)) { _ in } }
    }
}
