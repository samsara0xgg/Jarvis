#if DEBUG
import Foundation

/// Set while a `-scene 星环…` shows its made-up day, so the engine does not swap the world back for the unpaired sample.
enum RingScene { static var on = false }

extension StarEngine {
    /// `-scene 星环 -fakeNow 2026-10-11T20:30`: an invented day on his thread, as if the brain had served it (sleep, a morning work block, a walk, a bus ride,
    /// an afternoon exercise walk, a call, an evening work block). `星环近@08:40` is the near view resting at that time. Made-up data only.
    func runRingScene(near: String?) {
        RingScene.on = true
        switchWorld(real: true)
        let cal = Cal.cur, base = cal.origin.timeIntervalSince1970 * 1000
        func ms(_ minute: Double) -> Int64 { Int64(base + minute * 60_000) }
        var items: [[String: Any]] = []
        func add(_ kind: String, _ from: Double, _ to: Double, _ more: [String: Any] = [:]) {
            var o: [String: Any] = ["id": "ring-\(items.count)", "lane": "you", "kind": kind, "state": "done", "start_ms": ms(from), "end_ms": ms(to)]
            o.merge(more) { $1 }
            items.append(o)
        }
        add("sleep", 0, 430, ["minutes": 430])
        add("stay", 430, 510, ["place": "Home"])
        add("move", 510, 525, ["activity": "walking"])
        add("ride", 525, 558, ["route": "12", "board_stop": "Stop A", "alight_stop": "Stop B"])
        add("stay", 558, 1020, ["place": "Office"])
        add("work", 575, 700, ["active_s": 6600, "projects": [["name": "Project A", "seconds": 6600]]])
        add("move", 780, 850, ["activity": "walking"])
        add("call", 900, 925, ["app": "Phone"])
        add("move", 1020, 1040, ["activity": "walking"])
        add("stay", 1040, 1230, ["place": "Home"])
        add("work", 1100, 1210, ["active_s": 3000, "projects": [["name": "Project B", "seconds": 3000]]])
        let reply: [String: Any] = ["date": cal.iso(0), "tz": "UTC", "start_ms": ms(0), "end_ms": ms(DAY), "now_ms": ms(1230), "items": items, "missing": [String]()]
        guard let data = try? JSONSerialization.data(withJSONObject: reply), let day = try? JSONDecoder().decode(DayReply.self, from: data) else { return }
        EV = RealDay.shared.rebuild([cal.iso(0): day], now: nowT())
        for e in EV { e.born = -1e9 }
        sprites.minis.removeAll(); epoch += 1; wake()
        if let w = near {
            let hm = w.split(separator: ":").compactMap { Double($0) }
            guard hm.count == 2 else { return }
            let t = Double(floorDay(nowT())) * DAY + hm[0] * 60 + hm[1]
            setLevel(-1); fs.value = t; fs.velocity = 0; goal = t; mode = .idle
        }
    }
}
#endif
