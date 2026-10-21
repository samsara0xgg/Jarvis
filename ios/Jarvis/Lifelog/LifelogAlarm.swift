import Foundation
import UserNotifications
import os

// The day's recording tells him when it stops while the switch is on (ADR 0375). A notification waits in iOS's queue, due a minute and a
// half ahead, and the microphone's audio keeps pushing it back; so whatever stops the audio (the app frozen in the background, killed, its
// engine dead without a word) it fires by itself, with no need for the app to run. What the recorder knows about a stop (a start refused in
// the background, an interruption) replaces it with the reason. A pause chosen by him or for a call (the switch off, a call, another app's
// audio: ADR 0348) takes it away. Tapping it opens the app, and the recorder starts again in the foreground.

@MainActor
final class LifelogAlarm {
    static let shared = LifelogAlarm()
    private static let id = "com.yilunshi.jarvis.lifelog.stopped"
    /// it fires this long after the last audio
    private static let silence: TimeInterval = 90
    /// the audio pushes it back at most this often
    nonisolated private static let renewEvery: TimeInterval = 20

    enum Why: String { case silent, background, interrupted }

    /// the switch is on and no call is up
    private var expecting = false
    /// the recorder left the microphone to another app's audio
    private var yielded = false
    /// a stop with its reason is pending or was shown: the next audio takes it away
    private var told = false
    /// uptimes of the last audio and of the last push-back (0 while nothing is watched); written on the audio thread
    nonisolated private static let times = OSAllocatedUnfairLock(initialState: (heard: 0.0, renewed: 0.0))

    private var watching: Bool { expecting && !yielded }

    /// What the recorder applies: the switch on and no call.
    func expect(_ on: Bool) {
        guard on != expecting else { return }
        expecting = on
        changed()
    }

    /// The recorder yielded to another app's audio (true), or took the microphone back (false).
    func yield(_ y: Bool) {
        guard y != yielded else { return }
        yielded = y
        changed()
    }

    /// Audio came from the microphone (the audio thread): the notification is pushed back, at most every `renewEvery`.
    nonisolated static func heard() {
        let now = ProcessInfo.processInfo.systemUptime
        let due = times.withLock { t -> Bool in
            t.heard = now
            guard t.renewed > 0, now - t.renewed >= renewEvery else { return false }
            t.renewed = now
            return true
        }
        if due { Task { @MainActor in shared.renew() } }
    }

    /// How long since the microphone last gave audio (infinite if it never did).
    nonisolated static var silentFor: TimeInterval {
        let h = times.withLock { $0.heard }
        return h > 0 ? ProcessInfo.processInfo.systemUptime - h : .infinity
    }

    /// The recorder knows why it stopped: he is told after `after` seconds (now for 0) unless audio comes back first.
    func stopped(_ why: Why, after: TimeInterval = 0) {
        guard watching else { return }
        lifelogNote("telling him the recording stopped (\(why.rawValue))\(after > 0 ? " unless it is back within \(Int(after)) s" : "")")
        told = true
        schedule(why, after: after)
    }

    private func renew() {
        guard watching else { return }
        if told {
            told = false
            UNUserNotificationCenter.current().removeDeliveredNotifications(withIdentifiers: [Self.id])
            lifelogNote("audio is back: the stop notice is taken away")
        }
        schedule(.silent, after: Self.silence)
    }

    private func changed() {
        if watching {
            let started = Self.times.withLock { t -> Bool in
                guard t.renewed == 0 else { return false }
                t.renewed = ProcessInfo.processInfo.systemUptime
                return true
            }
            guard started else { return }
            schedule(.silent, after: Self.silence)              // audio that never starts is a stop too
        } else {
            Self.times.withLock { $0.renewed = 0 }
            told = false
            let c = UNUserNotificationCenter.current()
            c.removePendingNotificationRequests(withIdentifiers: [Self.id])
            c.removeDeliveredNotifications(withIdentifiers: [Self.id])
        }
    }

    /// One request under one identifier: each add replaces the one pending.
    private func schedule(_ why: Why, after: TimeInterval) {
        let c = UNMutableNotificationContent()
        c.title = Words.Lifelog.stoppedTitle
        let reason = switch why {
        case .silent: Words.Lifelog.whySilent
        case .background: Words.Lifelog.whyBackground
        case .interrupted: Words.Lifelog.whyInterrupted
        }
        c.body = Words.Lifelog.stoppedBody(reason)
        c.sound = .default
        c.interruptionLevel = .timeSensitive                // a driving Focus lets it through
        let trigger = after > 0 ? UNTimeIntervalNotificationTrigger(timeInterval: after, repeats: false) : nil
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: Self.id, content: c, trigger: trigger)) { error in
            if let error { lifelogNote("stop notice could not be queued: \(error.localizedDescription)") }
        }
    }
}
