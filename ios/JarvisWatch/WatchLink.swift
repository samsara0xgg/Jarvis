import Foundation
import WatchConnectivity
import os

// The watch's end of the link to the phone (WatchConnectivity). The watch reaches the brain only through the paired phone, which sends the day with `updateApplicationContext`
// and answers what is asked here. This adopts each day into `WatchModel`, carries a talk (the microphone to the phone, her answer back as text) and an answer to what waits.

@MainActor
final class WatchLink: NSObject, WCSessionDelegate {
    static let shared = WatchLink()

    private var session: WCSession { .default }
    private var started = false
    private var talk: UUID?                         // the talk going on, or the last one; what the phone tells of another is old
    private lazy var mic = WatchMic { WCSession.default.sendMessageData($0, replyHandler: nil, errorHandler: nil) }

    #if DEBUG
    private static let log = Logger(subsystem: "com.yilunshi.jarvis.watch", category: "watchlink")
    private func trace(_ s: String) { Self.log.notice("\(s, privacy: .public)") }
    #else
    private func trace(_ s: String) {}
    #endif

    func start() {
        guard !started, WCSession.isSupported() else { return }
        started = true
        let model = WatchModel.shared
        model.startTalk = { [weak self] in self?.beginTalk() }
        model.endTalk = { [weak self] in self?.endTalk() }
        model.cancelTalk = { [weak self] in self?.cancelTalk() }
        model.answer = { [weak self] id, action in self?.post(.answer(id: id, action: action)) }
        model.mark = { [weak self] in self?.post(.mark(at: .now, label: nil)) }
        model.unmark = { [weak self] day, id in self?.post(.unmark(day: day, id: id)) }
        session.delegate = self
        session.activate()
        adopt(session.receivedApplicationContext)
    }

    // MARK: the day

    private func adopt(_ context: [String: Any]) {
        guard let day = WatchWire.decode(WatchDay.self, context) else { return }
        WatchModel.shared.adopt(day)
        trace("day adopted: \(day.blocks.count) blocks, \(day.waiting.count) waiting, \(day.next.count) next")
    }

    private func reachability() {
        let reachable = session.activationState == .activated && session.isReachable
        WatchModel.shared.phoneReachable = reachable
        if reachable {
            ask(.refresh)
            #if DEBUG
            autoTalk()
            #endif
        }
    }

    // MARK: asking the phone

    private func ask(_ a: WatchAsk) {
        if session.isReachable { session.sendMessage(WatchWire.encode(a), replyHandler: nil, errorHandler: nil) }
    }

    /// An answer (and a 标一下, and its 撤销) is never lost: when the phone cannot be reached it is queued, and the phone gets it when it can.
    private func post(_ ask: WatchAsk) {
        let a = WatchWire.encode(ask)
        if session.isReachable {
            session.sendMessage(a, replyHandler: nil, errorHandler: { [weak self] _ in Task { @MainActor in self?.session.transferUserInfo(a) } })
        } else {
            session.transferUserInfo(a)
        }
    }

    // MARK: a talk

    private func beginTalk() {
        let id = UUID()
        talk = id
        guard session.isReachable else { WatchModel.shared.tell(.failed(talk: id, reason: Words.Relay.phoneAway)); return }
        WatchModel.shared.talk = .listening(heard: "")
        ask(.talkStart(id: id))
        mic.start(talk: id) { Task { @MainActor in WatchLink.shared.micFailed(id) } }
    }

    private func micFailed(_ id: UUID) {
        guard talk == id else { return }
        ask(.talkCancel(id: id))
        WatchModel.shared.tell(.failed(talk: id, reason: Words.Relay.micOff))
    }

    /// he is done: what the microphone still holds goes first, then the phone is told
    private func endTalk() {
        guard let id = talk else { return }
        mic.stop(flush: true)
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 200_000_000)
            ask(.talkEnd(id: id))
        }
    }

    private func cancelTalk() {
        guard let id = talk else { return }
        mic.stop()
        ask(.talkCancel(id: id))
        talk = nil
    }

    /// what the phone says of the talk; of another talk than this one it is old
    private func hear(_ t: WatchTell) {
        let id: UUID
        switch t {
        case .heard(let i, _, _), .thinking(let i), .reply(let i, _, _, _), .failed(let i, _): id = i
        case .marked, .markMissed: WatchModel.shared.tell(t); trace("phone told: mark"); return          // 标一下's answer, of no talk
        }
        guard id == talk else { return }
        switch t {
        case .thinking, .reply, .failed: mic.stop()             // the phone has what it needs
        case .heard, .marked, .markMissed: break
        }
        WatchModel.shared.tell(t)
        switch t {
        case .heard: trace("phone told: heard")
        case .thinking: trace("phone told: thinking")
        case .reply: trace("phone told: reply")
        case .failed: trace("phone told: failed")
        case .marked, .markMissed: break
        }
    }

    #if DEBUG
    private var autoTalked = false
    /// `-watchAutoTalk 1`: a talk begins by itself once the phone can be reached, and ends after three seconds, for a look at the round trip without a finger
    private func autoTalk() {
        guard !autoTalked, UserDefaults.standard.bool(forKey: "watchAutoTalk") else { return }
        autoTalked = true
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 1_500_000_000)
            beginTalk()
            try? await Task.sleep(nanoseconds: 3_000_000_000)
            endTalk()
        }
    }
    #endif

    // MARK: WCSessionDelegate

    nonisolated func session(_ session: WCSession, activationDidCompleteWith state: WCSessionActivationState, error: Error?) {
        Task { @MainActor in
            adopt(session.receivedApplicationContext)
            reachability()
        }
    }
    nonisolated func sessionReachabilityDidChange(_ session: WCSession) { Task { @MainActor in reachability() } }
    nonisolated func session(_ session: WCSession, didReceiveApplicationContext context: [String: Any]) { Task { @MainActor in adopt(context) } }
    nonisolated func session(_ session: WCSession, didReceiveMessage message: [String: Any]) {
        if let t = WatchWire.decode(WatchTell.self, message) { Task { @MainActor in hear(t) } }
    }
    nonisolated func session(_ session: WCSession, didReceiveUserInfo userInfo: [String: Any]) {
        if let t = WatchWire.decode(WatchTell.self, userInfo) { Task { @MainActor in hear(t) } }
    }
}
