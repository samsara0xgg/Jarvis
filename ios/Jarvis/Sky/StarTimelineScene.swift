#if DEBUG
import UIKit
import SwiftUI

/// A made-up day for the timeline, as if the brain had served it: the same day every launch, with now fixed at 21:50. Made-up data only.
enum TimelineFixture {
    static let now = 21 * 60 + 50.0

    /// today: the night, home, a drive, a bite, home again, the bus to school, school, the bus home, and the evening still going, with his Mac work, what he spent and a chat with Claude along the way (`late`: a talk that has just ended is there too)
    static func today(_ cal: Cal, late: Bool = false) -> DayReply { build(cal, day: 0) { b in
        b.add("sleep", 55, 545, ["minutes": 485])
        b.add("stay", 545, 685, ["place": "12 Example St, Town", "kept_place": b.kept("p-home", "家", "home")])
        b.add("talk", 612, 622, ["id": "talk-1", "lane": "her", "turns": 6])
        b.add("move", 685, 785, ["activity": "automotive"])
        b.add("move", 785, 795, ["activity": "walking"])
        b.add("stay", 795, 835, ["place": "A&W, Shelbourne St, Victoria"])
        b.add("move", 835, 845, ["activity": "walking"])
        b.add("stay", 845, 890, ["place": "12 Example St, Town", "kept_place": b.kept("p-home", "家", "home")])
        b.add("ride", 890, 919, ["route": "27", "board_stop": "Hillside Centre", "alight_stop": "UVic Exchange"])
        b.add("stay", 919, 1095, ["place": "Engineering Lab Wing, Finnerty Rd", "kept_place": b.kept("p-uvic", "UVic", "school")])
        b.add("picture", 962, 962, ["id": "picture-pic-demo-1", "attachment": "pic-demo-1", "note": "黑板上写着积分公式，下面一行是作业要求。\n第三题的符号有些看不清。"])
        b.add("call", 1000, 1018, ["id": "call-demo-1", "app": "FaceTime"])
        b.add("ride", 1095, 1130, ["route": "27", "board_stop": "UVic Exchange", "alight_stop": "Hillside Centre"])
        b.add("stay", 1130, nil, ["place": "12 Example St, Town", "kept_place": b.kept("p-home", "家", "home")])
        b.add("talk", 1240, 1262, ["id": "talk-2", "lane": "her", "turns": 14])
        if late { b.add("talk", 1305, 1309, ["id": "talk-3", "lane": "her", "turns": 4]) }
        b.add("event", 1320, 1350, ["id": "event-demo-1", "title": "陶艺课", "source": "mail", "confirmed": false, "place": "Studio 4"])
        b.add("reminder", 1380, nil, ["id": "reminder-demo-1", "lane": "her", "text": "给 Mia 回电话", "fired": false])
        // what he did on the Mac, what he spent, a chat with Claude, and the bus he means to take (explicit ids: the ones above are numbered by their place in this list)
        b.add("work", 630, 680, ["id": "work-demo-1", "active_s": 2520, "projects": [["name": "Jarvis", "seconds": 2520]], "apps": [["name": "Xcode", "seconds": 1560], ["name": "Ghostty", "seconds": 960]]])
        b.add("work", 1175, 1300, ["id": "work-demo-2", "active_s": 5880, "projects": [["name": "Jarvis", "seconds": 3300], ["name": "论文", "seconds": 2580]],
                                   "apps": [["name": "Xcode", "seconds": 2700], ["name": "Safari", "seconds": 2100], ["name": "Ghostty", "seconds": 1080]]])
        b.add("payment", 800, 800, ["id": "payment-demo-1", "source": "alipay", "amount": "12.40", "currency": "CAD", "direction": "out", "counterparty": "A&W", "text": "Teen Burger Combo"])
        b.add("claude_chat", 1210, 1235, ["id": "claude-demo-1", "source": "claude", "text": "积分换元的符号问题", "messages": 14])
        b.add("departure", 1400, 1400, ["id": "departure-demo-1", "title": "12 · Hillside Centre", "route": "12", "board_stop": "Hillside Centre", "to": "UVic", "leave_at_ms": b.ms(1390)])
        b.extra["line"] = ["text": "今天去了学校，下午在 UVic 待得最久，晚上回家早点休息。", "draft": true, "written_at": "2026-10-19T21:30:00+00:00"]
        b.extra["highlights"] = [["id": "call-demo-1", "label": "和 Mia 聊了很久", "by": "her"]]
        b.extra["missing"] = ["calendar"]
    } }

    /// the day before: a quieter one, with the night that began the evening before and a stay that is still going
    static func yesterday(_ cal: Cal) -> DayReply { build(cal, day: -1) { b in
        b.add("sleep", -40, 425, ["minutes": 440])
        b.add("stay", 425, 520, ["place": "12 Example St, Town", "kept_place": b.kept("p-home", "家", "home")])
        b.add("move", 520, 535, ["activity": "walking"])
        b.add("stay", 535, 1050, ["place": "Engineering Lab Wing, Finnerty Rd", "kept_place": b.kept("p-uvic", "UVic", "school")])
        b.add("move", 1050, 1065, ["activity": "walking"])
        b.add("stay", 1065, nil, ["place": "12 Example St, Town", "kept_place": b.kept("p-home", "家", "home")])
        b.add("talk", 1325, 1330, ["id": "talk-y1", "lane": "her", "turns": 3])
        b.extra["line"] = ["text": "安静的一天，大部分时间在学校。", "draft": false, "written_at": "2026-10-18T23:30:00+00:00"]
    } }

    final class Builder {
        let cal: Cal, day: Int
        var items: [[String: Any]] = []
        var extra: [String: Any] = [:]
        init(_ cal: Cal, _ day: Int) { self.cal = cal; self.day = day }
        func ms(_ minute: Double) -> Int64 { Int64(cal.date(day).timeIntervalSince1970 * 1000 + minute * 60_000) }
        func kept(_ id: String, _ name: String, _ role: String) -> [String: Any] { ["id": id, "name": name, "role": role] }
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
        var reply: [String: Any] = ["date": cal.iso(day), "tz": "UTC", "start_ms": b.ms(0), "end_ms": b.ms(1440), "now_ms": b.ms(day == 0 ? now : 1440),
                                    "items": b.items, "missing": [String]()]
        reply.merge(b.extra) { $1 }
        let data = try! JSONSerialization.data(withJSONObject: reply)
        return try! JSONDecoder().decode(DayReply.self, from: data)
    }
}

extension StarEngine {
    /// `-scene 时间线` puts the timeline on the made-up day (now 21:50), whatever the setting was; the words after a colon go further:
    /// `:早上` the list at the morning, `:对话` a talk opened, `:照片` the picture opened, `:工作` the evening's Mac work opened (its span is bracketed on the bar), `:透镜:路上` (or 睡, Mac, 通话, 和她, 花, 照片) that chip pressed, `:拖:1015` a finger held on the bar at that time (a fourth word after it lifts the finger again), `:等你` a card waiting on him over the list (`:等你展开` with it opened, `:等你晚到` with it arriving
    /// a moment later), `:关于` the event row opened and a turn going out over it (its chip is the talk's first row; the panel opens a moment later), `:新增` a talk coming in a moment later, `:走时` the clock moving on a minute a moment later, `:昨天` the day before, `:切换` the dial changing to the timeline a moment later (launch with `-homeModule stars`), `:切回` the timeline changing to the dial.
    func runTimelineScene(_ name: String) {
        RingScene.on = true                     // the made-up day stays: the brain is not asked and the world is not swapped back
        switchWorld(real: true)
        let cal = Cal.cur, parts = name.split(separator: ":").map(String.init), mode = parts.count > 1 ? parts[1] : ""
        let today = TimelineFixture.today(cal), yesterday = TimelineFixture.yesterday(cal)
        MainActor.assumeIsolated {
            ui.tl.fixture = .init(replies: [0: today, -1: yesterday], now: TimelineFixture.now)
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
        let model = DayTimelineModel.build(today, day: ui.tl.today, now: TimelineFixture.now)
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
        case "关于":
            show("event-demo-1", open: true)
            // an earlier round of talk, so the new one is the second, as it is for him
            let at = Date().addingTimeInterval(-3 * 3600), stamp = TalkStream.dayId(at, cal.cal) + "T" + TalkStream.clock(at, cal.cal) + ":00"
            MainActor.assumeIsolated {
                Conversation.shared.debugSet([ConversationRow(seq: 1, source: "allen", ts: stamp, text: "明天下午有什么安排？"),
                                              ConversationRow(seq: 2, source: "jarvis", ts: stamp, text: "<voice>下午三点有一个二面，在线上。</voice>")])
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.8) { [self] in
                captureAbout()              // what a typed turn does as it goes out: the open row is the chip
                beginTalk(); lingerHold = true
                addTurn(his: true, "把它推到明天")
                addTurn(his: false, "好，我把陶艺课推到明天晚上十点。")
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 3.6) { [self] in openPanel(quiet: true) }
        case "照片": show("picture-pic-demo-1", open: true)
        case "工作": show("work-demo-2", open: true)
        case "透镜":
            let lens = [("睡", TLLens.sleep), ("路上", .trip), ("Mac", .work), ("通话", .call), ("和她", .talk), ("花", .payment), ("照片", .picture)].first { $0.0 == (parts.count > 2 ? parts[2] : "") }?.1
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) { [self] in withAnimation(.easeOut(duration: 0.15)) { ui.tl.lens = lens } }
        case "拖":
            let n = Int(parts.count > 2 ? parts[2] : "") ?? 0, t = Double(n / 100 * 60 + n % 100)
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) { [self] in ui.tl.follow(t, in: model) }
            if parts.count > 3 { DispatchQueue.main.asyncAfter(deadline: .now() + 2.4) { [self] in withAnimation(.easeOut(duration: 0.2)) { ui.tl.follow(nil, in: nil) } } }
        case "昨天": DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) { [self] in ui.tl.setDay(ui.tl.today - 1) }
        case "新增": DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [self] in ui.tl.fixture?.replies[0] = TimelineFixture.today(cal, late: true) }
        case "走时": DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [self] in ui.tl.fixture?.now += 1 }
        case "切换": DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { HomeModule.shared.kind = .timeline }
        case "切回": DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { HomeModule.shared.kind = .stars }
        default: break
        }
        wake()
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
