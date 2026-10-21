// words: legacy
#if DEBUG
import UIKit
import SwiftUI

/// A made-up day for the timeline, as if the brain had served it: the same day every launch, with now fixed at 21:50. Made-up data only.
enum TimelineFixture {
    static let now = 21 * 60 + 50.0

    /// the id of the event his mail made in `today(mail: true)`, as the brain names a commitment: `commitment-<hex>@<day>`
    static func commitmentId(_ cal: Cal) -> String { "commitment-3f9a1c2e7b4d4a0c9e5f6a7b8c9d0e1f@" + cal.iso(0) }

    /// today: the night, home, a drive, a bite, home again, the bus to school, school, the bus home, and the evening still going, with his Mac work, what he spent and a chat with Claude along the way (`late`: a talk that has just ended is there too; `mail`: the class his mail made is a commitment of the brain's, by its id; `doing`: the brain's doing lane is there too, with what he was doing and the items under each stretch, and the phone's and the Mac's media, messages and calls)
    static func today(_ cal: Cal, late: Bool = false, mail: Bool = false, doing: Bool = false) -> DayReply { build(cal, day: 0) { b in
        // the night as Health recorded it (the first stage starts before the night does: only what lies inside it is the night's)
        b.add("sleep", 55, 545, ["minutes": 485, "stages": b.stages([(40, 55, "in_bed"), (55, 100, "core"), (100, 170, "deep"), (170, 215, "core"), (215, 245, "rem"), (245, 290, "core"), (290, 335, "deep"),
                                                                  (335, 380, "core"), (380, 430, "rem"), (430, 438, "awake"), (438, 490, "core"), (490, 530, "rem"), (530, 545, "awake")])])
        b.add("stay", 545, 685, ["place": "12 Example St, Town", "kept_place": b.kept("p-home", "家", "home"), "lat": 48.4516, "lng": -123.3489, "accuracy_m": 18])
        b.add("talk", 612, 622, ["id": "talk-1", "lane": "her", "turns": 6])
        b.add("move", 685, 785, ["activity": "automotive"])
        b.add("move", 785, 795, ["activity": "walking"])
        b.add("stay", 795, 835, ["place": "A&W, Shelbourne St, Victoria", "lat": 48.4538, "lng": -123.3268, "accuracy_m": 25])
        b.add("move", 835, 845, ["activity": "walking"])
        b.add("stay", 845, 890, ["place": "12 Example St, Town", "kept_place": b.kept("p-home", "家", "home"), "lat": 48.4516, "lng": -123.3489])
        b.add("ride", 890, 919, ["route": "27", "board_stop": "Hillside Centre", "alight_stop": "UVic Exchange", "credit": "BC Transit", "ended_reason": "left_vehicle"])
        b.add("stay", 919, 1095, ["place": "Engineering Lab Wing, Finnerty Rd", "kept_place": b.kept("p-uvic", "UVic", "school"), "lat": 48.4634, "lng": -123.3117, "accuracy_m": 20])
        b.add("picture", 962, 962, ["id": "picture-pic-demo-1", "attachment": "pic-demo-1", "note": "黑板上写着积分公式，下面一行是作业要求。\n第三题的符号有些看不清。"])
        b.add("call", 1000, 1018, ["id": "call-demo-1", "app": "FaceTime"])
        b.add("ride", 1095, 1130, ["route": "27", "board_stop": "UVic Exchange", "alight_stop": "Hillside Centre", "ended_reason": "passed_stop"])
        b.add("stay", 1130, nil, ["place": "12 Example St, Town", "kept_place": b.kept("p-home", "家", "home"), "lat": 48.4516, "lng": -123.3489])
        b.add("talk", 1240, 1262, ["id": "talk-2", "lane": "her", "turns": 14])
        if late { b.add("talk", 1305, 1309, ["id": "talk-3", "lane": "her", "turns": 4]) }
        b.add("event", 1320, 1350, ["id": mail ? commitmentId(cal) : "event-demo-1", "title": "陶艺课", "source": "mail", "confirmed": false, "place": "Studio 4"])
        b.add("reminder", 1380, nil, ["id": "reminder-demo-1", "lane": "her", "text": "给 Mia 回电话", "fired": false])
        // what he did on the Mac, what he spent, a chat with Claude, and the bus he means to take (explicit ids: the ones above are numbered by their place in this list)
        b.add("work", 630, 680, ["id": "work-demo-1", "active_s": 2520, "projects": [["name": "Jarvis", "seconds": 2520]], "apps": [["name": "Xcode", "seconds": 1560], ["name": "Ghostty", "seconds": 960]]])
        b.add("work", 1175, 1300, ["id": "work-demo-2", "active_s": 5880, "projects": [["name": "Jarvis", "seconds": 3300], ["name": "论文", "seconds": 2580]],
                                   "apps": [["name": "Xcode", "seconds": 2700], ["name": "Safari", "seconds": 2100], ["name": "Ghostty", "seconds": 1080]]])
        b.add("payment", 800, 800, ["id": "payment-demo-1", "source": "alipay", "amount": "12.40", "currency": "CAD", "direction": "out", "counterparty": "A&W", "text": "Teen Burger Combo"])
        b.add("claude_chat", 1210, 1235, ["id": "claude-demo-1", "source": "claude", "text": "积分换元的符号问题", "messages": 14])
        b.add("departure", 1400, 1400, ["id": "departure-demo-1", "title": "12 · Hillside Centre", "route": "12", "board_stop": "Hillside Centre", "to": "UVic", "leave_at_ms": b.ms(1390)])
        // a calendar event, money in, a reminder he answered and one that rang and waits for him, a run, and six sessions of Claude Code in two stays (a recap for most)
        b.add("event", 930, 980, ["id": "event-demo-2", "title": "线性代数小测", "place": "ECS 123"])
        b.add("payment", 1205, 1205, ["id": "payment-demo-2", "source": "wechat_pay", "amount": "50.00", "currency": "CAD", "direction": "in", "counterparty": "Mia", "text": "饭钱"])
        b.add("reminder", 560, nil, ["id": "reminder-demo-3", "lane": "her", "text": "带伞", "fired": true, "acknowledged": true])
        b.add("reminder", 1290, nil, ["id": "reminder-demo-2", "lane": "her", "text": "吃药", "fired": true, "acknowledged": false])
        b.add("workout", 1020, 1055, ["id": "workout-demo-1", "activity": "running", "text": "Outdoor Run", "distance_m": 3200, "energy_kcal": 240, "avg_hr": 142])
        for (n, from, to, title, project, account, recap) in [
            (1, 570.0, 605.0, "统一时间线的行高", "Jarvis", "a", "单行 44、带说明 56、地点标题 44 写进了 TLMetrics，旧的 40 和 52 不再用。测试都过了，下一步是行内的图标圈。"),
            (2, 612.0, 650.0, "接上天气和身体数据", "Jarvis", "a", "DayReply 多了 weather 和 summary，解码时每个字段单独容错，坏一个不会带走整天。"),
            (3, 656.0, 680.0, "整理 spec 第 11 节", "Jarvis", "b", nil),
            (4, 1180.0, 1218.0, "对照桌面端的行文案", "Jarvis", "a", "手机和 Mac 的行用同一套词：响过了、你知道了、还没确认。"),
            (5, 1235.0, 1268.0, "论文第三章的数据", "论文", "b", "把三组实验的结果合成一张表，图还没画。"),
            (6, 1276.0, 1306.0, "给等你的卡片补测试", "Jarvis", "a", nil),
        ] as [(Int, Double, Double, String, String, String, String?)] {
            var more: [String: Any] = ["id": "code-s\(n)", "title": title, "project": project, "account": account, "session_id": "s\(n)"]
            if let recap { more["detail"] = recap }
            b.add("code", from, to, more)
        }
        b.extra["line"] = ["text": "今天去了学校，下午在 UVic 待得最久，晚上回家早点休息。", "draft": true, "written_at": "2026-10-19T21:30:00+00:00"]
        b.extra["missing"] = ["calendar"]
        b.extra["weather"] = ["condition": "mostly cloudy", "high": 14, "low": 8, "sunrise_ms": b.ms(454), "sunset_ms": b.ms(1091)]
        b.extra["summary"] = ["steps": 8240, "resting_hr": 58, "hrv": 42]
        if doing {
            // what his phone and the Mac brought that the rest of the day does not have: phone use, music, messages (one in the evening too) and a call on the phone
            b.add("screen", 850, 885, ["id": "screen-demo-1", "device": "iPhone", "active_s": 1500, "apps": [["name": "微信", "bundle": "com.tencent.xin", "minutes": 14, "category": "social"],
                                                                                                      ["name": "小红书", "bundle": "com.xingin.discover", "minutes": 8, "category": "social"], ["name": "Safari", "bundle": "com.apple.mobilesafari", "minutes": 3, "category": "browser"]]])
            b.add("media", 860, 890, ["id": "media-demo-1", "device": "iPhone", "app": "Music", "active_s": 1620, "titles": [["title": "Sunday Morning", "artist": "Maroon 5"]], "route": "AirPods"])
            b.add("messages", 855, 872, ["id": "messages-demo-1", "device": "iPhone", "app": "WeChat", "conversation": "Mia", "is_group": false, "sent": 4, "received": 6])
            b.add("messages", 1262, 1270, ["id": "messages-demo-2", "device": "iPhone", "app": "WeChat", "conversation": "论文小组", "is_group": true, "sent": 2, "received": 9])
            b.add("phone_call", 868, 876, ["id": "phone_call-demo-1", "device": "iPhone", "app": "Phone", "direction": "received", "person": "妈妈", "duration_s": 480])
            // What he was doing, as the brain's doing lane serves it, with the items under each stretch (ADR 0307: a lasting item in every stretch it overlaps, a point in the one it falls in)
            // and the place he keeps that it was at. The morning and the evening at home are around one stay each, the afternoon at UVic is a stay under four stretches, a Claude Code session
            // begins in one stretch and ends in the next, a long drive is in the model's words, a lunch he told her, a rest it asks him about, homework it is not sure of, a stretch nobody
            // could say anything of, and the evening's work goes on to now. The two stretches of work and the rest say where his attention went (ADR 0314).
            b.doing(545, 575, "起床吃早饭", "meal", "inferred", detail: "起床后在家吃了早饭，没有别的记录", place: "p-home", items: ["stay-1", "reminder-demo-3", "code-s1"], clues: ["早上九点刚醒", "家里的 Wi-Fi 一直在线"])
            b.doing(575, 685, "Jarvis", "work", "observed", detail: "一直在改时间线和测试", place: "p-home", items: ["stay-1", "code-s1", "code-s2", "work-demo-1", "talk-1", "code-s3"],
                    clues: ["Xcode 在前台", "Claude Code 会话：统一时间线的行高", "你对她说过一次话"],
                    attention: b.attention(threads: [("t-tl", "时间线的行高", "work", "mac", 62, ["Xcode", "DayTimelineRows.swift"]), ("t-cc", "Claude Code", "work", "mac", 30, ["统一时间线的行高"]),
                                                     ("t-wx", "微信", "social", "iPhone", 6, []), ("t-yt", "YouTube", "entertainment", "mac", 4, [])],
                                           rows: [(575, 640, "t-tl", 56, 6, 3), (640, 685, "t-cc", 38, 4, 2)], alongside: ["t-wx"]))
            b.doing(685, 795, "开车去 A&W", "commute", "inferred", items: ["move-3", "move-4"], clues: ["先开车，再走了十分钟"])
            b.doing(795, 835, "吃午饭", "meal", "stated", items: ["stay-5", "payment-demo-1"])
            b.doing(835, 890, "歇一会儿", "rest", "inferred", confidence: "low", detail: "回到家，用了一会儿手机，听着歌", place: "p-home", items: ["move-6", "stay-7", "screen-demo-1", "messages-demo-1", "media-demo-1", "phone_call-demo-1"],
                    clues: ["手机上微信用得最多", "耳机里在放歌", "妈妈来了一个电话"], question: ("这段你在做什么？", ["休息", "刷手机", "等车"]),
                    attention: b.attention(threads: [("t-wx", "微信", "social", "iPhone", 14, ["Mia"]), ("t-xhs", "小红书", "social", "iPhone", 8, [])],
                                           rows: [(850, 868, "t-wx", 14, 2, 2), (876, 885, "t-xhs", 8, 1, 1)], alongside: []))
            b.doing(890, 919, "27", "commute", "observed", items: ["ride-8"])
            b.doing(919, 1000, "线性代数小测", "study", "inferred", place: "p-uvic", items: ["stay-9", "event-demo-2", "picture-pic-demo-1"], clues: ["日历里有小测", "你发来一张黑板的照片"])
            b.doing(1000, 1020, "FaceTime", "unknown", "observed", place: "p-uvic", items: ["stay-9", "call-demo-1"])
            b.doing(1020, 1055, "running", "exercise", "observed", place: "p-uvic", items: ["stay-9", "workout-demo-1"])
            b.doing(1055, 1095, "写作业", "study", "inferred", confidence: "low", place: "p-uvic", items: ["stay-9"], clues: ["还在学校"])
            b.doing(1095, 1130, "27", "commute", "observed", items: ["ride-12"])
            b.doing(1130, 1175, nil, "unknown", "observed", confidence: "low", place: "p-home", items: ["stay-13"])
            b.doing(1175, nil, "Jarvis", "work", "observed", place: "p-home",
                    items: ["stay-13", "code-s4", "work-demo-2", "payment-demo-2", "claude-demo-1", "talk-2", "code-s5", "messages-demo-2", "code-s6", "reminder-demo-2"], clues: ["Xcode 和终端在前台", "Claude Code 会话：论文第三章的数据"],
                    attention: b.attention(threads: [("t-test", "等你的卡片的测试", "work", "mac", 48, ["Xcode", "Ghostty"]), ("t-paper", "论文第三章", "study", "mac", 34, ["Pages", "第三章.pages"]),
                                                     ("t-claude", "Claude", "work", "mac", 18, ["积分换元的符号问题"]), ("t-wx", "微信", "social", "iPhone", 8, ["论文小组"]),
                                                     ("t-bili", "B 站", "entertainment", "mac", 6, [])],
                                           rows: [(1175, 1210, "t-test", 30, 4, 2), (1210, 1235, "t-claude", 18, 3, 1), (1235, 1270, "t-paper", 30, 4, 4), (1270, 1310, "t-test", 34, 5, 3)],
                                           alongside: ["t-wx"]))
        }
    } }

    /// the day before: a quieter one, with the night that began the evening before, a stretch of the afternoon nothing was recorded for, and a stay that is still going
    static func yesterday(_ cal: Cal) -> DayReply { build(cal, day: -1) { b in
        b.add("sleep", -40, 425, ["minutes": 440])
        b.add("stay", 425, 520, ["place": "12 Example St, Town", "kept_place": b.kept("p-home", "家", "home")])
        b.add("move", 520, 535, ["activity": "walking"])
        b.add("stay", 535, 980, ["place": "Engineering Lab Wing, Finnerty Rd", "kept_place": b.kept("p-uvic", "UVic", "school")])      // then nothing was recorded for an hour and ten minutes
        b.add("move", 1050, 1065, ["activity": "walking"])
        b.add("stay", 1065, nil, ["place": "12 Example St, Town", "kept_place": b.kept("p-home", "家", "home")])
        b.add("talk", 1325, 1330, ["id": "talk-y1", "lane": "her", "turns": 3])
        b.add("claude_day", 1430, 1440, ["id": "claude-day-demo", "source": "claude_daily", "text": "积分换元的符号问题;论文第三章结构;周末买菜清单", "chats": 3])
        b.extra["line"] = ["text": "安静的一天，大部分时间在学校。", "draft": false, "written_at": "2026-10-18T23:30:00+00:00"]
        b.extra["weather"] = ["condition": "clear", "high": 16, "low": 9, "sunrise_ms": b.ms(453), "sunset_ms": b.ms(1092)]
    } }

    /// two days back: errands on foot, by bike and by bus. Fifteen stays, four of them a few minutes at nowhere in particular between two ways (two of those with a label from the phone), eleven moves and two bus rides
    static func fragmented(_ cal: Cal) -> DayReply { build(cal, day: -2) { b in
        let home = b.kept("p-home", "家", "home"), uvic = b.kept("p-uvic", "UVic", "school")
        b.add("stay", 510, 580, ["place": "12 Example St, Town", "kept_place": home])
        b.add("move", 580, 592, ["activity": "walking"])
        b.add("stay", 592, 625, ["place": "Starbucks, Douglas St, Victoria"])
        b.add("move", 625, 631, ["activity": "walking"])
        b.add("stay", 631, 638, [:])
        b.add("move", 638, 646, ["activity": "walking"])
        b.add("stay", 646, 668, ["place": "Shoppers Drug Mart, Fort St, Victoria"])
        b.add("move", 668, 673, ["activity": "walking"])
        b.add("stay", 673, 681, ["place": "Yates St, Victoria"])
        b.add("ride", 681, 704, ["route": "14", "board_stop": "Yates & Douglas", "alight_stop": "UVic Exchange", "credit": "BC Transit", "ended_reason": "left_vehicle"])
        b.add("stay", 704, 738, ["place": "UVic Bookstore, Ring Rd, Victoria"])
        b.add("move", 738, 744, ["activity": "walking"])
        b.add("stay", 744, 830, ["place": "McPherson Library, Ring Rd, Victoria", "kept_place": uvic])
        b.add("stay", 830, 872, ["place": "Student Union Building, Ring Rd, Victoria"])
        b.add("move", 872, 880, ["activity": "walking"])
        b.add("stay", 880, 889, [:])
        b.add("move", 889, 897, ["activity": "walking"])
        b.add("stay", 897, 950, ["place": "Cadboro Bay Village, Sinclair Rd, Victoria"])
        b.add("move", 950, 972, ["activity": "cycling"])
        b.add("stay", 972, 981, ["place": "Beach Dr, Victoria"])
        b.add("move", 981, 1003, ["activity": "cycling"])
        b.add("stay", 1003, 1050, ["place": "Oak Bay Recreation Centre, Hampshire Rd, Victoria"])
        b.add("ride", 1050, 1078, ["route": "7", "board_stop": "Oak Bay Ave & Foul Bay", "alight_stop": "Hillside Centre", "ended_reason": "passed_stop"])
        b.add("stay", 1078, 1100, ["place": "Hillside Centre, Hillside Ave, Victoria"])
        b.add("move", 1100, 1118, ["activity": "walking"])
        b.add("stay", 1118, 1150, ["place": "Fairfield Plaza, Fairfield Rd, Victoria"])
        b.add("move", 1150, 1172, ["activity": "walking"])
        b.add("stay", 1172, nil, ["place": "12 Example St, Town", "kept_place": home])
        b.extra["line"] = ["text": "跑了一天的腿，书店、图书馆、海边都去了一趟。", "draft": false, "written_at": "2026-10-17T23:30:00+00:00"]
    } }

    /// three days back: only what he said to her, which the list has no rows for
    static func talkOnly(_ cal: Cal) -> DayReply { build(cal, day: -3) { b in
        b.add("talk", 495, 503, ["id": "talk-a", "lane": "her", "turns": 4])
        b.add("talk", 770, 790, ["id": "talk-b", "lane": "her", "turns": 12])
        b.add("talk", 1290, 1301, ["id": "talk-c", "lane": "her", "turns": 7])
    } }

    final class Builder {
        let cal: Cal, day: Int
        var items: [[String: Any]] = []
        var spans: [[String: Any]] = []
        var extra: [String: Any] = [:]
        init(_ cal: Cal, _ day: Int) { self.cal = cal; self.day = day }
        func ms(_ minute: Double) -> Int64 { Int64(cal.date(day).timeIntervalSince1970 * 1000 + minute * 60_000) }
        func kept(_ id: String, _ name: String, _ role: String) -> [String: Any] { ["id": id, "name": name, "role": role] }
        /// a night's stages: from minute, to minute, which
        func stages(_ list: [(Double, Double, String)]) -> [[String: Any]] { list.map { ["stage": $0.2, "start_ms": ms($0.0), "end_ms": ms($0.1)] } }
        /// a stretch of the brain's doing lane (ADR 0302, ADR 0307), as it serves one: `to` nil is the one that goes on; a stretch with no words and no category is `unknown`; `place` the
        /// place he keeps it was at; `question` what the model asks him of it, with its answers
        func doing(_ from: Double, _ to: Double?, _ title: String?, _ category: String, _ basis: String, confidence: String? = nil, detail: String? = nil, place: String? = nil,
                   items: [String] = [], clues: [String] = [], question: (String, [String])? = nil, attention: [String: Any]? = nil) {
            let words: Any = title.map { $0 as Any } ?? NSNull()
            spans.append(["id": "doing-\(Int(from))", "lane": "doing", "kind": title == nil && category == "unknown" ? "unknown" : "doing", "start_ms": ms(from), "end_ms": to.map { ms($0) as Any } ?? NSNull(),
                          "title": words, "detail": detail.map { $0 as Any } ?? NSNull(), "category": category, "basis": basis, "confidence": confidence ?? (basis == "inferred" ? "medium" : "high"),
                          "confirmed": basis == "stated", "clues": clues.map { ["kind": "words", "at_ms": ms(from), "text": $0] as [String: Any] },
                          "question": question.map { ["text": $0.0, "options": $0.1] as Any } ?? NSNull(), "attention": attention.map { $0 as Any } ?? NSNull(),
                          "place_id": place.map { $0 as Any } ?? NSNull(), "item_ids": items])
        }
        /// where his attention went in a stretch (ADR 0314), in minutes: the threads (id, name, category, where, minutes, what it was made of), the rows each led by one (from, to, the
        /// thread, its minutes, the others' minutes, how often he left it), and the ids of the threads on his phone alongside
        func attention(threads: [(String, String, String, String, Double, [String])], rows: [(Double, Double, String, Double, Double, Int)], alongside: [String]) -> [String: Any] {
            func thread(_ t: (String, String, String, String, Double, [String])) -> [String: Any] {
                ["id": t.0, "name": t.1, "category": t.2, "where": t.3, "ms": t.4 * 60_000, "keys": t.5.map { ["name": $0, "ms": t.4 * 30_000] as [String: Any] }]
            }
            return ["threads": threads.map(thread),
                    "rows": rows.map { ["start_ms": ms($0.0), "end_ms": ms($0.1), "thread": $0.2, "ms": $0.3 * 60_000, "others_ms": $0.4 * 60_000, "switches": $0.5] as [String: Any] },
                    "alongside": threads.filter { alongside.contains($0.0) }.map(thread)]
        }
        /// a span of the day; `to` nil: it goes on (a stay he is still in, a reminder that has not rung)
        func add(_ kind: String, _ from: Double, _ to: Double?, _ more: [String: Any] = [:]) {
            // the state a brain would have said at the fixed now
            let state = day != 0 ? (to == nil ? "now" : "done") : from > TimelineFixture.now ? "next" : (to == nil || to! > TimelineFixture.now) ? "now" : "done"
            var o: [String: Any] = ["id": "\(kind)-\(items.count)", "lane": "you", "kind": kind, "start_ms": ms(from), "state": state]
            o["end_ms"] = to.map { ms($0) } ?? NSNull()
            o.merge(more) { $1 }
            items.append(o)
        }
    }

    private static func build(_ cal: Cal, day: Int, _ fill: (Builder) -> Void) -> DayReply {
        let b = Builder(cal, day)
        fill(b)
        var reply: [String: Any] = ["date": cal.iso(day), "tz": cal.cal.timeZone.identifier, "start_ms": b.ms(0), "end_ms": b.ms(1440), "now_ms": b.ms(day == 0 ? now : 1440),
                                    "items": b.items, "missing": [String]()]
        if !b.spans.isEmpty { reply["spans"] = b.spans }
        reply.merge(b.extra) { $1 }
        let data = try! JSONSerialization.data(withJSONObject: reply)
        return try! JSONDecoder().decode(DayReply.self, from: data)
    }
}

extension TLBrain {
    /// A brain for the made-up day, which takes what he says of a commitment and of what he was doing, and keeps a place he names under the id `p-demo-new`, so that the screen can be seen
    /// answering without one; `fail`: it takes none of them.
    static func demo(fail: Bool = false) -> TLBrain {
        TLBrain(commit: { _, _ in fail ? .failed : .done },
        name: { name, _, _, role in fail ? .refused("这个名字或这个地方已经记过了") : .done(KeptPlace(id: "p-demo-new", name: name, role: role), change: "change-demo") },
        answer: { _, _, _, _ in try? await Task.sleep(nanoseconds: 600_000_000); return fail ? .failed : .done },
        choose: { _, _, _, _ in try? await Task.sleep(nanoseconds: 600_000_000); return fail ? .failed : .done },
        tidy: { try? await Task.sleep(nanoseconds: 600_000_000); return fail ? .failed : .unchanged },
        refetch: {})
    }
}

extension StarEngine {
    /// `-scene 足迹` opens the footprint's one-day screen on the made-up day (Sky/FootprintFixture.swift); the words after a colon go further: `:拖:1329` the bar held at that time (a fourth word after it lifts the finger again, and the map flies back to the day's whole), `:开:<outing id>`
    /// (or its number in the day, from 0) that outing's page opened.
    func runFootprintScene(_ name: String) {
        RingScene.on = true                     // the made-up day stays: the brain is not asked and the world is not swapped back
        switchWorld(real: true)
        let cal = Cal.cur, parts = name.split(separator: ":").map(String.init), mode = parts.count > 1 ? parts[1] : ""
        let day = FootprintFixture.day(cal.cal), reply = FootprintFixture.footprint(cal.cal)
        MainActor.assumeIsolated {
            ui.tl.fixture = .init(replies: [0: day], now: TimelineFixture.now)
            FootprintStore.shared.fixture = [cal.iso(ui.tl.today): reply]
            ui.footprintOpen = true
        }
        switch mode {
        case "拖":
            let n = Int(parts.count > 2 ? parts[2] : "") ?? 1329, t = Double(n / 100 * 60 + n % 100)
            let content = MainActor.assumeIsolated { FootprintDayContent.make(FootprintModel(reply: reply, day: day), past: false) }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) { [self] in
                ui.footprint.minute = t
                ui.footprint.list.hot = FootprintDayContent.hot(at: t, in: content.entries)
            }
            if parts.count > 3 { DispatchQueue.main.asyncAfter(deadline: .now() + 3.5) { [self] in ui.footprint.minute = nil; ui.footprint.list.hot = nil } }
        case "开":
            let want = parts.count > 2 ? parts[2] : "0"
            let pick = reply.outings.first { $0.id == want } ?? Int(want).flatMap { reply.outings.indices.contains($0) ? reply.outings[$0] : nil }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) { [self] in ui.footprint.openOuting = pick?.id }
        default: break
        }
        wake()
    }
}

extension StarEngine {
    /// `-scene 时间线` puts the timeline on the made-up day (now 21:50), whatever the setting was; the words after a colon go further:
    /// `:早上` the list at the morning, `:开:<id>` any row opened (`:开:ride-8`; a fourth word, `:开:stay-1:有对话` or `:有长对话`, with talk before it), `:改名` the place he keeps opened with the field for its new name, `:日出` the weather tapped (the sun's times in the header), `:她菜单` her cards out over the day, `:飘` her drift a moment later (she goes down behind her sentence and comes back; `:飘:1` to that one of her perches, and she stays a minute), `:理一下` 理一下 chosen with a brain that does not answer (在理… in her sentence's place, she thinks, and the light along the bar), `:碎片` the day with fifteen places, `:只有对话` the day of talks alone (none of them a row: the day says nothing was recorded), `:照片` the picture opened, `:补一段` a stretch opened with the sheet for what he was doing in it (a third word: the row's id; a last word `:败` and the brain will not take it), `:工作` the evening's Mac work opened (its span is bracketed on the bar), `:透镜:路上` (or 睡, 运动, Mac, 通话, 和她, 花, 照片) that chip pressed, `:拖:1015` a finger held on the bar at that time (a fourth word after it lifts the finger again), `:在干嘛` the bar with what he was doing on it and a finger held on the morning's work (`:在干嘛:1530` at that time), `:按在干嘛` the day as a row for each stretch of what he was doing (`:按在干嘛:早上` at the morning, `:按在干嘛:doing-575` a stretch opened, `:按在干嘛:doing-835` the one that asks him what it was), `:确认` the class his mail made, opened with 是我的 and 不是 (a third word: `:点是` with 是我的 tapped once it is open, `:点否` with 不是; a last word `:败` and the brain will not take it), `:命名` the stay no place he keeps is at, opened with the field for its first name and the kinds of place (a third word: `:点` with it named as 其他 once it is open, `:点学校` or `:点家` as that kind; a last word `:败` and the brain will not keep it), `:等你` a card waiting on him over the list (`:等你展开` with it opened, `:等你晚到` with it arriving
    /// a moment later), `:关于` the event row opened and a turn going out over it (its chip is the talk's first row; the panel opens a moment later), `:长答` an answer taller than her place for the talk (`:长答:开` with 全文 › tapped a moment later), `:语音` a round he speaks and she answers aloud (`:语音:文档` her answer has a document that says more), `:卡片` a card she puts up in the round, `:失败` a message that did not go out (`:失败:答` a turn she failed), `:空` a day with nothing recorded, `:打字` the keyboard up with words in the field (the day stays where it is; a keyboard the simulator does not show is made up), `:加载` the day still being asked for (the bar's empty track shimmers) until it arrives three seconds later, `:新增` a talk coming in a moment later, `:测帧` the frame times of the motion (Sky/StarTimelineProbe.swift; `:测帧:拖`, `:甩`, `:换日` or `:键盘` (the composer's lift moved a frame at a time, as a finger on the keyboard would) runs that kind alone, over and over), `:走时` the clock moving on a minute a moment later, `:昨天` the day before (`:昨天:1630` with a finger held on its bar), `:切换` the dial changing to the timeline a moment later (launch with `-homeModule stars`), `:切回` the timeline changing to the dial.
    func runTimelineScene(_ name: String) {
        RingScene.on = true                     // the made-up day stays: the brain is not asked and the world is not swapped back
        switchWorld(real: true)
        let cal = Cal.cur, parts = name.split(separator: ":").map(String.init), mode = parts.count > 1 ? parts[1] : ""
        let today = TimelineFixture.today(cal, mail: mode == "确认", doing: mode == "在干嘛" || mode == "按在干嘛"), yesterday = TimelineFixture.yesterday(cal), fragmented = TimelineFixture.fragmented(cal), talkOnly = TimelineFixture.talkOnly(cal)
        MainActor.assumeIsolated {
            ui.tl.fixture = .init(replies: [0: today, -1: yesterday, -2: fragmented, -3: talkOnly], now: TimelineFixture.now)
            ui.tl.brain = .demo(fail: parts.last == "败")          // what he does to the made-up day goes to no brain
            TalkTopics.shared.debugSet("talk-1", "明天下午有什么安排？", "下午三点有一个二面，在线上，大概四十五分钟。")      // the other talk has no topic yet
        }
        // the picture he sent, kept on this phone under its id, so nothing is asked of the brain
        Task { await PictureStore.shared.keep(id: "pic-demo-1", data: Self.demoPicture()) }
        if mode.hasPrefix("等你") {
            let json = #"{"clarification": {"id": "Qdebug01", "question": "哪一天，几位？", "fields": [{"label": "哪一天", "choices": ["周五", "周六"]}]}}"#
            let put = { MainActor.assumeIsolated { Waiting.shared.debugSet(WaitingCards.parse(Data(json.utf8)) ?? []) } }
            if mode == "等你晚到" { DispatchQueue.main.asyncAfter(deadline: .now() + 1.5, execute: put) } else { put() }
            if mode == "等你展开" { ui.tl.openCard = "clarification-Qdebug01" }
        }
        let model = DayTimelineModel.build(today, now: TimelineFixture.now)
        // a row brought into view, or opened where it stands as a tap opens it
        func show(_ id: String, open: Bool) {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) { [self] in
                if open, let e = model.entries.first(where: { $0.id == id }) { ui.tl.unfold(e, in: model, room: TLLayout.viewport); return }
                if let y = model.top(of: id) { ui.tl.scroll(to: max(0, y - 90), animated: false) }
            }
        }
        switch mode {
        case "早上": DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) { [self] in
            if let e = model.entry(at: 9 * 60 + 30), let y = model.top(of: e.id) { ui.tl.scroll(to: y, animated: false) } }
        case "开":                                                                 // any row, by its id: `:开:ride-8` (a fourth word: with a round of talk before it)
            if parts.count > 3 { earlierRound(cal, long: parts[3] == "有长对话") }
            show(parts.count > 2 ? parts[2] : "", open: true)
        case "改名":                                                               // the place he keeps, opened with the field for its new name
            show("stay-1", open: false)
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) { [self] in if let e = model.entries.first(where: { $0.id == "stay-1" }) { ui.tl.beginRename(e) } }
        case "理一下": DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) { [self] in          // 理一下 chosen, and the brain taking its time: 在理… in her sentence's place, and the light going along the bar
            ui.tl.brain.tidy = { try? await Task.sleep(nanoseconds: 60_000_000_000); return .failed }
            ui.tl.tidyDay()
        }
        case "她菜单": DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) { [self] in ui.tl.openArc(holding: false) }          // her cards out (`-scene 时间线:她菜单`)
        case "聊天":                                                               // the conversation's page of the bar at the foot, with a day and a half of talk on it (`:聊天:空` none)
            if parts.count < 3 || parts[2] != "空" { talkPageRounds(cal) }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) { [self] in showPage(.talk) }
        case "飘":          // her drift, without the wait for it
            if let k = Int(parts.count > 2 ? parts[2] : ""), TLSeat.perches.indices.contains(k) {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) { [self] in wander = (TLSeat.perches[k], nowMs() + 60_000) }
            } else { seatWait = 1500 }
        case "日出": DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [self] in ui.tl.saySun(for: 120) }          // the weather tapped, and the sun's times left on show
        case "碎片": DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) { [self] in ui.tl.setDay(ui.tl.today - 2) }
        case "只有对话": DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) { [self] in ui.tl.setDay(ui.tl.today - 3) }
        case "空": DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) { [self] in ui.tl.setDay(ui.tl.today - 4) }
        case "关于":
            show("event-demo-1", open: true)
            earlierRound(cal)           // so the new round is the second, as it is for him
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.8) { [self] in
                captureAbout()              // what a typed turn does as it goes out: the open row is the chip
                beginTalk(); lingerHold = true
                addTurn(his: true, "把它推到明天")
                addTurn(his: false, "好，我把陶艺课推到明天晚上十点。")
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 3.6) { [self] in openPanel(quiet: true) }
        case "长答":                                                               // a typed answer taller than her place for the talk: the last line fades and 全文 › stands in the corner (`:长答:开` taps it a moment later)
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [self] in
                beginTalk()
                addTurn(his: true, "这周还剩下哪些安排，帮我列出来")
                showStatus()
                talk = "speak"
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) { [self] in
                    startSay("这周剩下的安排：\n\n- 周三 10:00 线性代数小测，在 ECS 123，带计算器\n- 周四 09:30 和 Mia 通电话，聊毕业旅行的事\n- 周五 14:00 作品集评审，首页还差最后两张图\n- 周六 09:00 爬山，我会提前看天气，有雨就改到周日\n- 周日 21:00 整理这一周的记忆\n\n最要紧的是周五的评审，别的都可以往后放。要不要我把评审前一晚的时间空出来，帮你把图做完？")
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { [self] in endTalk() }
                    if parts.count > 2, parts[2] == "开" { DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) { [self] in showAnswer(TLTalk.of(ui.liveTurns).answer) } }
                }
            }
        case "语音": runSpokenRound(cal, document: parts.count > 2 && parts[2] == "文档")
        case "打字":
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [self] in
                beginEditing?()
                ui.text = "明天下午三点提醒我给 Mia 回电话"
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 2.4) { [self] in
                if ui.kbLift == 0 { withAnimation(.easeOut(duration: 0.3)) { ui.kbLift = StarUI.lift(keyboardHeight: 336) } }
            }
        case "卡片":                                                               // a typed round in which she puts a card up while she answers: her words, and under them the one amber line (the card itself is in the list's place for what waits)
            let json = #"{"clarification": {"id": "Qdebug01", "question": "哪一天，几位？", "fields": [{"label": "哪一天", "choices": ["周五", "周六"]}]}}"#
            let cards = WaitingCards.parse(Data(json.utf8)) ?? []
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [self] in
                beginTalk()
                addTurn(his: true, "帮我订周五晚上的位子")
                showStatus()
                talk = "speak"
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) { [self] in
                    startSay("好，订位之前我要先问你一件事，卡片在下面。")
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [self] in
                        MainActor.assumeIsolated { Waiting.shared.debugSet(cards) }
                        if let c = cards.first { phoneEvent(.card(c)) }
                    }
                    DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) { [self] in endTalk() }
                }
            }
        case "失败":                                                               // a message that did not go out (`:失败:答` a turn she failed): the line says so, with 点重试 (重试)
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [self] in
                beginTalk()
                talk = "listen"
                let his = addTurn(his: true, "帮我订周五晚上的位子")
                markSend(his, .sending)
                showStatus()
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.4) { [self] in
                    if parts.count > 2, parts[2] == "答" {
                        showFailed(); markSend(his, .sent)
                        endTalk(); lingerUntil = nowMs() + Self.lingerFailed; tlUntil = lingerUntil          // as the brain's own failed turn does
                    } else {
                        unsent[his] = SentTurn(words: "帮我订周五晚上的位子", shown: "帮我订周五晚上的位子", ids: [], pics: [], about: nil, his: his, utterance: "scene", taken: false)
                        markSend(his, .failed); dropStatus()
                        talk = nil; lingerFrom(nowMs()); trouble("连不上 Jarvis")
                    }
                }
            }
        case "确认":                                                               // the event his mail made, opened with 是我的 and 不是; `:点是` says it is his, `:点否` that it is not (`:败`: the brain will not)
            let id = TimelineFixture.commitmentId(cal), how = parts.count > 2 ? parts[2] : ""
            show(id, open: true)
            if how.hasPrefix("点") {
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.6) { [self] in
                    guard let r = ui.tl.reply, let e = ui.tl.model(r).entries.first(where: { $0.id == id }) else { return }
                    if how.hasPrefix("点是") { ui.tl.confirm(e) } else { ui.tl.deny(e) }
                }
            }
        case "命名":                                                               // the stay no place he keeps is at, opened with the field for its first name and the kinds of place; `:点` names it (`:败`: the brain will not)
            guard let stay = model.entries.first(where: { $0.place?.canName == true }) else { break }
            show(stay.id, open: false)
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) { [self] in ui.tl.beginRename(stay) }
            if parts.count > 2, parts[2].hasPrefix("点") {
                DispatchQueue.main.asyncAfter(deadline: .now() + 2.2) { [self] in
                    ui.tl.nameRole = parts[2].hasSuffix("家") ? .home : parts[2].hasSuffix("学校") ? .school : .other
                    ui.tl.rename(stay, to: "A&W 汉堡")
                }
            }
        case "补一段":                                                             // a stretch opened and the sheet for what he was doing in it (`:补一段:<id>` any stretch, the evening's home by default; `:败`: the brain will not take it)
            let id = parts.count > 2 && parts[2] != "败" ? parts[2] : "stay-1"
            show(id, open: true)
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.4) { [self] in if let e = model.entries.first(where: { $0.id == id }) { ui.tl.beginSayWhat(e) } }
        case "照片": show("picture-pic-demo-1", open: true)
        case "工作": show("work-demo-2", open: true)
        case "透镜":
            let lens = [("睡", TLLens.sleep), ("路上", .trip), ("运动", .workout), ("Mac", .work), ("通话", .call), ("和她", .talk), ("花", .payment), ("照片", .picture)].first { $0.0 == (parts.count > 2 ? parts[2] : "") }?.1
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) { [self] in withAnimation(.easeOut(duration: 0.15)) { ui.tl.lens = lens } }
        case "按在干嘛":                                                           // the day as a row for each stretch of what he was doing; `:按在干嘛:<id>` one opened (`doing-575` the long morning's work, `doing-835` the rest with his phone), `:按在干嘛:早上` the list at the morning
            let id = parts.count > 2 ? parts[2] : ""
            if id == "早上" { DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) { [self] in if let e = model.entry(at: 9 * 60 + 5), let y = model.top(of: e.id) { ui.tl.scroll(to: max(0, y - 12), animated: false) } } }
            else if !id.isEmpty { show(id, open: true) }
        case "拖", "在干嘛":
            let n = Int(parts.count > 2 ? parts[2] : "") ?? (mode == "拖" ? 0 : 1030), t = model.onBar(Double(n / 100 * 60 + n % 100))
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) { [self] in
                ui.tl.follow(t, in: model, inPlace: true)
                ui.tl.finger = TLGaze.bar(x: TLAxis.fair(model.scene, width: TLLayout.barW).x(t))
            }
            if parts.count > 3 { DispatchQueue.main.asyncAfter(deadline: .now() + 2.4) { [self] in withAnimation(.easeOut(duration: 0.2)) { ui.tl.follow(nil, in: nil) } } }
        case "昨天":
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) { [self] in ui.tl.setDay(ui.tl.today - 1) }
            if parts.count > 2 {                // `:昨天:1630` with a finger held on the bar at that time, once the day is there
                let n = Int(parts[2]) ?? 0, past = DayTimelineModel.build(yesterday, now: nil)
                DispatchQueue.main.asyncAfter(deadline: .now() + 2.4) { [self] in ui.tl.follow(Double(n / 100 * 60 + n % 100), in: past, inPlace: true) }
            }
        case "加载":                                                               // the day still being asked for: the bar's empty track shimmers, and the day arrives after three seconds
            ui.tl.fixture?.loading = true
            DispatchQueue.main.asyncAfter(deadline: .now() + 3) { [self] in ui.tl.fixture?.loading = false }
        case "新增": DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [self] in ui.tl.fixture?.replies[0] = TimelineFixture.today(cal, late: true) }
        case "测帧": runProbe(today, model, cal, only: parts.count > 2 ? parts[2] : "")
        case "走时": DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [self] in ui.tl.fixture?.now += 1 }
        case "切换": DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { HomeModule.shared.kind = .timeline }
        case "切回": DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { HomeModule.shared.kind = .stars }
        default: break
        }
        wake()
    }

    /// The motion run the way a hand does it, and the frames it takes: the launch, a scrub across the bar and back, a fling of the list down and up, a day change each way, a talk coming in, a row
    /// opened and shut. Each reports its median and 95th percentile frame time and its worst frame. A word after `:测帧:` runs one kind alone, over and over (`拖`, `甩`, `换日`, `键盘` the keyboard's height followed a frame at a time), so that something
    /// can be watched at work while it does.
    private func runProbe(_ reply: DayReply, _ model: DayTimelineModel, _ cal: Cal, only: String) {
        let ui = self.ui, tl = ui.tl, lo = model.span.lowerBound, hi = model.span.upperBound
        let ymax = max(0, (model.offsets.last ?? 0) + (model.entries.last?.height ?? 0) - 400)
        let probe = FrameProbe()
        let t0 = CACurrentMediaTime()
        for _ in 0..<20 { _ = tl.model(reply) }
        probe.note(String(format: "building the day's model: %.2f ms (mean of 20)", (CACurrentMediaTime() - t0) * 50))
        func pause(_ s: Double, start: @escaping () -> Void = {}) -> FrameProbe.Step { .init(label: nil, seconds: s, start: start) }
        let fling: (Double) -> Double = { (1 - exp(-4 * $0)) / (1 - exp(-4)) }          // a fast fling that slows as it goes
        let launch = [FrameProbe.Step(label: "launch: the bar grows and the rows rise", seconds: 1.3), pause(0.8)]
        let idle = [FrameProbe.Step(label: "idle: nothing moves", seconds: 1.5)]
        let there: (Double) -> Double = { p in p < 0.5 ? 2 * p : 2 - 2 * p }          // across and back
        let bare = [FrameProbe.Step(label: "the finger alone: the bar's hairline and the readout, no list", seconds: 2, frame: { p in
                        tl.scrub = lo + there(p) * (hi - lo); tl.finger = TLGaze.bar(x: there(p) * TLLayout.barW) }),
                    pause(0.6, start: { tl.scrub = nil; tl.finger = nil })]
        let scrub = [FrameProbe.Step(label: "scrub across the bar and back (2 s)", seconds: 2, frame: { p in
                         tl.follow(lo + there(p) * (hi - lo), in: model, inPlace: true); tl.finger = TLGaze.bar(x: there(p) * TLLayout.barW) }),
                     pause(0.6, start: { withAnimation(.easeOut(duration: 0.2)) { tl.follow(nil, in: nil) } })]
        let flings = [FrameProbe.Step(label: "list fling down (1 s)", seconds: 1, frame: { p in tl.scroll(to: ymax * fling(p), animated: false) }), pause(0.4),
                      FrameProbe.Step(label: "list fling up (1 s)", seconds: 1, frame: { p in tl.scroll(to: ymax * (1 - fling(p)), animated: false) }), pause(0.8)]
        let days = [FrameProbe.Step(label: "day change to yesterday", seconds: 0.9, start: { tl.setDay(tl.today - 1) }), pause(0.5),
                    FrameProbe.Step(label: "day change back to today", seconds: 0.9, start: { tl.setDay(tl.today) }), pause(0.8)]
        let rest = [FrameProbe.Step(label: "a talk comes in", seconds: 0.9, start: { tl.fixture?.replies[0] = TimelineFixture.today(cal, late: true) }), pause(0.5),
                    FrameProbe.Step(label: "a row opens where it stands", seconds: 0.7, start: {
                        if let e = model.entries.first(where: { $0.id == "event-demo-1" }) { tl.unfold(e, in: model, room: TLLayout.viewport) } }),
                    FrameProbe.Step(label: "a row shuts", seconds: 0.7, start: { tl.close() })]
        let keys = [pause(0.4), FrameProbe.Step(label: "keyboard height follows a finger (0.5 s)", seconds: 0.5, frame: { p in ui.kbLift = StarUI.lift(keyboardHeight: 336) * p }), pause(0.3, start: { ui.kbLift = 0 })]
        let alone: [String: [FrameProbe.Step]] = ["拖": scrub, "甩": flings, "换日": days, "静": idle, "指": bare, "键盘": keys]
        let steps = alone[only].map { launch + Array(repeating: $0, count: 6).flatMap { $0 } } ?? launch + idle + bare + scrub + flings + days + rest
        probe.run(steps) { _ in }
    }

    /// A round he speaks and she answers aloud, as the voice session lives it, with made-up states: his words grow as the recognizer hands them over, she thinks, her words come at the pace of her
    /// voice, and the microphone stays open for what he says next. With `document`, the conversation's row of her answer has a written part that says more than she said.
    private func runSpokenRound(_ cal: Cal, document: Bool) {
        earlierRound(cal)
        let t0 = nowMs(), ask = "明天下午三点有什么安排，顺便提醒我晚上给 Mia 回电话"
        let said = "明天下午三点有一个二面，在线上，大概四十五分钟。晚上八点半给 Mia 回电话，我到时候提醒你。"
        let written = "## 明天\n- 15:00 和 Lumen Labs 的二面，线上，约四十五分钟\n- 20:30 给 Mia 回电话，我到时候提醒你\n\n二面之前我会把你上次面试的笔记整理出来，放在日程里。"
        let shown = document ? written : said
        let speechEnd = 3.8 + Double(said.count) / 8 + 0.3
        voiceFake = { now in
            var v = VoiceShot()
            let s = (now - t0) / 1000
            if s < 2.6 { v.state = .listening; v.partial = String(ask.prefix(Int(s * 12) + 1)); v.input = 0.3 + 0.7 * voiceLevel(now / 1000 * 1.3) }
            else if s < 3.8 { v.state = .thinking; v.transcript = ask }
            else if s < speechEnd { v.state = .speaking; v.transcript = ask; v.reply = String(shown.prefix(Int(Double(shown.count) * min(1, (s - 3.8) / (Double(said.count) / 8))))); v.full = shown; v.output = voiceLevel(now / 1000) }
            else if s < speechEnd + 10 { v.state = .listening; v.reply = shown }          // the window for what he says next is ten seconds; her answer is over, and the session has cleared it
            return v
        }
        // the conversation's rows of this round, which her row takes when the session is over (`adoptWritten`): the voice part alone, or with a document
        let at = Date().addingTimeInterval(-60), stamp = TalkStream.dayId(at, cal.cal) + "T" + TalkStream.clock(at, cal.cal) + ":00"
        let raw = document ? "<voice>\(said)</voice><document>\(written)</document>" : "<voice>\(said)</voice>"
        DispatchQueue.main.asyncAfter(deadline: .now() + 6) { [self] in
            let base = MainActor.assumeIsolated { Conversation.shared.rows }
            MainActor.assumeIsolated {
                Conversation.shared.debugSet(base + [ConversationRow(seq: 33, source: "allen", ts: stamp, text: ask), ConversationRow(seq: 34, source: "jarvis", ts: stamp, text: raw)])
            }
        }
    }

    /// the conversation as it stood three hours ago: one question and its answer; `long`, thirty rows before them, so the panel is full
    private func earlierRound(_ cal: Cal, long: Bool = false) {
        let at = Date().addingTimeInterval(-3 * 3600), stamp = TalkStream.dayId(at, cal.cal) + "T" + TalkStream.clock(at, cal.cal) + ":00"
        let before: [ConversationRow] = long ? (1...30).map { n -> ConversationRow in
            let c = at.addingTimeInterval(-Double(31 - n) * 20 * 60)
            return ConversationRow(seq: n, source: n % 2 == 0 ? "allen" : "jarvis", ts: TalkStream.dayId(c, cal.cal) + "T" + TalkStream.clock(c, cal.cal) + ":00",
                                   text: n % 2 == 0 ? "第 \(n) 行，他说的话" : "<voice>第 \(n) 行，她的回答。</voice>")
        } : []
        MainActor.assumeIsolated {
            Conversation.shared.debugSet(before + [ConversationRow(seq: 31, source: "allen", ts: stamp, text: "明天下午有什么安排？"),
                                                  ConversationRow(seq: 32, source: "jarvis", ts: stamp, text: "<voice>下午三点有一个二面，在线上。</voice>")])
        }
    }

    /// A day and a half of talk as the conversation's page shows it: yesterday evening, this morning, lunch with a written answer, and a moment ago.
    private func talkPageRounds(_ cal: Cal) {
        let now = Date()
        func row(_ seq: Int, _ his: Bool, _ ago: Double, _ text: String) -> ConversationRow {
            let at = now.addingTimeInterval(-ago * 60)
            return ConversationRow(seq: seq, source: his ? "allen" : "jarvis", ts: TalkStream.dayId(at, cal.cal) + "T" + TalkStream.clock(at, cal.cal) + ":00", text: text)
        }
        // an answer as the brain writes one: said, and written out (a voice part alone is a line before the answer, with more to come)
        func said(_ s: String) -> String { "<voice>\(s)</voice><document>\(s)</document>" }
        MainActor.assumeIsolated {
            Conversation.shared.debugSet([
                row(1, true, 26 * 60, "明天早上提醒我九点出门"),
                row(2, false, 26 * 60, said("好，明早八点五十提醒你，路上大概二十分钟。")),
                row(3, true, 4 * 60, "今天要带什么"),
                row(4, false, 4 * 60, said("带上计算器和学生卡。线性代数小测在 ECS 123，十点开始。")),
                row(5, true, 2 * 60, "附近有什么吃的"),
                row(6, false, 2 * 60, "<voice>走路五分钟内有三家，我列给你。</voice><document>走路五分钟内有三家：\n\n- A&W，汉堡套餐 12 块左右\n- Subway，人少\n- 学校食堂，今天有咖喱饭</document>"),
                row(7, true, 10, "明天下午有什么安排？"),
                row(8, false, 10, said("下午三点有一个二面，在线上。两点半我提醒你准备。")),
            ])
        }
    }

    /// a made-up picture: a blackboard-green gradient with a few lines of chalk
    static func demoPicture() -> Data {
        let size = CGSize(width: 900, height: 600)
        let img = UIGraphicsImageRenderer(size: size, format: { let f = UIGraphicsImageRendererFormat(); f.scale = 1; return f }()).image { c in
            let cg = c.cgContext
            let space = CGColorSpaceCreateDeviceRGB()
            let grad = CGGradient(colorsSpace: space, colors: [UIColor(red: 0.10, green: 0.22, blue: 0.20, alpha: 1).cgColor, UIColor(red: 0.04, green: 0.12, blue: 0.14, alpha: 1).cgColor] as CFArray, locations: [0, 1])!
            cg.drawLinearGradient(grad, start: .zero, end: CGPoint(x: size.width, y: size.height), options: [])
            UIColor(white: 1, alpha: 0.8).setStroke()
            cg.setLineWidth(5); cg.setLineCap(.round)
            for (i, w) in [620.0, 540, 700, 420].enumerated() {
                let y = 140 + Double(i) * 110
                cg.move(to: CGPoint(x: 90, y: y)); cg.addLine(to: CGPoint(x: 90 + w, y: y + Double((i % 2) * 8)))
            }
            cg.strokePath()
        }
        return img.jpegData(compressionQuality: 0.8) ?? Data()
    }
}
#endif
