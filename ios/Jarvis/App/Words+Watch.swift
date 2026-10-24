import Foundation

extension Words {
    /// Every fixed string of the watch app and its complications. The phone tells the watch the language (`WatchDay.language`).
    @MainActor enum Watch {
        private static func pick(zh: String, en: String) -> String { Words.pick(zh: zh, en: en) }

        static var name: String { pick(zh: "Jarvis", en: "Jarvis") }

        static var now: String { pick(zh: "现在", en: "Now") }
        static var waiting: String { pick(zh: "等你", en: "Waiting") }
        static var next: String { pick(zh: "接下来", en: "Next") }
        /// 今天要办的's 等你 header, the inline complication and the corner's label
        static func waitingCount(_ n: Int) -> String { pick(zh: "等你 \(n)", en: "\(n) waiting") }

        // the cards over her
        static var reminder: String { pick(zh: "提醒", en: "Reminder") }
        static var leave: String { pick(zh: "该出门了", en: "Time to go") }
        /// 该出门了's countdown
        static func minutes(_ n: Int) -> String { pick(zh: "\(n) 分钟", en: "\(n) min") }
        /// 该出门了's line: when to go (`21:52`)
        static func leaveAt(_ time: String) -> String { pick(zh: "\(time) 走", en: "Leave at \(time)") }

        static var doubleTap: String { pick(zh: "双击", en: "Double tap") }
        static var listening: String { pick(zh: "在听", en: "Listening") }
        static var thinking: String { pick(zh: "在想", en: "Thinking") }
        static var noDay: String { pick(zh: "等手机发来今天", en: "Waiting for the phone") }
        static var nothingMore: String { pick(zh: "今天没有别的了", en: "Nothing more today") }

        // 设置: a page off the end of 今天要办的; only what belongs on the wrist
        static var settings: String { pick(zh: "设置", en: "Settings") }
        static var settingsHer: String { pick(zh: "她", en: "Her") }
        static var speaks: String { pick(zh: "她说出声", en: "She speaks aloud") }
        static var speaksOff: String { pick(zh: "关着时只出字", en: "Off: words only") }
        static var speaksOn: String { pick(zh: "从手表喇叭或耳机", en: "From the watch or headphones") }
        static var headphonesOnly: String { pick(zh: "只在戴耳机时", en: "Only with headphones") }
        static var headphonesOnlyNote: String { pick(zh: "外面人多时不外放", en: "Not out loud in a crowd") }
        static var taps: String { pick(zh: "提醒时轻敲", en: "Tap me for reminders") }
        static var link: String { pick(zh: "连接", en: "Connection") }
        static var iPhone: String { pick(zh: "iPhone", en: "iPhone") }
        static var linked: String { pick(zh: "连着", en: "Connected") }
        static var unlinked: String { pick(zh: "没连着", en: "Not connected") }
        static var standalone: String { pick(zh: "不带手机也能用", en: "Works without the phone") }
        static var later: String { pick(zh: "以后", en: "Later") }
        static var standaloneNote: String { pick(zh: "流量版直接连家里", en: "Cellular, straight to home") }
        static var stayTitle: String { pick(zh: "抬手就是她", en: "Raise your wrist to her") }
        static var stayHow: String { pick(zh: "手表「设置 › 通用 › 回到表盘 › Jarvis」选「1 小时后」", en: "On the watch: Settings › General › Return to Clock › Jarvis › After 1 hour") }
        /// the page's last line, with the app's version
        static func elsewhere(_ version: String) -> String { pick(zh: "别的设置在 iPhone 上 · \(version)", en: "Other settings are on the iPhone · \(version)") }

        static var complicationName: String { pick(zh: "她和今天一圈", en: "Her and today's ring") }
        static var complicationDetail: String { pick(zh: "一圈是今天，有事等你时亮一个小点。点一下直接和她说话。", en: "The ring is today, with a dot when something waits. Tap to talk to her.") }
    }
}
