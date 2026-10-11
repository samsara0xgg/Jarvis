#if DEBUG
import Foundation

/// Set while a `-scene 星环…` shows its made-up day, so the engine does not swap the world back for the unpaired sample.
enum RingScene { static var on = false }

extension StarEngine {
    /// `-scene 星环 -fakeNow 2026-10-11T20:30`: an invented day on his thread, as if the brain had served it (sleep, a morning work block, a walk, a bus ride,
    /// an afternoon exercise walk, a call, an evening work block), with the places he keeps and what the newer brain adds to them.
    /// `星环近@08:40` is the near view resting at that time; `星环旧` is the same day as the old brain served it, without the new fields.
    /// `星环家` is a day spent in one kept place: at home since last evening with no departure, and several work blocks on one project (two of them marked). Made-up data only.
    func runRingScene(near: String?, old: Bool = false, home: Bool = false) {
        RingScene.on = true
        switchWorld(real: true)
        let cal = Cal.cur, base = cal.origin.timeIntervalSince1970 * 1000, nowM = nowT()
        func ms(_ minute: Double) -> Int64 { Int64(base + minute * 60_000) }
        var items: [[String: Any]] = []
        /// a span of the past: it is cut at now, and one that has not begun is left out (`ahead`: a planned one, which is what the calendar and the pin bring)
        @discardableResult func add(_ kind: String, _ from: Double, _ to: Double, _ more: [String: Any] = [:], ahead: Bool = false) -> String? {
            if from > nowM && !ahead { return nil }
            var o: [String: Any] = ["id": "ring-\(items.count)", "lane": "you", "kind": kind, "state": to <= nowM ? "done" : from <= nowM ? "now" : "next", "start_ms": ms(from)]
            if to <= nowM || ahead { o["end_ms"] = ms(to) } else { o["end_ms"] = NSNull() }
            o.merge(more) { $1 }
            if old { o["kept_place"] = nil }
            items.append(o)
            return o["id"] as? String
        }
        func finish(_ extra: [String: Any]) {
            var reply: [String: Any] = ["date": cal.iso(0), "tz": "UTC", "start_ms": ms(0), "end_ms": ms(DAY), "now_ms": ms(nowM), "items": items, "missing": [String]()]
            reply.merge(extra) { $1 }
            guard let data = try? JSONSerialization.data(withJSONObject: reply), let day = try? JSONDecoder().decode(DayReply.self, from: data) else { return }
            EV = RealDay.shared.rebuild([cal.iso(0): day], now: nowT())
            for e in EV { e.born = -1e9 }
            sprites.minis.removeAll(); epoch += 1; wake()
        }
        func kept(_ id: String, _ name: String, _ role: String) -> [String: Any] { ["id": id, "name": name, "role": role] }
        var extra: [String: Any] = [:]
        if home {
            add("stay", -246, 1440, ["place": "12 Example St, Town", "kept_place": kept("p-home", "home", "home")])        // since yesterday 19:54, still there
            add("sleep", -30, 445, ["minutes": 475])
            let jarvis: (Double, Double, Double) -> String? = { from, to, active in
                add("work", from, to, ["active_s": active, "projects": [["name": "Jarvis", "seconds": active]]]) }
            let w1 = jarvis(665, 798, 6780), w3 = jarvis(860, 892, 1500)         // 11:05-13:18 (1h53m active), 14:20-14:52
            jarvis(823, 831, 420); jarvis(902, 920, 900)                          // 13:43-13:51, 15:02-15:20
            let marks = [(w1, "Jarvis 项目"), (w3, "Jarvis 项目")]
            extra["line"] = ["text": "今天一直在家，下午的时间几乎都给了 Jarvis 项目。", "draft": true, "written_at": "2026-10-11T15:30:00+00:00"]
            extra["highlights"] = marks.compactMap { id, label in id.map { ["id": $0, "label": label, "by": "her"] } }
        } else {
            add("sleep", 0, 430, ["minutes": 430])
            add("stay", 430, 510, ["place": "12 Example St, Town", "kept_place": kept("p-home", "Home", "home")])
            add("move", 510, 525, ["activity": "walking"])
            add("ride", 525, 558, ["route": "12", "board_stop": "Stop A", "alight_stop": "Stop B"])
            add("stay", 558, 775, ["place": "Science Hall, Campus Rd", "kept_place": kept("p-campus", "Campus", "school")])
            let focus = add("work", 575, 700, ["active_s": 6600, "projects": [["name": "Project A", "seconds": 6600]]])
            let walk = add("move", 775, 850, ["activity": "walking"])
            add("stay", 850, 960, ["place": "The Cafe, Main St", "kept_place": kept("p-cafe", "Cafe", "other")])
            let call = add("call", 900, 925, ["app": "Phone"])
            add("stay", 960, 1020, ["place": "Corner Library, 3 Example Ave"])
            add("move", 1020, 1040, ["activity": "walking"])
            add("stay", 1040, 1440, ["place": "12 Example St, Town", "kept_place": kept("p-home", "Home", "home")])
            add("work", 1100, 1210, ["active_s": 3000, "projects": [["name": "Project B", "seconds": 3000]]])
            if !old {
                add("event", 600, 650, ["id": "event-3f9a1c2b7d10", "title": "Lecture"], ahead: true)
                add("event", nowM + 90, nowM + 150, ["id": "event-a07be4419c2d", "title": "Study group"], ahead: true)
                add("departure", nowM + 45, nowM + 45, ["id": "departure-1", "title": "12 · Stop A", "route": "12", "board_stop": "Stop A", "to": "Downtown", "leave_at_ms": ms(nowM + 35)], ahead: true)
                extra["labels"] = [["title": "Open Day", "source": "calendar"]]
                let marks: [(String?, String, String)] = [(focus, "Deep work", "her"), (call, "Long call", "her"), (walk, "Walk in the sun", "him")]
                extra["highlights"] = marks.compactMap { id, label, by in id.map { ["id": $0, "label": label, "by": by] } }
                extra["line"] = ["text": "今天走了不少路，下午在咖啡馆待得最久。晚上早点休息。", "draft": true, "written_at": "2026-10-11T15:30:00+00:00"]
            }
        }
        finish(extra)
        if let w = near {
            let hm = w.split(separator: ":").compactMap { Double($0) }
            guard hm.count == 2 else { return }
            let t = Double(floorDay(nowT())) * DAY + hm[0] * 60 + hm[1]
            setLevel(-1); fs.value = t; fs.velocity = 0; goal = t; mode = .idle
        }
    }
}
#endif
