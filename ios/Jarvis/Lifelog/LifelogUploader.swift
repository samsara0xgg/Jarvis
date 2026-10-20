import CryptoKit
import Foundation
import Observation

/// What to do with a piece after the brain's answer (or none).
enum LifelogVerdict: Equatable {
    /// the brain holds the same bytes: delete the piece
    case delete
    /// try again later on the growing delay (`LifelogUploader.backoffMs`)
    case backoff
    /// try again after this many milliseconds
    case wait(Int64)
    /// never send again, never delete: moved to held/<why>/
    case hold(String)
    /// the brain does not accept this phone's token: keep, and tell him
    case repair
}

/// Sends closed pieces to the paired brain (PUT /inherent/lifelog/audio/{id}, or /motion/{id} for a ride's device motion) on a background session,
/// so a piece finishes after the app is suspended or killed. One at a time, oldest first, on Wi-Fi only. A piece is deleted only when the brain answers with the sha256 of the same bytes.
@MainActor
@Observable
final class LifelogUploader {
    static let shared = LifelogUploader()

    /// recordings closed and not yet in the brain, for the settings line (a ride's motion is not a recording he turned on)
    private(set) var waiting = 0
    /// the brain refused this pairing's token; clears by itself when he pairs again
    private(set) var needsRepair = false

    @ObservationIgnored private var _session: URLSession?
    @ObservationIgnored private let delegate = Delegate()
    @ObservationIgnored private var busy = false
    @ObservationIgnored private var wake: Task<Void, Never>?
    /// iOS's handler for a relaunch to deliver the session's events; called once they are all handled
    @ObservationIgnored var backgroundCompletion: (() -> Void)?

    static let sessionID = "com.yilunshi.jarvis.lifelog"
    private static let repairKey = "lifelog.repairFor"
    nonisolated private static let maxBackoffMs: Int64 = 3_600_000

    // MARK: policy (pure)

    /// The brain's answer to a PUT, as the doc of the wire gives it (docs/spec.html, lifelog audio).
    nonisolated static func verdict(status: Int?, body: Data?, localSha: String) -> LifelogVerdict {
        switch status {
        case 200, 201:
            let sha = body.flatMap { try? JSONDecoder().decode(Reply.self, from: $0) }?.sha256
            return sha?.lowercased() == localSha.lowercased() ? .delete : .backoff
        case 507: return .wait(3 * 3_600_000)           // the brain's disk floor: a long pause
        case 409: return .hold("conflict")
        case 400, 411, 413, 415, 422: return .hold("rejected")
        case 401, 403: return .repair
        default: return .backoff                        // no answer, a transport error, any other 5xx
        }
    }

    /// 1 minute after the first failure, doubling, at most 1 hour.
    nonisolated static func backoffMs(attempts: Int) -> Int64 {
        min(maxBackoffMs, 60_000 << min(max(attempts - 1, 0), 10))
    }

    private struct Reply: Decodable { let sha256: String }

    // MARK: sending

    /// A piece was closed, the app came to the foreground, or the pairing changed: send what is due. After a refusal of the token,
    /// only `foreground` tries again (the owner may have paired anew; a new token clears the refusal anyway).
    func kick(foreground: Bool = false) {
        refresh()
        guard !busy, let ep = endpoint else { return }
        if needsRepair && !foreground { return }
        busy = true
        Task {
            defer { busy = false }
            await sendNext(ep)
        }
    }

    /// the pieces waiting and whether the token was refused, from the outbox and the defaults
    func refresh() {
        waiting = LifelogStore.waiting().filter { $0.kind == nil }.count
        needsRepair = endpoint.map { UserDefaults.standard.string(forKey: Self.repairKey) == Self.fingerprint($0.token) } ?? false
    }

    private var endpoint: (base: URL, token: String)? {
        #if DEBUG
        // `-lifelogStubURL http://127.0.0.1:PORT -lifelogStubToken x` sends to a local stub server, never the brain
        let d = UserDefaults.standard
        if let base = d.string(forKey: "lifelogStubURL").flatMap(URL.init(string:)), let token = d.string(forKey: "lifelogStubToken") { return (base, token) }
        #endif
        return BrainLink.shared.voiceEndpoint
    }

    /// not the token, only enough of its hash to tell one pairing from another
    private static func fingerprint(_ token: String) -> String {
        SHA256.hash(data: Data(token.utf8)).prefix(8).map { String(format: "%02x", $0) }.joined()
    }

    private var session: URLSession {
        if let s = _session { return s }
        let c = URLSessionConfiguration.background(withIdentifier: Self.sessionID)
        c.allowsExpensiveNetworkAccess = false        // Wi-Fi only: a piece waits for it, it does not spend his data
        c.allowsConstrainedNetworkAccess = false
        c.sessionSendsLaunchEvents = true
        c.httpMaximumConnectionsPerHost = 1
        let s = URLSession(configuration: c, delegate: delegate, delegateQueue: nil)
        _session = s
        return s
    }

    private func sendNext(_ ep: (base: URL, token: String)) async {
        // one at a time: a piece still in flight (this launch's or an earlier one's) is waited for; its answer kicks the next
        if !(await session.allTasks).isEmpty { return }
        let nowMs = Int64(Date().timeIntervalSince1970 * 1000)
        let pieces = LifelogStore.waiting()
        for p in pieces where !FileManager.default.fileExists(atPath: p.file.path) {
            Self.log("\(p.id) file is gone, dropping its record")
            try? FileManager.default.removeItem(at: p.sidecar)
        }
        guard let p = pieces.first(where: { FileManager.default.fileExists(atPath: $0.file.path) }) else { return refresh() }
        guard p.nextTryMs <= nowMs else { return scheduleWake(at: p.nextTryMs) }
        var url = ep.base.appendingPathComponent("inherent/lifelog/\(p.route)/\(p.id)")
        url.append(queryItems: [.init(name: "started_ms", value: String(p.startedMs)), .init(name: "duration_ms", value: String(p.durationMs))])
        var req = URLRequest(url: url)
        req.httpMethod = "PUT"
        req.setValue("Bearer \(ep.token)", forHTTPHeaderField: "Authorization")
        req.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
        let task = session.uploadTask(with: req, fromFile: p.file)
        task.taskDescription = p.id
        task.resume()
        Self.log("\(p.id) sending \(p.durationMs / 1000)s")
    }

    /// An answer (or a failure) for the piece `id`: the verdict is applied to its files.
    fileprivate func finished(id: String, status: Int?, body: Data?) async {
        guard let p = LifelogStore.waiting().first(where: { $0.id == id }) else { return refresh() }
        let local = await Task.detached { Self.sha256Hex(of: p.file) }.value
        let v = local.map { Self.verdict(status: status, body: body, localSha: $0) } ?? .backoff
        let fm = FileManager.default
        var next = p
        switch v {
        case .delete:
            try? fm.removeItem(at: p.file); try? fm.removeItem(at: p.sidecar)
            UserDefaults.standard.removeObject(forKey: Self.repairKey)      // an answer under this token: it is accepted after all
            Self.log("\(id) stored (\(status ?? 0))")
        case .backoff, .wait:
            var delay: Int64
            if case .wait(let ms) = v { delay = ms } else { next.attempts += 1; delay = Self.backoffMs(attempts: next.attempts) }
            next.nextTryMs = Int64(Date().timeIntervalSince1970 * 1000) + delay
            try? next.save()
            Self.log("\(id) kept, status \(status.map(String.init) ?? "none"), next try in \(delay / 1000)s")
            scheduleWake(at: next.nextTryMs)
        case .hold(let why):
            let dir = LifelogStore.ensure(LifelogStore.heldDir(why))
            for f in [p.file, p.sidecar] {
                let to = dir.appendingPathComponent(f.lastPathComponent)
                try? fm.removeItem(at: to); try? fm.moveItem(at: f, to: to)
            }
            Self.log("\(id) held as \(why), status \(status.map(String.init) ?? "none")")
        case .repair:
            if let t = endpoint?.token { UserDefaults.standard.set(Self.fingerprint(t), forKey: Self.repairKey) }
            Self.log("\(id) kept, the brain refused the token (\(status ?? 0))")
        }
        refresh()
        switch v { case .delete, .hold: kick() default: break }      // the piece is off the queue: the next one goes
    }

    /// Back in this process at the time the earliest backed-off piece is due. (A suspended app is not woken by this: the next
    /// recording piece or foreground kicks instead.)
    private func scheduleWake(at ms: Int64) {
        wake?.cancel()
        let secs = max(1, Double(ms) / 1000 - Date().timeIntervalSince1970)
        wake = Task { try? await Task.sleep(for: .seconds(secs)); if !Task.isCancelled { kick() } }
    }

    /// iOS relaunched the app for the session's events: the session is made again under its identifier so its delegate hears them.
    func handleBackgroundEvents(completion: @escaping () -> Void) {
        backgroundCompletion = completion
        _ = session
    }

    fileprivate func eventsDone() {
        backgroundCompletion?(); backgroundCompletion = nil
    }

    nonisolated static func sha256Hex(of url: URL) -> String? {
        guard let h = try? FileHandle(forReadingFrom: url) else { return nil }
        defer { try? h.close() }
        var hasher = SHA256()
        while let d = try? h.read(upToCount: 1 << 20), !d.isEmpty { hasher.update(data: d) }
        return hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }

    /// No token, no address: only the piece's id and the outcome.
    nonisolated static func log(_ s: String) { StallLog.append("lifelog upload: \(s)", to: "lifelog.log") }

    /// The session's delegate: collects each task's body, and hands the finished task to the uploader on the main actor.
    private final class Delegate: NSObject, URLSessionDataDelegate, @unchecked Sendable {
        private let lock = NSLock()
        private var bodies: [Int: Data] = [:]

        func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
            lock.lock(); bodies[dataTask.taskIdentifier, default: Data()].append(data); lock.unlock()
        }

        func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
            lock.lock(); let body = bodies.removeValue(forKey: task.taskIdentifier); lock.unlock()
            let status = error == nil ? (task.response as? HTTPURLResponse)?.statusCode : nil
            guard let id = task.taskDescription else { return }
            Task { @MainActor in await LifelogUploader.shared.finished(id: id, status: status, body: body) }
        }

        func urlSessionDidFinishEvents(forBackgroundURLSession session: URLSession) {
            Task { @MainActor in LifelogUploader.shared.eventsDone() }
        }
    }
}
