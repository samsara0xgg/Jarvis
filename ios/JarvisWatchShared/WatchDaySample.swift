// words: sample — made-up day for debug screenshots
#if DEBUG
import Foundation

/// A made-up day in the shape of the design boards (21:50, the pottery class at 22:00, the bus at 23:10), for the screenshots and the `-watchScene` switch.
extension WatchDay {
    static var sample: WatchDay {
        let sleep: UInt32 = 0x7078CD, mac: UInt32 = 0x4F8CE8, school: UInt32 = 0x8B6AD8, meal: UInt32 = 0xE8A33D, gym: UInt32 = 0x3FB68B, trip: UInt32 = 0xC4D0FF, other: UInt32 = 0x9DA8D8
        return WatchDay(
            date: "2026-10-20", language: "zh", now: 1310, made: .now,
            blocks: [
                .init(t0: 0, t1: 440, rgb: sleep, title: "睡眠", sub: "00:00–07:20 · 7 小时 20 分"),
                .init(t0: 440, t1: 500, rgb: other, title: "早上", sub: "07:20–08:20 · 1 小时"),
                .init(t0: 500, t1: 560, rgb: trip, title: "路上", sub: "12 路 · 08:20–09:20"),
                .init(t0: 560, t1: 680, rgb: school, title: "上课", sub: "UVic · 09:20–11:20 · 2 小时"),
                .init(t0: 680, t1: 740, rgb: meal, title: "午饭", sub: "11:20–12:20 · 1 小时"),
                .init(t0: 740, t1: 800, rgb: mac, title: "做 Jarvis", sub: "12:20–13:20 · 1 小时"),
                .init(t0: 800, t1: 915, rgb: school, title: "上课", sub: "UVic · 13:20–15:15 · 1 小时 55 分"),
                .init(t0: 915, t1: 935, rgb: trip, title: "路上", sub: "12 路 · 15:15–15:35"),
                .init(t0: 935, t1: 1010, rgb: gym, title: "运动", sub: "15:35–16:50 · 1 小时 15 分"),
                .init(t0: 1010, t1: 1075, rgb: meal, title: "晚饭", sub: "16:50–17:55 · 1 小时 5 分"),
                .init(t0: 1075, t1: 1190, rgb: mac, title: "做 Jarvis", sub: "17:55–19:50 · 1 小时 55 分"),
                .init(t0: 1190, t1: 1215, rgb: other, title: "休息", sub: "19:50–20:15 · 25 分钟"),
                .init(t0: 1215, t1: 1310, rgb: mac, title: "做 Jarvis", sub: "20:15–21:50 · 1 小时 35 分"),
            ],
            ahead: [.init(t: 1320, waits: true), .init(t: 1350, waits: false), .init(t: 1380, waits: false), .init(t: 1390, waits: false)],
            headline: .init(title: "22:00 陶艺课", note: "还有 10 分钟", waits: true),
            current: .init(id: "now", symbol: "laptopcomputer", rgb: mac, title: "做 Jarvis", sub: "家", right: "2 小时 15 分"),
            waiting: [
                .init(id: "med", kind: .reminder, symbol: "bell", title: "吃药", sub: "21:30 响过了 · 饭后那次",
                      actions: [.init(id: "ack", title: "吃了"), .init(id: "later", title: "晚点再提醒")]),
                .init(id: "pottery", kind: .card, symbol: "calendar", title: "陶艺课 22:00", sub: "Studio 4 等你确认",
                      actions: [.init(id: "go", title: "去"), .init(id: "skip", title: "不去")]),
            ],
            next: [
                .init(id: "mia", symbol: "phone", title: "给 Mia 回电话", sub: "23:00 · 还有 1 小时 10 分 · 提醒", right: "", planned: true),
                .init(id: "bus", symbol: "bus", title: "出门 · 12 路", sub: "23:10 · 还有 1 小时 20 分", right: "", planned: true),
            ],
            ride: .init(route: "12 路", title: "还有 3 站", sub: "去 UVic · 23:24 到", stops: ["Hillside", "Shelbourne", "Richmond", "Cedar Hill", "UVic"], at: 1, note: "下一站下车，提前轻敲你", alightNext: false),
            leaveAt: Date().addingTimeInterval(4800))
    }

    /// the board's her at rest: nothing waits, and leaving is far off
    static var sampleQuiet: WatchDay { var d = sample; d.waiting = []; return d }
    /// 该出门了 two minutes before he should go
    static var sampleLeaving: WatchDay { var d = sampleQuiet; d.leaveAt = Date().addingTimeInterval(120); return d }

    /// what he said, what she said and the card she put up, for the talk scenes
    static let sampleHeard = "陶艺课改到明天同一时间"
    static let sampleSpoken = "好，改到明天 22:00。"
    static let sampleWritten = "好，改到明天 22:00。\n我回邮件告诉 Studio 4，说你明天这个时间到。"
    static let sampleCard = Waiting(id: "reply", kind: .card, symbol: "envelope", title: "回邮件", sub: "告诉 Studio 4", actions: [.init(id: "send", title: "发吧"), .init(id: "no", title: "先别")])
}
#endif
