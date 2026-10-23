import Foundation
import Observation
import UIKit

// The footprint's ▶ replay as it is played (brief, section 3): `FootprintPlayback` is the state machine (rest, playing, paused: a touch pauses, ▶ goes on from the playhead, the end goes back to rest),
// `FootprintPlayer` steps it by a display link and hands each frame of the schedule (`FootprintReplay`) to whoever draws it. A finger on the bar or a curve goes through the same schedule, so that the
// dot is placed by one function whether the replay plays it or a finger holds it.

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
    /// the one being played or paused, or under a finger
    @ObservationIgnored private(set) var replay: FootprintReplay?
    /// each frame, with the replay it is of and whether it plays (else a finger holds it); the last one played is the replay's end
    @ObservationIgnored var emit: ((FootprintReplay.Frame, FootprintReplay, Bool) -> Void)?
    /// the replay ran out
    @ObservationIgnored var ended: (() -> Void)?
    /// A finger's playhead, in seconds of the replay: it eases to where the finger points, so that the dot and the camera glide along the line even where a little of the finger is a long
    /// way on the ground (a curve, a quick drag). Nil while no finger holds it.
    @ObservationIgnored private var finger: FootprintCamera.Spring?
    @ObservationIgnored private var lifted = false
    @ObservationIgnored private var link: CADisplayLink?
    @ObservationIgnored private var lastTick = 0.0

    /// ▶ on `r`: a replay at rest starts, a paused one goes on from its playhead
    func play(_ r: FootprintReplay) {
        if phase == .rest || replay?.duration != r.duration { playback = FootprintPlayback(duration: r.duration) }
        replay = r
        finger = nil; lifted = false
        playback.play()
        send(playback.seconds, playing: true)
        startLink()
    }

    /// a touch pauses it
    func touch() {
        guard phase == .playing else { return }
        playback.touch()
        if finger == nil { stopLink() }
    }

    /// A finger on the bar or a curve points at `seconds` of `r`. The first touch of a replay at rest is taken at once (the camera flies down to it); after that, and from a paused
    /// replay's playhead, the playhead eases there.
    func point(at seconds: Double, of r: FootprintReplay) {
        if phase == .playing { playback.touch() }
        if replay?.scope != r.scope || replay?.duration != r.duration {
            playback.stop(); replay = r; finger = nil
        }
        let to = min(max(seconds, 0), r.duration)
        if finger == nil { finger = FootprintCamera.Spring(phase == .paused ? playback.seconds : to) }
        finger?.target = to
        lifted = false
        if link == nil { send(finger?.value ?? to, playing: false) }          // the first frame at once, not a frame later
        startLink()
    }

    /// the finger lifted: a paused replay's playhead eases on to where it was left, then stands; at rest nothing more goes out
    func lift() {
        guard finger != nil else { return }
        if phase == .paused { lifted = true } else { finger = nil; stopLink() }
    }

    func stop() { playback.stop(); stopLink(); replay = nil; finger = nil; lifted = false }

    /// the display link's step, and the test's: `dt` seconds on
    func advance(_ dt: Double) {
        guard let r = replay else { return }
        if var f = finger {
            let w = 2 * .pi / TLMotion.mapResponse
            f.step(dt: dt, omega: w)
            let done = f.settled(within: 1e-3, omega: w)
            if done { f.value = f.target; f.velocity = 0 }
            finger = f
            playback.seek(f.value)
            send(f.value, playing: false)
            if done && lifted { finger = nil; lifted = false; stopLink() }
            return
        }
        guard phase == .playing else { return }
        let done = playback.tick(dt)
        send(done ? r.duration : playback.seconds, playing: true)
        if done { stopLink(); ended?() }
    }

    private func send(_ seconds: Double, playing: Bool) {
        guard let r = replay else { return }
        emit?(r.frame(at: seconds), r, playing)
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
