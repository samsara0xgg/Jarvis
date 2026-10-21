import Foundation

extension Words {
    /// Every fixed string of the iPad's own home (Pad/). What it shares with the phone (the rows, the bar, the chips, an opened row) words itself where the phone does.
    @MainActor enum Pad {
        private static func pick(zh: String, en: String) -> String { Words.pick(zh: zh, en: en) }

        // her menu
        static var type: String { pick(zh: "打字", en: "Type") }
        static var history: String { pick(zh: "历史", en: "History") }
        static var tidy: String { pick(zh: "理一下", en: "Tidy up") }
        static var tidying: String { pick(zh: "在理…", en: "Tidying…") }
        static var today: String { pick(zh: "回到今天", en: "Today") }
        static var settings: String { pick(zh: "设置", en: "Settings") }
        static var her: String { pick(zh: "Jarvis，点一下说话，按住打开菜单", en: "Jarvis: tap to talk, hold for her menu") }

        // the stage
        static var now: String { pick(zh: "现在", en: "Now") }
        static func waiting(_ n: Int) -> String { pick(zh: "等你 \(n)", en: "Waiting on you · \(n)") }
        static var backToNow: String { pick(zh: "今天", en: "Today") }

        // the day
        static var today0: String { pick(zh: "今天", en: "Today") }
        static var unpaired: String { pick(zh: "还没连上大脑，连上以后这里会排出你的一天。", en: "Not connected to your brain yet. Once it is, your day lines up here.") }
        static var nothing: String { pick(zh: "这一天没有记下什么。", en: "Nothing was recorded that day.") }

        // the talk and the field in her band
        static var more: String { pick(zh: "全文 ›", en: "All ›") }
        static var field: String { pick(zh: "跟她说…", en: "Talk to her…") }
        static var send: String { pick(zh: "发送", en: "Send") }
        static var notSent: String { pick(zh: "没发出去，点重试", en: "Not sent. Tap to retry") }
        static var retry: String { pick(zh: "重试", en: "Retry") }
        static var bar: String { pick(zh: "一天的概览", en: "The day at a glance") }
    }
}
