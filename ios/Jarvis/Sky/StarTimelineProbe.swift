#if DEBUG
import UIKit

/// How long each frame took while the timeline moves (a debug build only; `-scene 时间线:测帧`, Sky/StarTimelineScene.swift). A display link notes the gap between one frame and the
/// next, a script of steps moves the timeline the way a hand would, and each step with a label reports its median, its 95th percentile and its worst frame, and how many frames
/// came later than one and a half and two frame times, and for each late frame how long the main thread's longest turn was. It is printed and written to frame-probe.txt (and every gap to frame-probe.json) in the app's tmp, where the host reads it.
final class FrameProbe: NSObject {
    struct Step {
        var label: String?                              // none: a pause nobody measures
        var seconds: Double
        var start: () -> Void = {}                      // once, as the step begins
        var frame: (Double) -> Void = { _ in }          // on every frame, with how far through the step it is (0...1)
    }

    private var steps: [Step] = []
    private var at = 0, began = 0.0, last = 0.0, frameMs = 1000.0 / 60
    private var gaps: [Double] = [], busies: [Double] = []
    private var lines: [String] = []
    private var raw: [String: [Double]] = [:]
    private var link: CADisplayLink?
    private var done: ([String]) -> Void = { _ in }
    // how long the main thread was busy in each turn of its run loop (from waking to going back to sleep), to tell a late frame that the app made from one the display did
    private var observer: CFRunLoopObserver?
    private var woke = 0.0, turns: [(end: Double, ms: Double)] = [], called = 0.0
    private var slow: [String] = []

    func note(_ s: String) { lines.append(s) }

    func run(_ steps: [Step], done: @escaping ([String]) -> Void) {
        self.steps = steps; self.done = done
        at = 0; began = 0; last = 0; gaps = []
        lines.insert("screen \(UIScreen.main.maximumFramesPerSecond) Hz", at: 0)
        let l = CADisplayLink(target: self, selector: #selector(tick))
        l.preferredFrameRateRange = CAFrameRateRange(minimum: 60, maximum: 120, preferred: 120)
        l.add(to: .main, forMode: .common)
        link = l
        let acts = CFRunLoopActivity([.afterWaiting, .beforeWaiting]).rawValue
        observer = CFRunLoopObserverCreateWithHandler(kCFAllocatorDefault, acts, true, Int(Int32.max)) { [weak self] _, act in
            guard let self else { return }
            let now = CACurrentMediaTime()
            if act == .afterWaiting { woke = now } else if woke > 0 { turns.append((now, (now - woke) * 1000)); woke = 0 }
        }
        CFRunLoopAddObserver(CFRunLoopGetMain(), observer, .commonModes)
    }

    @objc private func tick(_ l: CADisplayLink) {
        let now = l.timestamp
        frameMs = (l.targetTimestamp - l.timestamp) * 1000
        let gap = last > 0 ? (now - last) * 1000 : 0
        last = now
        let ran = CACurrentMediaTime()
        if gap > 1.5 * frameMs, called > 0, let t = steps[safe: at] {
            // the longest turn of the main thread that ended since the last frame: if it fills the gap the app was busy, if not the frame was late for another reason
            let busy = turns.filter { $0.end > called }.map(\.ms).max() ?? 0
            slow.append(String(format: "  %@ +%.2fs: gap %.0f ms, longest main-thread turn %.0f ms", t.label ?? "(pause)", ran - (began > 0 ? began : ran), gap, busy))
        }
        let work = turns.filter { $0.end > called && $0.end <= ran }.reduce(0) { $0 + $1.ms }          // what the main thread did since the last frame
        called = ran
        turns.removeAll { $0.end < ran - 1 }
        guard at < steps.count else { finish(); return }
        if began == 0 {                                 // the first frame of a step only starts it: the gap before it belongs to the step before
            began = now; gaps = []; busies = []
            steps[at].start()
            steps[at].frame(0)
            return
        }
        gaps.append(gap); busies.append(work)
        let p = (now - began) / steps[at].seconds
        if p >= 1 {
            if let label = steps[at].label { lines.append(Self.line(label, gaps, busies, frameMs)); raw[label] = gaps.map { ($0 * 10).rounded() / 10 } }
            at += 1; began = 0
            return
        }
        steps[at].frame(p)
    }

    private func finish() {
        link?.invalidate(); link = nil
        if let o = observer { CFRunLoopRemoveObserver(CFRunLoopGetMain(), o, .commonModes); observer = nil }
        lines += ["late frames:"] + slow
        let text = lines.joined(separator: "\n")
        print("[frame-probe]\n" + text)
        try? text.write(toFile: NSTemporaryDirectory() + "frame-probe.txt", atomically: true, encoding: .utf8)
        if let data = try? JSONSerialization.data(withJSONObject: raw, options: [.sortedKeys]) { try? data.write(to: URL(fileURLWithPath: NSTemporaryDirectory() + "frame-probe.json")) }
        done(lines)
    }

    /// `n` frames; the median and the 95th percentile of the gaps between them, the worst, and how many came later than 1.5 and 2 frame times (a frame time is the screen's own)
    static func line(_ label: String, _ gaps: [Double], _ busies: [Double], _ frameMs: Double) -> String {
        guard !gaps.isEmpty else { return "\(label): no frames" }
        let s = gaps.sorted(), b = busies.sorted()
        func q(_ v: [Double], _ p: Double) -> Double { v[min(v.count - 1, Int(p * Double(v.count)))] }
        return String(format: "%@: n=%d p50=%.1f p95=%.1f worst=%.1f ms (frame %.1f), slower than 1.5 frames: %d, than 2 frames: %d; main thread per frame p50=%.1f p95=%.1f max=%.1f ms",
                      label, s.count, q(s, 0.5), q(s, 0.95), s[s.count - 1], frameMs, s.filter { $0 > 1.5 * frameMs }.count, s.filter { $0 > 2 * frameMs }.count,
                      q(b, 0.5), q(b, 0.95), b[b.count - 1])
    }
}

private extension Array {
    subscript(safe i: Int) -> Element? { indices.contains(i) ? self[i] : nil }
}
#endif
