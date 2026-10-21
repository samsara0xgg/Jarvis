import ActivityKit
import Foundation
import Observation
import UIKit
import WatchConnectivity
import os

// The phone's end of the link to the watch. The watch reaches the brain only through this phone (watchOS has no VPN, the brain answers only on the tailnet): the phone keeps the
// watch's day up to date with `updateApplicationContext`, answers what is answered on the wrist through its own paths, and turns a talk from the wrist into a turn on the brain.
// The wrist reports nothing of its own, and her answers go back as text only: nothing here plays a sound or opens the phone's microphone.

@MainActor
final class WatchRelay: NSObject, WCSessionDelegate {
    static let shared = WatchRelay()

    private var session: WCSession { .default }
    private var started = false
    private var sent: WatchDay?                                 // the last day pushed
    private var debounce: Task<Void, Never>?
    private var followed: Set<String> = []                      // the ride activities watched

    // what he has just said on the wrist that the brain's next answer will say too (as the timeline holds its own: `TimelineState.ackHolds`, `commitHolds`)
    private var acked: [String: Date] = [:], confirmed: [String: Date] = [:], dropped: [String: Date] = [:]

    // the talk going on, if any. The audio callback of WatchConnectivity feeds it from its own thread, in the order the messages came;
    // chunks that come before their talk exists (its start hops to the main actor) are held and fed first when it does.
    private var talk: WatchTalk?
    private var finishing = false
    private var backgroundTask = UIBackgroundTaskIdentifier.invalid
    private struct Feed { var talk: WatchTalk?; var early: [(UUID, Data)] = [] }
    private nonisolated let feeding = OSAllocatedUnfairLock(initialState: Feed())

    #if DEBUG
    private static let log = Logger(subsystem: "com.yilunshi.jarvis", category: "watchrelay")
    /// `-watchFixture 1`: the watch gets the made-up day at 21:50, not the brain's; `-watchStubBrain 1`: a talk is answered here with a made-up answer, and the brain is never asked
    static let fixture = UserDefaults.standard.bool(forKey: "watchFixture")
    static let stub = UserDefaults.standard.bool(forKey: "watchStubBrain")
    private lazy var fixtureDay = TimelineFixture.today(Cal.cur, doing: true)
    private func trace(_ s: String) { Self.log.notice("\(s, privacy: .public)") }
    #else
    static let fixture = false, stub = false
    private func trace(_ s: String) {}
    #endif

    // MARK: start

    /// Once, when the app starts.
    func start() {
        guard !started, WCSession.isSupported() else { return }
        started = true
        session.delegate = self
        session.activate()
        arm()
        watchRides()
        Task { @MainActor in                                    // the minute turns: "还有 10 分钟" says 9
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: UInt64((60 - Date().timeIntervalSince1970.truncatingRemainder(dividingBy: 60)) * 1e9) + 500_000_000)
                push()
            }
        }
        BrainLink.shared.ensure([Cal.cur.iso(floorDay(NOW1))])
    }

    // MARK: the day

    /// What the day is made of, watched: when any of it changes the watch gets a new one (a second after the last change, so a burst of them is one push).
    private func arm() {
        withObservationTracking {
            _ = BrainLink.shared.days; _ = BrainLink.shared.hasToken; _ = Waiting.shared.cards; _ = Words.shared.language
        } onChange: { [weak self] in
            Task { @MainActor in self?.arm(); self?.schedule() }
        }
    }

    private func watchRides() {
        Task { @MainActor in for await a in ActivityKit.Activity<RideAttributes>.activityUpdates { follow(a) } }
        for a in ActivityKit.Activity<RideAttributes>.activities { follow(a) }
    }

    private func follow(_ a: ActivityKit.Activity<RideAttributes>) {
        guard followed.insert(a.id).inserted else { return }
        Task { @MainActor in for await _ in a.contentUpdates { schedule() } }
        Task { @MainActor in for await _ in a.activityStateUpdates { schedule() } }
        schedule()
    }

    private func schedule() {
        debounce?.cancel()
        debounce = Task { @MainActor in
            try? await Task.sleep(nanoseconds: 1_000_000_000)
            if !Task.isCancelled { push() }
        }
    }

    /// today's day, from the brain's (or the made-up one), for the watch; none yet: nothing to say
    private func makeDay() -> WatchDay? {
        let now = Date()
        acked = acked.filter { $0.value > now }; confirmed = confirmed.filter { $0.value > now }; dropped = dropped.filter { $0.value > now }
        let done = TLDone(confirmed: Set(confirmed.keys), dropped: Set(dropped.keys))
        let riding = ActivityKit.Activity<RideAttributes>.activities.first { $0.activityState == .active && !$0.content.state.isEnded }
            .map { WatchDayMaker.Riding(route: $0.attributes.route, board: $0.attributes.boardStop, state: $0.content.state) }
        #if DEBUG
        if Self.fixture {
            return WatchDayMaker.make(fixtureDay, minute: TimelineFixture.now, made: now, cards: Waiting.shared.cards, riding: riding, acked: Set(acked.keys), done: done)
        }
        #endif
        guard BrainLink.shared.hasToken, let r = BrainLink.shared.days[Cal.cur.iso(floorDay(NOW1))] else { return nil }
        let minute = TLClock(r).minute(now)
        return WatchDayMaker.make(r, minute: minute >= 0 && minute < DAY ? minute : nil, made: now, cards: Waiting.shared.cards, riding: riding, acked: Set(acked.keys), done: done)
    }

    /// Send the day when it is not the one the watch has. `now` and `made` are how the watch finds the minute it is (it counts on from them), so they do not make a new day.
    private func push(force: Bool = false) {
        guard session.activationState == .activated, session.isPaired, session.isWatchAppInstalled, let day = makeDay() else { return }
        func same(_ a: WatchDay?, _ b: WatchDay) -> Bool { a.map { var x = $0; x.now = b.now; x.made = b.made; return x == b } ?? false }
        guard force || !same(sent, day) else { return }
        sent = day
        do { try session.updateApplicationContext(WatchWire.encode(day)); trace("context sent: \(day.blocks.count) blocks, \(day.waiting.count) waiting, \(day.next.count) next") }
        catch { trace("context not sent") }
    }

    // MARK: what the wrist says

    private func hear(_ a: WatchAsk) {
        switch a {
        case .refresh:
            if !Self.fixture { BrainLink.shared.opened(); BrainLink.shared.ensure([Cal.cur.iso(floorDay(NOW1))]) }
            push(force: true)
        case .answer(let id, let action): answer(id, action)
        case .talkStart(let id): startTalk(id)
        case .talkEnd(let id): finishTalk(id)
        case .talkCancel(let id): cancelTalk(id)
        }
    }

    /// A waiting item answered on the wrist, by the paths the timeline and its cards use.
    private func answer(_ id: String, _ action: String) {
        trace("answer: \(action)")
        if let c = Waiting.shared.cards.first(where: { $0.id == id }), let i = WatchActionID.cardIndex(action), c.actions.indices.contains(i) {
            Waiting.shared.answer(c, c.actions[i].answer)
            return
        }
        let until = Date().addingTimeInterval(TimelineState.ackHolds)
        switch action {
        case WatchActionID.ack:
            acked[id] = until
            schedule()
            if Self.fixture { return }
            let card = WaitingCard(id: "notice-" + id, kind: .notice, brainId: id, short: "", title: sent?.waiting.first { $0.id == id }?.title ?? "", meta: "", body: "",
                                   actions: [CardAction(label: "", answer: .acknowledge)])
            Waiting.shared.answer(card, .acknowledge)
            Task { @MainActor in
                while Waiting.shared.sending.contains(card.id) { try? await Task.sleep(nanoseconds: 100_000_000) }
                if Waiting.shared.failures[card.id] != nil { acked[id] = nil; schedule() }
            }
        case WatchActionID.confirm, WatchActionID.deny:
            guard let hex = DayTimelineModel.commitmentHex(id) else { return }
            let say = action == WatchActionID.confirm ? CommitAction.confirm : .cancel
            if say == .confirm { confirmed[id] = until } else { dropped[id] = until }
            schedule()
            if Self.fixture { return }
            Task { @MainActor in
                switch await TLBrain.live.commit(hex, say) {
                case .done, .already: BrainLink.shared.opened()
                case .gone: confirmed[id] = nil; dropped[id] = until; BrainLink.shared.opened()
                case .failed: confirmed[id] = nil; dropped[id] = nil
                }
                schedule()
            }
        default: break
        }
    }

    // MARK: a talk

    private func startTalk(_ id: UUID) {
        cancelTalk(talk?.id)
        trace("talk start")
        guard Self.stub || BrainLink.shared.hasToken else { return fail(id, Words.Relay.notPaired) }
        guard WatchTalk.canListen else { return fail(id, Words.Relay.cannotListen) }
        let t = WatchTalk(id: id, send: { [weak self] in self?.tell($0) }, paused: { [weak self] in Task { @MainActor in self?.finishTalk(id) } })
        talk = t; finishing = false
        feeding.withLock { f in
            f.talk = t
            for (i, d) in f.early where i == id { t.feed(d) }
            f.early = []
        }
        // the phone may be asleep behind the screen: it is kept running for the turn
        backgroundTask = UIApplication.shared.beginBackgroundTask(withName: "watch-talk") { [weak self] in Task { @MainActor in self?.cancelTalk(id) } }
    }

    private func cancelTalk(_ id: UUID?) {
        guard let id, talk?.id == id else { return }
        talk?.cancel()
        endTalk()
    }

    private func endTalk() {
        talk = nil; finishing = false
        feeding.withLock { $0.talk = nil }
        if backgroundTask != .invalid { UIApplication.shared.endBackgroundTask(backgroundTask); backgroundTask = .invalid }
    }

    private func fail(_ id: UUID, _ reason: String) {
        tell(.failed(talk: id, reason: reason))
    }

    /// He stopped, or the phone heard the pause: what was said goes to the brain as a turn, and her answer comes back.
    private func finishTalk(_ id: UUID) {
        guard let t = talk, t.id == id, !finishing else { return }
        finishing = true
        Task { @MainActor in
            var said = await t.finish()
            let counts = t.counts
            trace("talk finished: \(counts.chunks) chunks, \(counts.samples) samples, \(said.count) characters heard")
            guard talk?.id == id else { return }
            #if DEBUG
            if Self.stub, said.isEmpty { said = "test words" }          // the simulator's wrist has no voice: a made-up utterance stands in
            #endif
            guard !said.isEmpty else { fail(id, Words.Relay.heardNothing); endTalk(); return }
            tell(.heard(talk: id, text: said, final: true))
            tell(.thinking(talk: id))
            let tells = await turn(said, id)
            if talk?.id == id { for x in tells { tell(x) }; endTalk() }
        }
    }

    /// Her answer to what he said, as what the wrist is told. The same ask as the Shortcuts action (Voice/AskIntent.swift).
    private func turn(_ said: String, _ id: UUID) async -> [WatchTell] {
        #if DEBUG
        if Self.stub { return [.reply(talk: id, spoken: "Made-up answer to: " + said, written: "", card: nil)] }
        #endif
        guard let body = AskReply.body(said) else { return [.failed(talk: id, reason: Words.Relay.tooLong)] }
        let before = Set(Waiting.shared.cards.map(\.id))
        do {
            switch try await BrainLink.shared.call({ try await BrainClient.ask(body, bases: $0, token: $1) }) {
            case .answered(let spoken, let written):
                let said = AnswerText.spoken(spoken), shown = AnswerText.visible(written)
                await Waiting.shared.poll()                     // a card she put up in the turn is looked for now, not in four seconds
                let card = Waiting.shared.cards.first { !before.contains($0.id) }.flatMap(WatchDayMaker.card)
                return [.reply(talk: id, spoken: said.isEmpty ? AnswerText.visible(spoken) : said, written: shown, card: card)]
            case .working: return [.reply(talk: id, spoken: Words.Relay.working, written: "", card: nil)]
            case .failed: return [.failed(talk: id, reason: Words.Relay.turnFailed)]
            }
        } catch let e as BrainError {
            return [.failed(talk: id, reason: e.message)]
        } catch {
            return [.failed(talk: id, reason: BrainError.refused.message)]
        }
    }

    /// Tell the wrist something about the talk. Its end (an answer, a failure) is kept for the watch if it cannot be reached just now.
    private nonisolated func tell(_ t: WatchTell) {
        let s = WCSession.default
        guard s.activationState == .activated else { return }
        let message = WatchWire.encode(t)
        if s.isReachable { s.sendMessage(message, replyHandler: nil, errorHandler: nil); return }
        switch t {
        case .reply, .failed: s.transferUserInfo(message)
        default: break
        }
    }

    // MARK: WCSessionDelegate

    nonisolated func session(_ session: WCSession, activationDidCompleteWith state: WCSessionActivationState, error: Error?) {
        Task { @MainActor in push(force: true) }
    }
    nonisolated func sessionDidBecomeInactive(_ session: WCSession) {}
    nonisolated func sessionDidDeactivate(_ session: WCSession) { session.activate() }
    nonisolated func sessionWatchStateDidChange(_ session: WCSession) { Task { @MainActor in push(force: true) } }

    nonisolated func session(_ session: WCSession, didReceiveMessage message: [String: Any]) {
        if let a = WatchWire.decode(WatchAsk.self, message) { Task { @MainActor in hear(a) } }
    }
    nonisolated func session(_ session: WCSession, didReceiveUserInfo userInfo: [String: Any]) {
        if let a = WatchWire.decode(WatchAsk.self, userInfo) { Task { @MainActor in hear(a) } }
    }
    /// the wrist's microphone: fed to the talk at once, on this thread, so the chunks keep their order
    nonisolated func session(_ session: WCSession, didReceiveMessageData data: Data) {
        guard let (id, samples) = WatchWire.audio(data) else { return }
        feeding.withLock { f in
            if let t = f.talk, t.id == id { t.feed(samples) } else if f.early.count < 50 { f.early.append((id, samples)) }
        }
    }
}
