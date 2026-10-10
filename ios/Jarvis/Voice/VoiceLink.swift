import Foundation

// The phone's line to the host. The session talks to a VoiceLink; which one is behind it is decided by whether a pairing exists.

enum LinkEvent: Sendable {
    case ready(device: String, voice: String, sampleRate: Int)
    case row(RowFrame)
    /// a frame for the player (from the host's binary messages)
    case audio(PlayerIn)
    case error(String)
    /// the host refused the token; nothing is retried until the phone is paired again
    case unpaired
    case pong(Int64?)
    /// the connection dropped; it is being retried
    case closed
}

enum LinkError: Error, LocalizedError {
    case notConnected, timeout, unpaired
    var errorDescription: String? {
        switch self {
        case .notConnected: return "没有连上 Jarvis"
        case .timeout: return "Jarvis 没有回应"
        case .unpaired: return "还没有配对"
        }
    }
}

@MainActor
protocol VoiceLink: AnyObject {
    var events: AsyncStream<LinkEvent> { get }
    func connect()
    func disconnect()
    /// The turn id the host gives this utterance.
    func say(utteranceId: String, text: String, language: VoiceLang?, spoken: Bool) async throws -> String
    func barge()
    func cancel()
    /// what the player reports back (READY, REPORT, STATUS, DISCARD_ACK)
    func send(_ frames: [PlayerOut])
}

/// One URLSessionWebSocketTask to the host. Reconnects after 1 s, doubling to 30 s; a 401 or 403 is "unpaired" and stops.
@MainActor
final class WebSocketVoiceLink: VoiceLink {
    static let path = "/phone/ws"      // TODO(ADR 0209): confirm path

    let events: AsyncStream<LinkEvent>
    private let out: AsyncStream<LinkEvent>.Continuation
    private let endpoint: () -> (base: URL, token: String)?
    private let urlSession = URLSession(configuration: .default)
    private var task: URLSessionWebSocketTask?
    private var wanted = false
    private var backoff = 1.0
    private var generation = 0                       // which connection a late callback belongs to
    private var pending: [String: CheckedContinuation<String, Error>] = [:]
    private var pingTask: Task<Void, Never>?

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
        task = t
        generation += 1
        let mine = generation
        t.resume()
        sendText(.hello)
        Task { await receive(t, mine) }
        pingTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 20_000_000_000)
                self?.sendText(.ping(tNs: uptimeNs()))
            }
        }
    }

    private func close(_ why: LinkError) {
        pingTask?.cancel(); pingTask = nil
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
        generation += 1
        for (_, c) in pending { c.resume(throwing: why) }
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
            close(status == 401 || status == 403 ? .unpaired : .notConnected)
            if status == 401 || status == 403 { wanted = false; out.yield(.unpaired); return }
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
        case .ready(let d, let v, let r): out.yield(.ready(device: d, voice: v, sampleRate: r))
        case .said(let u, let turn): pending.removeValue(forKey: u)?.resume(returning: turn)
        case .row(let r): out.yield(.row(r))
        case .pong(let t): out.yield(.pong(t))
        case .error(let m): out.yield(.error(m))
        }
    }

    private func sendText(_ f: PhoneFrame) {
        guard let task, let s = String(data: f.json, encoding: .utf8) else { return }
        task.send(.string(s)) { _ in }
    }

    func say(utteranceId: String, text: String, language: VoiceLang?, spoken: Bool) async throws -> String {
        if task == nil { connect() }
        guard task != nil else { throw LinkError.notConnected }
        return try await withCheckedThrowingContinuation { c in
            pending[utteranceId] = c
            sendText(.say(utteranceId: utteranceId, text: text, language: language?.code, spoken: spoken))
            Task { [weak self] in
                try? await Task.sleep(nanoseconds: 10_000_000_000)
                self?.pending.removeValue(forKey: utteranceId)?.resume(throwing: LinkError.timeout)
            }
        }
    }

    func barge() { sendText(.barge) }
    func cancel() { sendText(.cancel) }

    func send(_ frames: [PlayerOut]) {
        guard let task else { return }
        for f in frames { task.send(.data(f.bytes)) { _ in } }
    }
}
