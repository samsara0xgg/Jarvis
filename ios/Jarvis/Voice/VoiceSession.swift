import AVFoundation
import Observation
import os
import Speech
import UIKit

/// Talking to Jarvis: listen, pick the language, say it to the host, play her answer, listen again for a moment. The UI binds to this.
///
///     VoiceSession.shared.start()   // tap to talk (while she talks: interrupt her and listen)
///     VoiceSession.shared.stop()    // tap again: the utterance ends now (while she talks: cancel)
///     VoiceSession.shared.cancel()  // drop everything
///     VoiceSession.shared.sendTyped("...")  // typed words: her answer comes back in `reply` as text only, no audio
///
/// Read `state`, `partial` (live words), `transcript` (what was said), `reply` (her words so far), `inputLevel` / `outputLevel` (0...1),
/// `language`, `errorText`, `modelProgress` (0...1 while the language models download on first use). Set `endpoint` once the pairing exists;
/// until then a local stub answers.
@MainActor @Observable
final class VoiceSession {
    enum State: Equatable { case idle, listening, thinking, speaking }

    static let shared = VoiceSession()

    private(set) var state = State.idle
    private(set) var errorText: String?
    private(set) var partial = ""
    private(set) var transcript = ""
    private(set) var reply = ""
    private(set) var inputLevel = 0.0
    private(set) var outputLevel = 0.0
    private(set) var language = VoiceLang.zh
    private(set) var modelProgress: Double?

    /// Listen for "Jarvis" / "贾维斯" while the app is open and nothing else is going on.
    var wakeWordInApp: Bool {
        get { wake }
        set { wake = newValue; refreshArming() }
    }
    private var wake = false

    /// Where the host is and the token that proves this phone; the pairing owns both.
    @ObservationIgnored var endpoint: () -> (base: URL, token: String)? = { nil }

    // MARK: timings

    private let silenceEnd = 0.8       // no new words and a quiet mic after speech began: the utterance is over
    private let noSpeech = 10.0        // nothing said this long after listening began: back to idle (also the follow-up window; the Mac's conversation_idle_exit_s)
    private let waitHold = 60.0        // after 「等一下」 the microphone stays open this long (the Mac's conversation_wait_s)
    private let utteranceCap = 30.0
    private let quiet = 0.3            // input level below this is quiet
    private let bargeLevel = 0.12      // words heard while she talks only count as him when the mic is at least this loud
    private let audioFallback = 3.0    // her text is done and her ring has been empty this long but ACTIVE(-1) never came: treat her audio as done (logged)
    private let noAudioAfterText = 6.0 // her text is done and no audio ever came this long after: the host did not speak it
    private let noReply = 30.0         // nothing from the host (no row, no audio) this long: she did not answer
    private let wakeReset = 120.0      // an armed recognizer with no words is restarted this often
    private let judgeTimeout = 2.5     // a judged say (over her, or a conversation follow-up) unanswered this long counts as a turn
    private let revealRate = 6.0       // her words appear at about the pace she speaks them (a CJK character is 1, see `speechUnits`)
    private let holdCap = 8.0          // her voice is held at most this long for words over her

    // MARK: internals

    @ObservationIgnored private let audio = VoiceAudio()
    @ObservationIgnored private let feed = Feed()
    @ObservationIgnored private var rec: AnyObject?                 // a DualRecognizer (iOS 26)
    @ObservationIgnored private var segment = 0
    @ObservationIgnored private var herWords = ""          // what she said lately (rows' text), for telling her echo from him
    @ObservationIgnored private var judgedOverHer = ""     // the start of this segment's text already judged echo or a nod
    @ObservationIgnored private var overHer: OverHer?
    @ObservationIgnored private var ducked = false
    @ObservationIgnored private var waitUntil = 0.0       // listening is held open until then after 「等一下」
    @ObservationIgnored private var lastTrouble: (overloads: UInt64, gaps: UInt64) = (0, 0)
    @ObservationIgnored private var segmentText = ""
    @ObservationIgnored private var segmentAt = 0.0
    @ObservationIgnored private var link: VoiceLink?
    @ObservationIgnored private var linkIsReal = false
    @ObservationIgnored private var linkTask: Task<Void, Never>?
    @ObservationIgnored private var tickTask: Task<Void, Never>?
    @ObservationIgnored private var assetsReady = false
    @ObservationIgnored private var assetsTriedAt = -60.0      // `now` of the last model install try, so a missing model is retried at most once a minute
    @ObservationIgnored private var missingNote: String?       // what listening keeps saying while one language's model is not installed
    @ObservationIgnored private var preparing = false
    @ObservationIgnored private var foreground = true
    @ObservationIgnored private var rawLevel = 0.0
    @ObservationIgnored private var startedAt = 0.0
    @ObservationIgnored private var lastWordAt = 0.0
    @ObservationIgnored private var wakeTurn = false
    @ObservationIgnored private var sayAt = 0.0
    @ObservationIgnored private var lastActivityAt = 0.0
    @ObservationIgnored private var playedAtSay: UInt64 = 0
    // the turn being answered
    @ObservationIgnored private var turnSeq = 0                     // bumped whenever a turn starts or ends; late results of an older one are dropped
    @ObservationIgnored private var currentTurn: String?            // the host's turn_id, from `said`
    @ObservationIgnored private var awaitingSaid = false
    @ObservationIgnored private var heldRows: [RowFrame] = []       // rows that beat `said`
    @ObservationIgnored private var typedTurn = false
    @ObservationIgnored private var finalText = ""
    @ObservationIgnored private var commentary = ""
    @ObservationIgnored private var waitLines = Set<String>()       // response ids of commentary runs: their cancel is not the turn's
    @ObservationIgnored private var rowsDone = false                // surface.response_emitted of the answer arrived
    @ObservationIgnored private var rowsDoneAt = 0.0
    @ObservationIgnored private var audioStarted = false
    @ObservationIgnored private var audioClosed = false             // ACTIVE(-1) after her last PCM16: the host saw it all presented
    // the host's side of this connection
    @ObservationIgnored private var hostVoice = true
    @ObservationIgnored private var readySent = false
    @ObservationIgnored private var commandSaid: VoiceCommand?
    @ObservationIgnored private var hostJudges = false             // the host judges words over her and follow-ups (`ready.judges`)
    @ObservationIgnored private var held = false                   // his words over her are with the host; her output is held here meanwhile
    @ObservationIgnored private var heldAt = 0.0
    @ObservationIgnored private var answer = ""                    // her words so far; `reply` shows them as she speaks
    @ObservationIgnored private var shown = 0.0                    // how much of `answer` is in `reply`, in speech units
    @ObservationIgnored private var lastTickAt = 0.0
    @ObservationIgnored private var inConversation = false         // listening was entered from a follow-up window or 「等一下」, not a tap or the wake word
    @ObservationIgnored private let log = Logger(subsystem: "com.allen.jarvis", category: "voice")

    private var now: Double { Double(uptimeNs()) / 1e9 }
    private var armed: Bool { wake && foreground && state == .idle }

    private init() {
        foreground = UIApplication.shared.applicationState != .background
        hop(UIApplication.didEnterBackgroundNotification) { $0.foreground = false; $0.refreshArming() }
        hop(UIApplication.willEnterForegroundNotification) { $0.foreground = true; $0.refreshArming() }
        hop(UIApplication.didBecomeActiveNotification) { if TalkRequest.pending { $0.requestTalk() } }
        hop(TalkRequest.notification) { $0.requestTalk() }
        audio.onMic = { [weak self, feed] buf, level in
            feed.feed(buf)
            Task { @MainActor in self?.rawLevel = level }
        }
        audio.onOut = { [weak self] frames in self?.link?.send(frames) }
        audio.onReset = { [weak self] interrupted in
            guard let self else { return }
            self.goIdle(releasing: true)
            if !interrupted { self.refreshArming() }
        }
        if TalkRequest.pending { requestTalk() }
    }

    private func hop(_ name: Notification.Name, _ body: @escaping @MainActor (VoiceSession) -> Void) {
        _ = NotificationCenter.default.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { if let self { body(self) } }
        }
    }

    // MARK: the calls

    /// Start listening. Only once the app is on screen, so an intent that fires during launch waits for it.
    func requestTalk() {
        if UIApplication.shared.applicationState == .active { TalkRequest.pending = false; start() } else { TalkRequest.pending = true }
    }

    func start() {
        VoiceTrace.note("mic tapped, state \(state)")
        guard #available(iOS 26, *) else { errorText = "语音需要 iOS 26 或更新的系统"; return }
        errorText = nil
        switch state {
        case .listening: break
        case .thinking, .speaking: bargeIn()
        case .idle: Task { if await prepare(), state == .idle { enterListening(); startSegment() } }
        }
    }

    /// While he talks: the utterance ends now. While she talks or thinks: cancel.
    func stop() {
        switch state {
        case .listening: endUtterance()
        case .thinking, .speaking: cancel()
        case .idle: break
        }
    }

    func cancel() {
        if state == .thinking || state == .speaking { audio.core?.stopNow(); link?.cancel() }
        goIdle(releasing: false)
    }

    /// Typed words from the composer: sent the way spoken ones are, answered in `reply` as text only. Whatever she was doing is dropped first.
    func sendTyped(_ text: String) {
        let text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        cancel()
        errorText = nil
        transcript = text
        partial = ""
        commandSaid = nil
        set(.thinking)
        say(text, lang: nil, confidence: nil, spoken: false)
        let seq = turnSeq
        Task {                                                      // no tick runs without the microphone, so this turn times itself
            while seq == turnSeq, state == .thinking, !rowsDone {
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                if now - lastActivityAt >= noReply { fail("Jarvis 没有回应"); return }
            }
        }
    }

    // MARK: getting ready

    /// Microphone, models, audio engine. False (with `errorText`) if any of them is not there.
    private func prepare() async -> Bool {
        guard #available(iOS 26, *) else { return false }
        if preparing { return false }
        preparing = true
        defer { preparing = false }
        let heard = await AVAudioApplication.requestRecordPermission()
        VoiceTrace.note("microphone permission \(heard ? "granted" : "refused")")
        if !heard { errorText = "没有麦克风权限"; return false }
        if !assetsReady && now - assetsTriedAt >= 60 {
            assetsTriedAt = now
            guard SpeechTranscriber.isAvailable else { errorText = "这台设备不能在本地识别语音"; return false }
            modelProgress = 0
            defer { modelProgress = nil }
            do {
                let missing = try await DualRecognizer.ensureAssets { p in Task { @MainActor in VoiceSession.shared.modelProgress = p } }
                assetsReady = missing.isEmpty
                missingNote = missing.first?.modelMissingText
                if let note = missingNote { errorText = note }
            } catch { errorText = "语言模型没装好：\(error.localizedDescription)"; return false }
        }
        do {
            try await audio.start()
            VoiceTrace.note("audio engine started")
            sendReady()
            if tickTask == nil { tickTask = Task { while !Task.isCancelled { tick(); try? await Task.sleep(nanoseconds: 33_000_000) } } }
        } catch is VoiceAudio.NotReady {
            errorText = "麦克风还没准备好，再点一次"; return false
        } catch { errorText = "打不开麦克风：\(error.localizedDescription)"; return false }
        return true
    }

    private func ensureLink() {
        let real = endpoint() != nil
        if let link, real == linkIsReal { link.connect(); return }
        linkTask?.cancel()
        link?.disconnect()
        VoiceTrace.note(real ? "link: websocket to \(endpoint().map { ($0.base.scheme ?? "") + "://" + (BrainLink.host($0.base)) } ?? "?")" : "link: local stub (not paired)")
        let l: VoiceLink = real ? WebSocketVoiceLink(endpoint: endpoint) : LocalStubLink()
        link = l
        linkIsReal = real
        l.connect()
        linkTask = Task { for await e in l.events { handle(e) } }
    }

    /// Wake-word listening runs while the app is open and idle; otherwise an idle session lets go of the microphone.
    private func refreshArming() {
        guard #available(iOS 26, *) else { return }
        if armed {
            Task { if await prepare(), armed, rec == nil { startSegment() } }
        } else if state == .idle {
            release()
        }
    }

    private func release() {
        dropSegment()
        audio.stop()
        tickTask?.cancel(); tickTask = nil
        linkTask?.cancel(); linkTask = nil
        link?.disconnect(); link = nil
        inputLevel = 0; outputLevel = 0
    }

    // MARK: segments: one recognizer per stretch of audio

    @available(iOS 26, *)
    private func startSegment() {
        guard audio.running else { return }
        segment += 1
        let id = segment
        let old = rec as? DualRecognizer
        let r = DualRecognizer(onDead: { [weak self] lang, both in Task { @MainActor in self?.laneDead(lang, both: both, id) } },
                               onPartial: { [weak self] p in Task { @MainActor in self?.heard(p, id) } })
        rec = r
        feed.set(r)
        if let old { Task { await old.cancel() } }
        segmentText = ""
        judgedOverHer = ""; overHer = nil
        segmentAt = now
        lastWordAt = now
        partial = ""
    }

    private func dropSegment() {
        segment += 1
        feed.set(nil)
        if #available(iOS 26, *), let old = rec as? DualRecognizer { Task { await old.cancel() } }
        rec = nil
        segmentText = ""
        partial = ""
    }

    /// One language's recognizer could not start: say so; with none left, the listen fails.
    private func laneDead(_ lang: VoiceLang, both: Bool, _ id: Int) {
        guard id == segment else { return }
        if both { if state == .listening { fail("中文和英文语音模型都没装好") } else { errorText = "中文和英文语音模型都没装好" } }
        else { errorText = lang.modelMissingText }
    }

    /// Words from the recognizer, live.
    private func heard(_ p: Partial, _ id: Int) {
        guard id == segment, !p.text.isEmpty else { return }
        if p.text != segmentText { lastWordAt = now }
        segmentText = p.text
        switch state {
        case .listening: partial = p.text; language = p.lang; reply = ""   // he has started again: her last answer must not come back when he pauses
        case .idle: if armed, heardWake(p.heard) { wakeTurn = true; language = p.lang; enterListening(); partial = p.text }
        case .thinking, .speaking: if held { partial = p.text } else { overHerWords(p.text) }
        }
    }

    /// Words while she talks: her own echo and nods are let through, a few words lower her voice, an interruption stops her.
    private func overHerWords(_ text: String) {
        let v = judgeOverHer(text, hers: herRecent(since: now), judged: judgedOverHer)
        if v != overHer { overHer = v; VoiceTrace.note("over her: \(v) (\(afterJudged(text, judgedOverHer).count) chars)") }
        switch v {
        case .echo, .backchannel: judgedOverHer = text; duck(false)
        case .tooShort: break                                // her voice stays as it is (yilun 2026-10-10: no ducking)
        case .barge: if inputLevel < bargeLevel { break } else if hostJudges, !awaitingSaid { holdHer() } else { bargeIn() }   // a follow-up still with the host: a plain barge-in
        }
    }

    /// An interruption the host judges: her voice is held here (not stopped), his words keep being recognized, the turn stays as it is.
    private func holdHer() {
        VoiceTrace.note("soft barge: holding her")
        // ponytail: with no audio yet (thinking, generation -1) there is nothing to pin, so her first audio may start during the judgment; duck covers it
        if let core = audio.core, core.activeGeneration >= 0 { core.ingest(.hold(core.activeGeneration)) }
        held = true
        heldAt = now
        partial = segmentText
        if let c = shortCommand(afterJudged(segmentText, judgedOverHer)), c == .stop || c == .goodnight { audio.core?.stopNow() }
    }

    /// His words over her ended: finish the segment and let the host judge them (state and turn stay until it answers).
    @available(iOS 26, *)
    private func finishHeld() {
        guard held, let r = rec as? DualRecognizer else { return }
        rec = nil
        feed.set(nil)
        let seq = turnSeq
        Task {
            let (picked, _) = await r.finish()
            guard seq == turnSeq, held else { return }
            var text = afterJudged(picked.text, judgedOverHer).trimmingCharacters(in: .whitespacesAndNewlines)
            if !text.isEmpty, isOwnEcho(text, herRecent(since: heldAt)) { VoiceTrace.note("held words were her own echo: dropped"); text = "" }
            if text.isEmpty { VoiceTrace.note("held: nothing recognized"); resumeHer(); return }
            let id = UUID().uuidString, lang = picked.lang
            guard let link else { resumeHer(); return }
            if let c = shortCommand(text), c == .stop || c == .goodnight {
                VoiceTrace.note("judged over her: local \(c.rawValue)")
                audio.core?.stopNow()
                Task { _ = try? await link.say(utteranceId: id, text: text, language: lang, confidence: picked.confidence, spoken: true, overHer: true, conversation: false, about: AboutNow.current?.json, timeout: 10) }
                goIdle(releasing: false)
                return
            }
            VoiceTrace.note("judging over her: \(text.count) chars")
            let r: (turnId: String?, verdict: String)
            do { r = try await link.say(utteranceId: id, text: text, language: lang, confidence: picked.confidence, spoken: true, overHer: true, conversation: false, about: AboutNow.current?.json, timeout: judgeTimeout) }
            catch {
                guard seq == turnSeq, held else { return }
                VoiceTrace.note("judge timed out: turn (\(error.localizedDescription))")
                link.barge()
                takeOver(text, lang)
                say(text, lang: lang, confidence: picked.confidence, spoken: true, utteranceId: id)
                startSegment()
                return
            }
            guard seq == turnSeq, held else { return }
            VoiceTrace.note("judged over her: \(r.verdict)")
            switch r.verdict {
            case "backchannel", "unclear", "echo", "elsewhere": resumeHer()    // elsewhere: the Mac already took these words
            case "wait": audio.core?.stopNow(); holdForWait()
            case _ where r.verdict == "stop" || r.verdict == "dismissed" || r.verdict.hasPrefix("quiet:"): audio.core?.stopNow(); goIdle(releasing: false)
            default:
                if let turn = r.turnId {
                    takeOver(text, lang)
                    sayAt = now; lastActivityAt = now
                    currentTurn = turn
                    startSegment()
                } else {                                    // a turn with no id: the host did not write it down, so say it again
                    link.barge()
                    takeOver(text, lang)
                    say(text, lang: lang, confidence: picked.confidence, spoken: true, utteranceId: id)
                    startSegment()
                }
            }
        }
    }

    /// A nod, an echo or noise over her: let go of her voice and listen on with a fresh segment.
    @available(iOS 26, *)
    private func resumeHer() {
        audio.core?.ingest(.hold(-1))
        held = false
        duck(false)
        startSegment()
    }

    /// His words over her became the turn: her audio is gone here and the screen follows his words (the host has stopped her, or is about to).
    private func takeOver(_ text: String, _ lang: VoiceLang) {
        audio.core?.stopNow()
        duck(false)
        endTurn()
        language = lang
        transcript = text
        partial = ""
        reply = ""
        commandSaid = nil
        set(.thinking)
        playedAtSay = audio.core?.playedSamples ?? 0
    }

    /// Her words as they come; typed turns and a host without voice show them whole, spoken ones follow her voice (`revealAnswer`).
    private func setAnswer(_ text: String) {
        if !text.hasPrefix(answer) { shown = 0 }            // the answer replaced her wait line
        answer = text
        if typedTurn || !hostVoice { reply = text }
    }

    /// While her voice plays, `reply` grows at her speaking pace; once the host has sent all her audio, it paces itself to end with it.
    private func revealAnswer(_ core: PlayerCore, _ dt: Double) {
        guard !answer.isEmpty, reply != answer, audioStarted, !held, !core.ringEmpty else { return }
        let left = speechUnits(answer) - shown
        let rate = audioClosed ? max(revealRate, left / max(core.bufferedSeconds, 0.3)) : revealRate
        shown += rate * dt
        reply = speechPrefix(answer, shown)
    }

    /// Her words, if she was audible within 1.5 s of `t`: an echo needs her voice; words she said a while ago he may repeat himself.
    private func herRecent(since t: Double) -> String {
        guard let ns = audio.core?.lastAudioNs, ns > 0, Double(ns) / 1e9 >= t - 1.5 else { return "" }
        return herWords
    }

    /// Her voice lowered (or back) while words over her are judged; the player ramps it in 50 ms.
    private func duck(_ on: Bool) {
        guard on != ducked else { return }
        ducked = on
        audio.core.map { $0.ingest(.gain(target: on ? 0.3 : 1, ramp: UInt32($0.sampleRate / 20))) }
    }

    // MARK: turns

    private func enterListening() {
        set(.listening)
        inConversation = false
        startedAt = now
        lastWordAt = now
        errorText = missingNote
        transcript = ""
    }

    /// He spoke over her (or tapped): stop her audio here at once, tell the host, listen.
    private func bargeIn() {
        VoiceTrace.note("barge-in from \(state)")
        audio.core?.stopNow()
        duck(false)                                         // the next answer plays at full volume
        link?.barge()
        endTurn()
        wakeTurn = false
        enterListening()
        if #available(iOS 26, *), rec == nil { startSegment() }
        partial = segmentText
    }

    private func endUtterance() {
        guard #available(iOS 26, *), state == .listening, let r = rec as? DualRecognizer else { return }
        let wakeTurn = wakeTurn
        self.wakeTurn = false
        set(.thinking)
        sayAt = now
        waitUntil = 0                                       // he spoke: the wait is over
        lastActivityAt = now                                // the no-reply clock starts here, not at the last turn (the finals take a moment)
        playedAtSay = audio.core?.playedSamples ?? 0        // her earlier replies are not this turn's audio: thinking stays thinking until new audio plays
        rec = nil
        feed.set(nil)
        segment += 1                                        // the finishing recognizer's last partials are his own words, not words over her
        Task {
            let (picked, _) = await r.finish()
            gotFinal(picked, wakeTurn: wakeTurn)
        }
    }

    @available(iOS 26, *)
    private func gotFinal(_ picked: LaneResult, wakeTurn: Bool) {
        guard state == .thinking else { VoiceTrace.note("final dropped: state \(state)"); return }   // cancelled meanwhile
        var text = (wakeTurn ? stripWake(picked.text) : picked.text).trimmingCharacters(in: .whitespacesAndNewlines)
        text = afterJudged(text, judgedOverHer).trimmingCharacters(in: .whitespacesAndNewlines)   // her echo or a nod before his words
        if !text.isEmpty, isOwnEcho(text, herRecent(since: startedAt)) { VoiceTrace.note("final was her own echo: dropped"); text = "" }
        if text.isEmpty {
            VoiceTrace.note("nothing recognized (picked lane \(picked.lang.rawValue))")
            if wakeTurn { enterListening(); startSegment() }  // only the wake word: now the request
            else { errorText = "没听清，再说一次"; goIdle(releasing: false) }
            return
        }
        language = picked.lang
        transcript = text
        partial = ""
        commandSaid = shortCommand(text)
        let conversation = hostJudges && inConversation
        if commandSaid == .stop || commandSaid == .wait { audio.core?.stopNow(); if !conversation { link?.cancel() } }   // a judged say lets the host stop her
        playedAtSay = audio.core?.playedSamples ?? 0
        say(text, lang: picked.lang, confidence: picked.confidence, spoken: true, conversation: conversation)
        startSegment()                                      // listening for him to interrupt
    }

    /// Send the words and start following the turn the host opens for them.
    private func say(_ text: String, lang: VoiceLang?, confidence: Double?, spoken: Bool, utteranceId: String = UUID().uuidString, conversation: Bool = false) {
        endTurn()
        awaitingSaid = true
        typedTurn = !spoken
        reply = ""
        sayAt = now
        lastActivityAt = now
        ensureLink()
        let seq = turnSeq
        VoiceTrace.note("say sent: \(text.count) chars, spoken \(spoken), lang \(lang?.rawValue ?? "none")\(conversation ? ", follow-up" : "")")
        Task {
            do {
                guard let link else { return }
                var r: (turnId: String?, verdict: String)
                do { r = try await link.say(utteranceId: utteranceId, text: text, language: lang, confidence: confidence, spoken: spoken, overHer: false, conversation: conversation, about: AboutNow.current?.json, timeout: conversation ? judgeTimeout : 10) }
                catch LinkError.timeout where conversation {
                    VoiceTrace.note("judge timed out: turn")
                    guard seq == turnSeq else { return }
                    r = try await link.say(utteranceId: utteranceId, text: text, language: lang, confidence: confidence, spoken: spoken, overHer: false, conversation: false, about: AboutNow.current?.json, timeout: 10)
                }
                guard seq == turnSeq else { return }
                if conversation {
                    VoiceTrace.note("judged follow-up: \(r.verdict)")
                    switch r.verdict {
                    case "backchannel", "unclear", "echo", "elsewhere":     // not for her (or the Mac took them): stay in the conversation window
                        endTurn()
                        enterListening()
                        inConversation = true
                        if #available(iOS 26, *) { startSegment() }
                        return
                    case "wait": holdForWait(); return
                    case _ where r.verdict == "stop" || r.verdict == "dismissed" || r.verdict.hasPrefix("quiet:"): goIdle(releasing: false); return
                    default: break
                    }
                }
                if r.verdict == "elsewhere" { VoiceTrace.note("said: the Mac took these words"); goIdle(releasing: false); return }
                VoiceTrace.note("said acknowledged by the host")
                awaitingSaid = false
                currentTurn = r.turnId
                let held = heldRows
                heldRows = []
                for r in held { handleRow(r) }
            } catch {
                VoiceTrace.note("say failed: \(error.localizedDescription)")
                if seq == turnSeq { fail(error.localizedDescription) }
            }
        }
    }

    /// Nothing of the turn in flight is kept; a late `said` or row of it is dropped.
    private func endTurn() {
        turnSeq += 1
        currentTurn = nil
        awaitingSaid = false
        heldRows = []
        typedTurn = false
        held = false
        answer = ""; shown = 0
        finalText = ""; commentary = ""
        waitLines = []
        rowsDone = false
        audioStarted = false
        audioClosed = false
    }

    private func replyFinished() {
        if held { return }                                  // his words over her are being judged; the verdict decides what happens next
        if !answer.isEmpty { reply = answer }
        if commandSaid == .wait { holdForWait(); return }
        if typedTurn || commandSaid == .stop || commandSaid == .goodnight { goIdle(releasing: false); return }
        // a short window to answer without tapping; its own silence timer ends it
        endTurn()
        enterListening()
        inConversation = true
        if #available(iOS 26, *) { startSegment() }
    }

    /// 「等一下」: she is quiet and the microphone stays open for a minute; anything he says then is a turn.
    private func holdForWait() {
        VoiceTrace.note("waiting for him: listening \(Int(waitHold)) s")
        endTurn()
        enterListening()
        inConversation = true
        waitUntil = now + waitHold
        if #available(iOS 26, *) { startSegment() }
    }

    private func fail(_ message: String) {
        errorText = message
        goIdle(releasing: false)
    }

    private func goIdle(releasing: Bool) {
        VoiceTrace.note("idle from \(state)\(releasing ? ", releasing the mic" : "")")
        audio.core?.stopNow()
        duck(false)
        herWords = ""
        waitUntil = 0
        set(.idle)
        wakeTurn = false
        inConversation = false
        endTurn()
        dropSegment()
        if releasing { release() } else { refreshArming() }
    }

    private func set(_ s: State) {
        guard s != state else { return }
        log.debug("voice \(String(describing: self.state)) -> \(String(describing: s))")
        state = s
    }

    // MARK: the host

    private func handle(_ e: LinkEvent) {
        switch e {
        case .ready(_, let voice, let rate, let judges):
            VoiceTrace.note("host ready: voice \(voice), rate \(rate), judges \(judges)")
            hostVoice = voice
            hostJudges = judges
            readySent = false
            sendReady()
        case .row(let r): handleRow(r)
        case .audio(let f):
            audio.core?.ingest(f)
            switch f {
            case .pcm where currentTurn != nil || awaitingSaid: audioStarted = true; audioClosed = false; lastActivityAt = now
            case .active(-1) where audioStarted: audioClosed = true
            default: break
            }
        case .error(_, let m): errorText = m
        case .unpaired: fail("还没有和 Jarvis 配对")
        case .replaced: fail("Jarvis 在这台手机的另一条连接上被接管了")
        case .closed:
            VoiceTrace.note("link closed")
            readySent = false
            if state == .thinking || state == .speaking { fail("和 Jarvis 的连接断了") }
        }
    }

    /// The player's READY, once per connection and only for a host that speaks: our audio session must be up. Its rate is the wire's 32 kHz.
    private func sendReady() {
        guard hostVoice, !readySent, let link, let core = audio.core else { return }
        link.send([core.readyFrame(bufferFrames: audio.bufferFrames)])
        readySent = true
    }

    /// One row of this device's turns. `reply` is her words so far; the answer's `response_emitted` ends the text, the audio is judged in `tick`.
    private func handleRow(_ r: RowFrame) {
        PhoneRows.offer(r)
        guard state == .thinking || state == .speaking else { return }
        guard let current = currentTurn else {
            if awaitingSaid { heldRows.append(r) }
            return
        }
        guard r.turnId == current else { return }
        lastActivityAt = now
        let isLine = r.phase == "commentary"
        switch r.eventType {
        case "surface.response_open":
            if isLine, let id = r.payload["response_id"] as? String { waitLines.insert(id) }
        case "surface.response_chunk":
            guard let t = r.text else { return }
            if isLine { commentary += t } else { finalText += t }
            herWords = String((herWords + t).suffix(300))
            setAnswer(finalText.isEmpty ? commentary : finalText)
        case "surface.response_emitted":
            guard !isLine else { return }
            if finalText.isEmpty, let t = r.text { finalText = t; setAnswer(t) }     // a turn that was not streamed has only this row
            rowsDone = true
            rowsDoneAt = now
            if typedTurn || !hostVoice { replyFinished() }
        case "response.cancelled":
            if let id = r.payload["response_id"] as? String, waitLines.contains(id) { return }
            if held { return }                              // his words over her are being judged: a turn verdict cancels this answer, the verdict decides
            goIdle(releasing: false)
        case "response.failed":
            if let id = r.payload["response_id"] as? String, waitLines.contains(id) { return }
            fail("Jarvis 没答出来" + (r.reason.map { "：\($0)" } ?? ""))
        default: break
        }
    }

    // MARK: the clock

    private func tick() {
        let t = now
        let dt = min(0.1, t - lastTickAt)
        lastTickAt = t
        inputLevel = max(rawLevel, inputLevel * 0.85)
        outputLevel = Double(min(1, (audio.core?.level ?? 0) * 4))
        switch state {
        case .listening:
            let age = t - startedAt
            if !segmentText.isEmpty, t - lastWordAt > silenceEnd, inputLevel < quiet { endUtterance() }
            else if segmentText.isEmpty, age > noSpeech, t > waitUntil { VoiceTrace.note("no words for \(Int(noSpeech)) s: idle"); goIdle(releasing: false) }
            else if age > utteranceCap + (waitUntil > 0 ? waitHold : 0) { endUtterance() }
        case .thinking, .speaking:
            guard let core = audio.core, !typedTurn else { return }
            revealAnswer(core, dt)
            if core.trouble != lastTrouble { lastTrouble = core.trouble; VoiceTrace.note("player: \(lastTrouble.overloads) overloads, \(lastTrouble.gaps) gaps") }
            if state == .thinking, core.playedSamples > playedAtSay + 480 { set(.speaking) }
            if held, #available(iOS 26, *), (t - lastWordAt > silenceEnd && inputLevel < quiet) || t - heldAt > holdCap { finishHeld() }
            if ducked, !held, t - lastWordAt > 1.5 { duck(false) }      // the words over her stopped short of an interruption
            if !core.ringEmpty { lastActivityAt = t }
            let quietAfter = commandSaid == .stop || commandSaid == .wait || commandSaid == .goodnight
            if rowsDone {
                if !audioStarted {
                    if t - rowsDoneAt > noAudioAfterText { replyFinished() }
                } else if core.ringEmpty, audioClosed {
                    replyFinished()
                } else if core.ringEmpty, core.lastAudioNs > 0, t - Double(core.lastAudioNs) / 1e9 > audioFallback {
                    log.notice("voice: reply ended by the \(self.audioFallback) s fallback, no ACTIVE(-1) came")
                    replyFinished()
                }
            } else if t - lastActivityAt > (quietAfter ? 3 : noReply) {
                if commandSaid == .wait { holdForWait() } else if quietAfter { goIdle(releasing: false) } else { fail("Jarvis 没有回应") }
            }
        case .idle:
            // armed: a phrase that is not the wake word is forgotten, and a long silence restarts the recognizer
            if #available(iOS 26, *), armed, rec != nil {
                if !segmentText.isEmpty, t - lastWordAt > silenceEnd, inputLevel < quiet { startSegment() }
                else if segmentText.isEmpty, t - segmentAt > wakeReset { startSegment() }
            }
        }
    }
}

/// Hands the microphone's buffers to the current recognizer from the audio thread.
private final class Feed: @unchecked Sendable {
    private let lock = OSAllocatedUnfairLock<AnyObject?>(initialState: nil)
    func set(_ r: AnyObject?) { lock.withLock { $0 = r } }
    func feed(_ b: AVAudioPCMBuffer) {
        if #available(iOS 26, *) { (lock.withLock { $0 } as? DualRecognizer)?.feed(b) }
    }
}
