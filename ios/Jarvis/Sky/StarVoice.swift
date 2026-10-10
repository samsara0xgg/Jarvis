import SwiftUI
import Observation

// What the star chart shows of the phone voice (Voice/VoiceSession): her three looks, the words, the one status line.
// The session is only read here, and started or stopped by the mic button.

/// the session's values the sky needs, read once a frame (DEBUG `-voiceScene` hands in made-up ones)
struct VoiceShot {
    var state = VoiceSession.State.idle
    var partial = "", transcript = "", reply = ""
    var input = 0.0, output = 0.0
    var error: String?
    var progress: Double?
}

extension StarEngine {
    func voiceShot(_ now: Double) -> VoiceShot {
        #if DEBUG
        if let fake = voiceFake { return fake(now) }
        #endif
        return MainActor.assumeIsolated {
            let v = VoiceSession.shared
            return VoiceShot(state: v.state, partial: v.partial, transcript: v.transcript, reply: AnswerText.visible(v.reply),
                             input: v.inputLevel, output: v.outputLevel, error: v.errorText, progress: v.modelProgress)
        }
    }

    /// the mic button: start listening, or (listening, thinking, speaking) end the utterance or cancel her
    func micVoice() {
        voiceErrAt = nowMs()        // a trouble this tap meets is shown again
        MainActor.assumeIsolated {
            let v = VoiceSession.shared
            if v.state == .idle { v.start() } else { v.stop() }
        }
    }

    /// The idle loop (20 fps) would notice a change of the session's state up to 50 ms late: wake the stage as it happens.
    func watchVoice() {
        MainActor.assumeIsolated {
            withObservationTracking { _ = VoiceSession.shared.state } onChange: { [weak self] in
                DispatchQueue.main.async { self?.wake(); self?.watchVoice() }
            }
        }
    }

    /// Every frame. While the session is idle this is a few compares. Returns the level of her voice while she speaks (the orb's `say`).
    func syncVoice(_ now: Double) -> Double? {
        let v = voiceShot(now)
        // the one status line: the model download, the last trouble for eight seconds, or what the brain said about what he sent (Sky/StarBrain)
        if v.error != voiceErr { voiceErr = v.error; voiceErrAt = now; if let e = v.error { VoiceTrace.note("error shown: \(e)") } }
        put(\.voiceNote, v.progress.map { "正在下载中英文识别模型 \(Int(($0 * 100).rounded()))%" } ?? (now - voiceErrAt < 8000 ? v.error : nil) ?? (now < brainNoteUntil ? brainNote : nil))
        if v.state != voiceState {
            let was = voiceState
            wake()
            if was == .idle && talk != nil { interrupt() }      // a scripted talk in progress is over (voiceState is still idle: the session is left alone)
            voiceState = v.state; voiceAt = now
            if was == .idle { beginTalk() }
            if v.state == .listening { vHis = nil; vHer = nil }     // a new utterance of the session: its words are new rows
            else if v.state == .idle {
                voiceRows(v)
                talk = nil; endTalk()                           // no `end` cue: the microphone may still be open
                adoptWritten()
                return nil
            }
        }
        guard v.state != .idle else { return nil }
        // the looks of the typed flow: listening, a nod when she has heard, thinking, speaking
        let listening = v.state == .listening, says = v.state == .speaking
        talk = says ? "speak" : "listen"
        expr = listening ? "35" : says ? "39" : now - voiceAt < 520 ? "31" : "30"
        hear = listening ? v.input : 0
        if hear > 0.55 && (sonar.isEmpty || now - sonar.last! > 360) { sonar.append(now) }
        voiceRows(v)
        return says ? v.output : nil
    }
}

#if DEBUG
extension StarEngine {
    /// launch with `-voiceScene listening|thinking|speaking|speakingLong|error|download`: the sky shows that voice state with made-up words and levels
    func runVoiceScene(_ name: String) {
        let t0 = nowMs()
        let ask = "明天下午三点有什么安排，顺便提醒我晚上给 Mia 回电话"
        let short = "明天下午三点有一个二面，晚上八点半给 Mia 回电话。"
        let answer = "明天下午三点你有一个和 Lumen Labs 的二面，在线上进行，大概四十五分钟。之前我会把你上次面试的笔记整理出来。晚上八点半给 Mia 回电话，我到时候提醒你。路上要不要我先查一下天气和出门的时间？"
            + "另外后天上午十点你约了牙医，在市中心，坐 32 路过去大约三十分钟，我会在九点钟提醒你出门。周五的作品集评审还差首页的最后两张图，我可以今晚先帮你把素材整理好，明天上午再一起过一遍。"
        voiceFake = { now in
            var v = VoiceShot()
            switch name {
            case "listening": v.state = .listening; v.partial = ask; v.input = 0.3 + 0.7 * voiceLevel(now / 1000 * 1.3)
            case "thinking": v.state = .thinking; v.transcript = ask
            case "speaking": v.state = .speaking; v.transcript = ask; v.reply = String(short.prefix(Int((now - t0) / 120))); v.output = voiceLevel(now / 1000)
            case "speakingLong": v.state = .speaking; v.transcript = ask; v.reply = String(answer.prefix(Int((now - t0) / 90))); v.output = voiceLevel(now / 1000)
            case "error": v.error = "没有麦克风权限"
            case "download": v.progress = 0.43
            default: break
            }
            return v
        }
        wake()
    }
}
#endif
