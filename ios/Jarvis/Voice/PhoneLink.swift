import Foundation
import UIKit

// The one line to the host (/phone/ws). The voice and the typed words both go over it, and the steps of every turn come back on it; it is open while the app is
// on screen, and a turn in flight keeps it a little longer.

/// How a typed send ended.
enum TypedSend: Equatable {
    /// the host took the words as a turn; `turn` is its id (nil when it answered without one), `record` the conversation record of the words (ADR 0256; nil from an older host)
    case taken(turn: String?, record: String? = nil)
    /// this host cannot take the say (no socket, an older brain that refuses the fields): POST /inherent/submit instead
    case useSubmit
    /// the words did not go; the sentence says why
    case refused(String)

    /// What an error of the say means. An `error` frame about the new fields is an older brain (it never saw those fields), not a failure of the words.
    static func of(_ error: Error) -> TypedSend {
        switch error as? LinkError {
        case .refused(let m): return .refused(m)
        case .noAttachments, .host, .notConnected, .unpaired, .timeout: return .useSubmit          // no answer is not a refusal: the words may be there, and the other route carries the same id
        case nil: return .refused(LinkError.timeout.errorDescription ?? "Jarvis 没有回应")
        }
    }
}

@MainActor
final class PhoneLink {
    static let shared = PhoneLink()

    /// Where the host is and the token that proves this phone; the pairing owns both. Nil until it is paired: the local stub answers.
    var endpoint: () -> (base: URL, token: String)? = { nil } { didSet { connectIfOnScreen() } }
    /// how long a typed send waits for the line before it goes by HTTP instead
    static let typedWait = 3.0

    private(set) var link: VoiceLink?
    private(set) var ready = false
    private var linkIsReal = false
    private var pump: Task<Void, Never>?
    private var sinks: [Int: AsyncStream<LinkEvent>.Continuation] = [:]
    private var nextSink = 0
    private var foreground = true
    private let board: StatusBoard
    private let make: ((_ real: Bool) -> VoiceLink)?

    /// `make` stands in for the websocket and the stub (the tests' line)
    init(board: StatusBoard = .shared, make: ((_ real: Bool) -> VoiceLink)? = nil) {
        self.board = board
        self.make = make
        foreground = UIApplication.shared.applicationState != .background
        hop(UIApplication.didEnterBackgroundNotification) { $0.foreground = false }
        hop(UIApplication.willEnterForegroundNotification) { $0.foreground = true; $0.connectIfOnScreen() }
    }

    private func hop(_ name: Notification.Name, _ body: @escaping @MainActor (PhoneLink) -> Void) {
        _ = NotificationCenter.default.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { if let self { body(self) } }
        }
    }

    /// Every event of the line from now on, whichever link is behind it. The steps and the end of a turn are already in the board when it arrives.
    func events() -> AsyncStream<LinkEvent> {
        let id = nextSink
        nextSink += 1
        let (stream, c) = AsyncStream.makeStream(of: LinkEvent.self)
        sinks[id] = c
        c.onTermination = { [weak self] _ in Task { @MainActor in self?.sinks[id] = nil } }
        return stream
    }

    /// The line, made if it is not there: the websocket once the phone is paired, else the stub. Switching between them drops the old one.
    @discardableResult func ensure() -> VoiceLink {
        let real = endpoint() != nil
        if let link, real == linkIsReal { link.connect(); return link }
        pump?.cancel()
        link?.disconnect()
        VoiceTrace.note(real ? "link: websocket to \(endpoint().map { ($0.base.scheme ?? "") + "://" + (BrainLink.host($0.base)) } ?? "?")" : "link: local stub (not paired)")
        let l: VoiceLink
        if let make { l = make(real) } else { l = real ? WebSocketVoiceLink(endpoint: endpoint) : LocalStubLink() }
        link = l
        linkIsReal = real
        ready = false
        l.connect()
        pump = Task { for await e in l.events { handle(e) } }
        return l
    }

    /// Over Tailscale the line has taken 13-15 s to connect, and a say waits for it: it is made as the app comes on screen, and stays while it is (the host reads
    /// that as "the app is open", ADR 0210).
    func connectIfOnScreen() { if foreground, endpoint() != nil { ensure() } }

    /// The app is in the background and nothing is using the line: it is let go. (A voice answer still playing keeps it; the session calls this when it is idle.)
    func settle() {
        guard !foreground else { return }
        pump?.cancel(); pump = nil
        link?.disconnect(); link = nil
        ready = false
    }

    private func handle(_ e: LinkEvent) {
        switch e {
        case .ready: ready = true
        case .closed, .unpaired, .replaced: ready = false
        case .reaction(let f): Reactions.shared.apply(f)
        default: break
        }
        board.feed(e)
        for c in sinks.values { c.yield(e) }
    }

    /// A say through the line. The board knows the turn from here: its steps, rows and end are kept while the host answers. Words that may not be a turn at all (his
    /// words over her, a follow-up) are a turn for the board only once the host says so.
    func say(utteranceId: String, text: String, language: VoiceLang?, confidence: Double?, spoken: Bool, overHer: Bool, conversation: Bool,
             about: [String: Any]?, attachments: [String] = [], timeout: Double) async throws -> SayResult {
        let l = ensure()
        let judged = overHer || conversation
        if !judged { board.begin(utterance: utteranceId) }
        let r = try await l.say(utteranceId: utteranceId, text: text, language: language, confidence: confidence, spoken: spoken, overHer: overHer,
                                conversation: conversation, about: about, attachments: attachments, timeout: timeout)
        if r.verdict == "turn", let turn = r.turnId {
            if judged { board.begin(utterance: utteranceId) }
            board.bind(utterance: utteranceId, turn: turn)
        } else {
            board.end(utterance: utteranceId)
        }
        return r
    }

    /// Typed words and their files over the line, if it is there (or comes within `typedWait`): the host answers `said` at once. A say that gets no answer is sent
    /// once more (the host knows it by its id and answers the same turn), and after the second try the words go by POST /inherent/submit under the same id (ADR 0262);
    /// an `error` about the fields means an older brain.
    func sendTyped(utterance: String, text: String, attachments: [String], about: [String: Any]?) async -> TypedSend {
        guard endpoint() != nil else { return .useSubmit }
        ensure()
        let until = Date().timeIntervalSince1970 + Self.typedWait
        while !ready, Date().timeIntervalSince1970 < until { try? await Task.sleep(nanoseconds: 50_000_000) }
        guard ready else { return .useSubmit }
        var last: TypedSend = .useSubmit
        for timeout in [6.0, 10.0] {
            do {
                let r = try await say(utteranceId: utterance, text: text, language: nil, confidence: nil, spoken: false, overHer: false, conversation: false,
                                      about: about, attachments: attachments, timeout: timeout)
                return .taken(turn: r.turnId, record: r.recordId)
            } catch LinkError.timeout {
                VoiceTrace.note("typed say unanswered: sent again")
                continue
            } catch {
                last = TypedSend.of(error)
                break
            }
        }
        return last
    }
}
