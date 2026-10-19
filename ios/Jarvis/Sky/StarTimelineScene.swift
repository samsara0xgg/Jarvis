#if DEBUG
import UIKit
import SwiftUI

/// A made-up day for the timeline, as if the brain had served it: the same day every launch, with now fixed at 21:50. Made-up data only.
enum TimelineFixture {
    static let now = 21 * 60 + 50.0

    /// the id of the event his mail made in `today(mail: true)`, as the brain names a commitment: `commitment-<hex>@<day>`
    static func commitmentId(_ cal: Cal) -> String { "commitment-3f9a1c2e7b4d4a0c9e5f6a7b8c9d0e1f@" + cal.iso(0) }

    /// today: the night, home, a drive, a bite, home again, the bus to school, school, the bus home, and the evening still going, with his Mac work, what he spent and a chat with Claude along the way (`late`: a talk that has just ended is there too; `mail`: the class his mail made is a commitment of the brain's, by its id)
    static func today(_ cal: Cal, late: Bool = false, mail: Bool = false) -> DayReply { build(cal, day: 0) { b in
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
        b.extra["highlights"] = [["id": "call-demo-1", "label": "和 Mia 聊了很久", "by": "her"]]
        b.extra["missing"] = ["calendar"]
        b.extra["weather"] = ["condition": "mostly cloudy", "high": 14, "low": 8, "sunrise_ms": b.ms(454), "sunset_ms": b.ms(1091)]
        b.extra["summary"] = ["steps": 8240, "resting_hr": 58, "hrv": 42]
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

    /// three days back: only what he said to her
    static func talkOnly(_ cal: Cal) -> DayReply { build(cal, day: -3) { b in
        b.add("talk", 495, 503, ["id": "talk-a", "lane": "her", "turns": 4])
        b.add("talk", 770, 790, ["id": "talk-b", "lane": "her", "turns": 12])
        b.add("talk", 1290, 1301, ["id": "talk-c", "lane": "her", "turns": 7])
    } }

    final class Builder {
        let cal: Cal, day: Int
        var items: [[String: Any]] = []
        var extra: [String: Any] = [:]
        init(_ cal: Cal, _ day: Int) { self.cal = cal; self.day = day }
        func ms(_ minute: Double) -> Int64 { Int64(cal.date(day).timeIntervalSince1970 * 1000 + minute * 60_000) }
        func kept(_ id: String, _ name: String, _ role: String) -> [String: Any] { ["id": id, "name": name, "role": role] }
        /// a night's stages: from minute, to minute, which
        func stages(_ list: [(Double, Double, String)]) -> [[String: Any]] { list.map { ["stage": $0.2, "start_ms": ms($0.0), "end_ms": ms($0.1)] } }
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
        reply.merge(b.extra) { $1 }
        let data = try! JSONSerialization.data(withJSONObject: reply)
        return try! JSONDecoder().decode(DayReply.self, from: data)
    }
}

extension TLBrain {
    /// A brain for the made-up day, which keeps his marks the way the real one does (at most five of his, hers in the slots he leaves, a sixth of his refused), takes what he says of a
    /// commitment and keeps a place he names under the id `p-demo-new`, so that the screen can be seen answering without one; `fail`: it takes neither of the last two.
    static func demo(marks start: [Highlight], fail: Bool = false) -> TLBrain {
        final class Kept { var list: [Highlight]; init(_ list: [Highlight]) { self.list = list } }
        let kept = Kept(start)
        return TLBrain(mark: { _, id, on in
            if on, !kept.list.contains(where: { $0.id == id && $0.by != "her" }), kept.list.filter({ $0.by != "her" }).count >= TLMarks.cap { return .full }
            kept.list = TLMarks.toggled(kept.list, id: id, on: on)
            return .done(kept.list)
        }, commit: { _, _ in fail ? .failed : .done },
        name: { name, _, _, role in fail ? .refused("这个名字或这个地方已经记过了") : .done(KeptPlace(id: "p-demo-new", name: name, role: role), change: "change-demo") },
        refetch: {})
    }
}

extension StarEngine {
    /// `-scene 时间线` puts the timeline on the made-up day (now 21:50), whatever the setting was; the words after a colon go further:
    /// `:早上` the list at the morning, `:开:<id>` any row opened (`:开:ride-8`; a fourth word, `:开:stay-1:有对话` or `:有长对话`, with talk before it), `:改名` the place he keeps opened with the field for its new name, `:日出` the weather tapped (the sun's times in the header), `:碎片` the day with fifteen places, `:只有对话` the day of talks alone, `:对话` a talk opened, `:照片` the picture opened, `:工作` the evening's Mac work opened (its span is bracketed on the bar), `:透镜:路上` (or 睡, 运动, Mac, 通话, 和她, 花, 照片) that chip pressed, `:拖:1015` a finger held on the bar at that time (a fourth word after it lifts the finger again), `:星:talk-1` that row opened (a fourth word: `:点` with its star tapped once it is open, `:满` with five of his marks standing and the star of a sixth tapped, `:二` with his mark on one row and hers on the one opened), `:确认` the class his mail made, opened with 是我的 and 不是 (a third word: `:点是` with 是我的 tapped once it is open, `:点否` with 不是; a last word `:败` and the brain will not take it), `:命名` the stay no place he keeps is at, opened with the field for its first name and the kinds of place (a third word: `:点` with it named as 其他 once it is open, `:点学校` or `:点家` as that kind; a last word `:败` and the brain will not keep it), `:等你` a card waiting on him over the list (`:等你展开` with it opened, `:等你晚到` with it arriving
    /// a moment later), `:关于` the event row opened and a turn going out over it (its chip is the talk's first row; the panel opens a moment later), `:总结` 总结 sent over the evening's Mac work (`:总结:列表` her answer has a list), `:长答` an answer taller than her place for the talk (`:长答:开` with 全文 › tapped a moment later), `:语音` a round he speaks and she answers aloud (`:语音:文档` her answer has a document that says more), `:卡片` a card she puts up in the round, `:失败` a message that did not go out (`:失败:答` a turn she failed), `:加载` the day still being asked for (the bar's empty track shimmers) until it arrives three seconds later, `:新增` a talk coming in a moment later, `:测帧` the frame times of the motion (Sky/StarTimelineProbe.swift; `:测帧:拖`, `:甩` or `:换日` runs that kind alone, over and over), `:走时` the clock moving on a minute a moment later, `:昨天` the day before (`:昨天:1630` with a finger held on its bar), `:切换` the dial changing to the timeline a moment later (launch with `-homeModule stars`), `:切回` the timeline changing to the dial.
    func runTimelineScene(_ name: String) {
        RingScene.on = true                     // the made-up day stays: the brain is not asked and the world is not swapped back
        switchWorld(real: true)
        let cal = Cal.cur, parts = name.split(separator: ":").map(String.init), mode = parts.count > 1 ? parts[1] : ""
        let today = TimelineFixture.today(cal, mail: mode == "确认"), yesterday = TimelineFixture.yesterday(cal), fragmented = TimelineFixture.fragmented(cal), talkOnly = TimelineFixture.talkOnly(cal)
        MainActor.assumeIsolated {
            ui.tl.fixture = .init(replies: [0: today, -1: yesterday, -2: fragmented, -3: talkOnly], now: TimelineFixture.now)
            ui.tl.brain = .demo(marks: today.highlights, fail: parts.last == "败")          // what he does to the made-up day goes to no brain
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
        func show(_ id: String, open: Bool) {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) { [self] in
                if open { ui.tl.openRow = id }
                if let y = model.top(of: id) { ui.tl.scroll(to: max(0, y - 90), animated: false) }
            }
        }
        switch mode {
        case "早上": DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) { [self] in
            if let e = model.entry(at: 9 * 60 + 30), let y = model.top(of: e.id) { ui.tl.scroll(to: y, animated: false) } }
        case "对话": show("talk-1", open: true)
        case "开":                                                                 // any row, by its id: `:开:ride-8` (a fourth word: with a round of talk before it)
            if parts.count > 3 { earlierRound(cal, long: parts[3] == "有长对话") }
            show(parts.count > 2 ? parts[2] : "", open: true)
        case "改名":                                                               // the place he keeps, opened with the field for its new name
            show("stay-1", open: false)
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) { [self] in if let e = model.entries.first(where: { $0.id == "stay-1" }) { ui.tl.beginRename(e) } }
        case "日出": DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [self] in ui.tl.saySun(for: 120) }          // the weather tapped, and the sun's times left on show
        case "碎片": DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) { [self] in ui.tl.setDay(ui.tl.today - 2) }
        case "只有对话": DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) { [self] in ui.tl.setDay(ui.tl.today - 3) }
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
        case "总结":                                                               // 总结 on the evening's Mac work, her answer as a typed turn brings it (`:总结:列表` one with a list), and the talk left to end by itself
            show("work-demo-2", open: true)
            earlierRound(cal, long: true)          // a day of talk before it: the stream has rows to scroll
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.8) { [self] in
                captureAbout()
                beginTalk()
                addTurn(his: true, TimelineState.summarizeWords)
                showStatus()                        // her typing line until her words come, as a turn on its way has it
                talk = "speak"
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.4) { [self] in
                    startSay(parts.count > 2 ? "这段时间你在 Mac 上主要做了三件事：\n\n- 改时间线的悬停，修好了外接屏上一闪就没的问题\n- 跑了五百多项检查，全过了\n- 看了一会儿星盘的代码"
                                             : "这段时间你在 Mac 上主要在改时间线：先修好了外接屏上悬停一闪就没的问题，又跑了五百多项检查，全过了。中间看了一会儿星盘的代码，大概二十分钟。后面一直在终端里，和 Claude Code 来回改了几轮。")
                    DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [self] in endTalk() }
                }
            }
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
                        endTalk(); lingerUntil = nowMs() + Self.lingerFailed
                    } else {
                        unsent[his] = SentTurn(words: "帮我订周五晚上的位子", shown: "帮我订周五晚上的位子", ids: [], pics: [], about: nil, his: his, utterance: "scene", taken: false)
                        markSend(his, .failed); dropStatus()
                        talk = nil; lingerFrom(nowMs()); trouble("连不上 Jarvis")
                    }
                }
            }
        case "星":
            let id = parts.count > 2 ? parts[2] : "", how = parts.count > 3 ? parts[3] : ""
            var marks = today.highlights
            if how == "满" { marks = ["sleep-0", "stay-1", "work-demo-1", "payment-demo-1", "claude-demo-1"].map { Highlight(id: $0, label: "", by: "him") } }
            if how == "二" { marks = [Highlight(id: "talk-2", label: "", by: "him")] + today.highlights }
            ui.tl.highlights[today.date] = TLHeld(value: marks, until: .distantFuture)
            ui.tl.brain = .demo(marks: marks)
            show(id, open: true)
            if how == "点" || how == "满" {
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.6) { [self] in
                    if let r = ui.tl.reply, let e = ui.tl.model(r).entries.first(where: { $0.id == id }) { ui.tl.toggleStar(e) }
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
        case "照片": show("picture-pic-demo-1", open: true)
        case "工作": show("work-demo-2", open: true)
        case "透镜":
            let lens = [("睡", TLLens.sleep), ("路上", .trip), ("运动", .workout), ("Mac", .work), ("通话", .call), ("和她", .talk), ("花", .payment), ("照片", .picture)].first { $0.0 == (parts.count > 2 ? parts[2] : "") }?.1
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) { [self] in withAnimation(.easeOut(duration: 0.15)) { ui.tl.lens = lens } }
        case "拖":
            let n = Int(parts.count > 2 ? parts[2] : "") ?? 0, t = model.onBar(Double(n / 100 * 60 + n % 100)), lo = model.span.lowerBound, hi = model.span.upperBound
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) { [self] in
                ui.tl.follow(t, in: model)
                ui.tl.finger = TLGaze.bar(x: (t - lo) / (hi - lo) * TLLayout.barW)
            }
            if parts.count > 3 { DispatchQueue.main.asyncAfter(deadline: .now() + 2.4) { [self] in withAnimation(.easeOut(duration: 0.2)) { ui.tl.follow(nil, in: nil) } } }
        case "昨天":
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) { [self] in ui.tl.setDay(ui.tl.today - 1) }
            if parts.count > 2 {                // `:昨天:1630` with a finger held on the bar at that time, once the day is there
                let n = Int(parts[2]) ?? 0, past = DayTimelineModel.build(yesterday, now: nil)
                DispatchQueue.main.asyncAfter(deadline: .now() + 2.4) { [self] in ui.tl.follow(Double(n / 100 * 60 + n % 100), in: past) }
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
    /// opened and shut. Each reports its median and 95th percentile frame time and its worst frame. A word after `:测帧:` runs one kind alone, over and over (`拖`, `甩`, `换日`), so that something
    /// can be watched at work while it does.
    private func runProbe(_ reply: DayReply, _ model: DayTimelineModel, _ cal: Cal, only: String) {
        let tl = ui.tl, lo = model.span.lowerBound, hi = model.span.upperBound
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
                         tl.follow(lo + there(p) * (hi - lo), in: model); tl.finger = TLGaze.bar(x: there(p) * TLLayout.barW) }),
                     pause(0.6, start: { withAnimation(.easeOut(duration: 0.2)) { tl.follow(nil, in: nil) } })]
        let flings = [FrameProbe.Step(label: "list fling down (1 s)", seconds: 1, frame: { p in tl.scroll(to: ymax * fling(p), animated: false) }), pause(0.4),
                      FrameProbe.Step(label: "list fling up (1 s)", seconds: 1, frame: { p in tl.scroll(to: ymax * (1 - fling(p)), animated: false) }), pause(0.8)]
        let days = [FrameProbe.Step(label: "day change to yesterday", seconds: 0.9, start: { tl.setDay(tl.today - 1) }), pause(0.5),
                    FrameProbe.Step(label: "day change back to today", seconds: 0.9, start: { tl.setDay(tl.today) }), pause(0.8)]
        let rest = [FrameProbe.Step(label: "a talk comes in", seconds: 0.9, start: { tl.fixture?.replies[0] = TimelineFixture.today(cal, late: true) }), pause(0.5),
                    FrameProbe.Step(label: "a row opens", seconds: 0.7, start: { withAnimation(.spring(duration: 0.26, bounce: 0.08)) { tl.openRow = "event-demo-1" } }),
                    FrameProbe.Step(label: "a row shuts", seconds: 0.7, start: { withAnimation(.spring(duration: 0.26, bounce: 0.08)) { tl.openRow = nil } })]
        let alone: [String: [FrameProbe.Step]] = ["拖": scrub, "甩": flings, "换日": days, "静": idle, "指": bare]
        let steps = alone[only].map { launch + Array(repeating: $0, count: 6).flatMap { $0 } } ?? launch + idle + bare + scrub + flings + days + rest
        probe.run(steps) { _ in }
    }

    /// A round he speaks and she answers aloud, as the voice session lives it, with made-up states: his words grow as the recognizer hands them over, she thinks, her words come at the pace of her
    /// voice, and the microphone stays open for what he says next. With `document`, the conversation's row of her answer has a written part that says more than she said.
    private func runSpokenRound(_ cal: Cal, document: Bool) {
        earlierRound(cal)
        let t0 = nowMs(), ask = "明天下午三点有什么安排，顺便提醒我晚上给 Mia 回电话"
        let said = "明天下午三点有一个二面，在线上，大概四十五分钟。晚上八点半给 Mia 回电话，我到时候提醒你。"
        let speechEnd = 3.8 + Double(said.count) / 8 + 0.3
        voiceFake = { now in
            var v = VoiceShot()
            let s = (now - t0) / 1000
            if s < 2.6 { v.state = .listening; v.partial = String(ask.prefix(Int(s * 12) + 1)); v.input = 0.3 + 0.7 * voiceLevel(now / 1000 * 1.3) }
            else if s < 3.8 { v.state = .thinking; v.transcript = ask }
            else if s < speechEnd { v.state = .speaking; v.transcript = ask; v.reply = String(said.prefix(Int((s - 3.8) * 8))); v.full = said; v.output = voiceLevel(now / 1000) }
            else if s < speechEnd + 10 { v.state = .listening; v.reply = said; v.full = said }          // the window for what he says next is ten seconds
            return v
        }
        // the conversation's rows of this round, which her row takes when the session is over (`adoptWritten`): the voice part alone, or with a document
        let at = Date().addingTimeInterval(-60), stamp = TalkStream.dayId(at, cal.cal) + "T" + TalkStream.clock(at, cal.cal) + ":00"
        let doc = "<voice>\(said)</voice><document>## 明天\n- 15:00 和 Lumen Labs 的二面，线上，约四十五分钟\n- 20:30 给 Mia 回电话，我到时候提醒你\n\n二面之前我会把你上次面试的笔记整理出来，放在日程里。</document>"
        DispatchQueue.main.asyncAfter(deadline: .now() + 6) { [self] in
            let base = MainActor.assumeIsolated { Conversation.shared.rows }
            MainActor.assumeIsolated {
                Conversation.shared.debugSet(base + [ConversationRow(seq: 33, source: "allen", ts: stamp, text: ask), ConversationRow(seq: 34, source: "jarvis", ts: stamp, text: document ? doc : "<voice>\(said)</voice>")])
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
