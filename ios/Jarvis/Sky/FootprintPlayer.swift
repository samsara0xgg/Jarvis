import Foundation
import Observation
import UIKit

// The footprint's ▶ replay as it is played (brief, section 3): `FootprintPlayback` is the state machine (rest, playing, paused: a touch pauses, ▶ goes on from the playhead, the end goes back to rest),
// `FootprintPlayer` steps it by a display link and hands each frame of the schedule (`FootprintReplay`) to whoever draws it.

struct FootprintPlayback: Equatable {
    enum Phase: Equatable { case rest, playing, paused }
    private(set) var phase = Phase.rest
    /// the playhead, in seconds of the replay
    private(set) var seconds = 0.0
    var duration: Double

    init(duration: Double) { self.duration = duration }

    /// ▶: from the start at rest (or when the playhead stands at the end), else from the playhead
    mutating func play() {
        guard phase != .playing else { return }
        if phase == .rest || seconds >= duration { seconds = 0 }
        phase = .playing
    }

    /// a touch on the bar, the map or a curve
    mutating func touch() { if phase == .playing { phase = .paused } }

    /// a finger moves the playhead of a paused replay
    mutating func seek(_ s: Double) { if phase == .paused { seconds = min(max(s, 0), duration) } }

    /// `dt` seconds go by; true when that ran the replay out, which is back at rest
    mutating func tick(_ dt: Double) -> Bool {
        guard phase == .playing else { return false }
        seconds += dt
        guard seconds >= duration else { return false }
        phase = .rest; seconds = 0
        return true
    }

    mutating func stop() { phase = .rest; seconds = 0 }
}

@Observable
final class FootprintPlayer {
    private(set) var playback = FootprintPlayback(duration: 0)
    var phase: FootprintPlayback.Phase { playback.phase }
    /// the one being played or paused
    @ObservationIgnored private(set) var replay: FootprintReplay?
    /// each frame, with the replay it is of; the last one is the replay's end
    @ObservationIgnored var emit: ((FootprintReplay.Frame, FootprintReplay) -> Void)?
    /// the replay ran out
    @ObservationIgnored var ended: (() -> Void)?
    @ObservationIgnored private var link: CADisplayLink?
    @ObservationIgnored private var lastTick = 0.0

    /// ▶ on `r`: a replay at rest starts, a paused one goes on from its playhead
    func play(_ r: FootprintReplay) {
        if phase == .rest || replay?.duration != r.duration { playback = FootprintPlayback(duration: r.duration) }
        replay = r
        playback.play()
        send(playback.seconds)
        startLink()
    }

    /// a touch pauses it
    func touch() {
        guard phase == .playing else { return }
        playback.touch(); stopLink()
    }

    /// where a finger on the bar or a curve has put the playhead of a paused replay
    func seek(_ seconds: Double) { playback.seek(seconds) }

    func stop() { playback.stop(); stopLink(); replay = nil }

    /// the display link's step, and the test's: `dt` seconds on
    func advance(_ dt: Double) {
        guard phase == .playing else { return }
        let done = playback.tick(dt)
        guard let r = replay else { return }
        send(done ? r.duration : playback.seconds)
        if done { stopLink(); ended?() }
    }

    private func send(_ seconds: Double) {
        guard let r = replay else { return }
        emit?(r.frame(at: seconds), r)
    }

    private func startLink() {
        guard link == nil else { return }
        lastTick = 0
        let l = CADisplayLink(target: LinkTarget { [weak self] l in
            guard let self else { return }
            let dt = self.lastTick == 0 ? l.targetTimestamp - l.timestamp : l.timestamp - self.lastTick
            self.lastTick = l.timestamp
            self.advance(min(dt, 0.1))
        }, selector: #selector(LinkTarget.fire(_:)))
        l.add(to: .main, forMode: .common)
        link = l
    }

    private func stopLink() { link?.invalidate(); link = nil }

    private final class LinkTarget: NSObject {
        let run: (CADisplayLink) -> Void
        init(_ run: @escaping (CADisplayLink) -> Void) { self.run = run }
        @objc func fire(_ l: CADisplayLink) { run(l) }
    }
}
