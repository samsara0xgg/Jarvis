import Foundation

extension Words {
    /// The fixed words of the day timeline's rows opened in place (Sky/DayTimelineOpen.swift) and of the marks on its rows. The rest of the timeline still words itself where it is drawn.
    @MainActor enum Timeline {
        private static func pick(zh: String, en: String) -> String { Words.pick(zh: zh, en: en) }

        // a row
        /// after the title of a stretch she is not sure of
        static var unsure: String { pick(zh: "？", en: "?") }
        static var unsureSaid: String { pick(zh: "不太确定", en: "Not sure") }
        static var expand: String { pick(zh: "按两下展开", en: "Double-tap to expand") }
        static var collapse: String { pick(zh: "按两下收起", en: "Double-tap to collapse") }

        // what she asks of it, in one line
        /// her question when the brain sends none: the words she has for the stretch, put to him
        static func isIt(_ title: String) -> String { pick(zh: "是「\(title)」吗？", en: "Was it “\(title)”?") }
        /// a stretch nothing was recorded for
        static var fillIn: String { pick(zh: "补一句", en: "Fill this in") }
        static var yes: String { pick(zh: "是", en: "Yes") }
        static var writeLine: String { pick(zh: "写一句…", en: "Write a line…") }
        /// his answer, as it stands until the brain's next reading of the day says it
        static func noted(_ said: String) -> String { pick(zh: "记下了：\(said)", en: "Noted: \(said)") }
        /// what the brain could not take
        static var notNoted: String { pick(zh: "没存上，稍后再试", en: "Not saved, try again later") }

        // its lines
        /// what took him off the stretch, said and not judged: its minutes, and how often he left it
        static func drift(minutes: Int, switches: Int) -> String {
            let off = minutes > 0 ? pick(zh: "分心 \(minutes) 分", en: "Off it \(minutes) min") : nil
            let away = switches > 0 ? pick(zh: "切走 \(switches) 次", en: "Left it \(switches)×") : nil
            return [off, away].compactMap { $0 }.joined(separator: " · ")
        }
        /// what his phone or tablet had in front while he was at the Mac
        static func onDevice(_ device: String, _ name: String) -> String { pick(zh: "\(device) 上的 \(name)", en: "\(name) on \(device)") }
        /// the rows of a long stretch past the first few, in one line
        static func moreRuns(_ n: Int) -> String { pick(zh: "还有 \(n) 段", en: "\(n) more") }
        /// the Claude Code sessions that ran by themselves, he typed nothing in them, in one line
        static func backgroundSessions(_ n: Int) -> String { pick(zh: "另有 \(n) 个后台会话", en: "\(n) more running on their own") }
        /// the Claude Code sessions a Remote Control bridge on the Mac ran for his claude.ai threads, in one line
        static func remoteSessions(_ n: Int) -> String { pick(zh: "云端线程在 Mac 上跑 · \(n) 个", en: "Cloud threads running on the Mac · \(n)") }
        /// messages that only came in, of one app at about the same time, in one line
        static func received(_ app: String, people: Int, count: Int) -> String { pick(zh: "\(app) · \(people) 人 · 收到 \(count) 条", en: "\(app) · \(people) people · \(count) received") }
        /// how she knows what he was doing: shown when he asks
        static var why: String { pick(zh: "依据", en: "Why") }
    }
}
