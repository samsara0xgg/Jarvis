import Foundation
import Observation

/// Everything the settings sheet shows. Defaults are the prototype's sample values; wire real data by
/// assigning to these properties (the sheet observes them). The brain rows already read the real link.
@Observable
final class SettingsModel {
    static let shared = SettingsModel()

    struct Switch: Identifiable {
        let title: String
        var detail: String?
        var isOn = true
        var id: String { title }
    }

    struct Record: Identifiable {
        let id = UUID()
        let time: String
        let kind: String
        let value: String
    }

    enum AnswerLanguage: Int, CaseIterable {
        case chinese, english, followMe
        var title: String { ["中文", "English", "跟着我"][rawValue] }
    }

    // 大脑: the real link (Brain/BrainLink.swift); the sheet observes it through these
    var brainName: String { BrainLink.shared.brainName }
    var latency: String { BrainLink.shared.latencyText }
    var statusLine: String { BrainLink.shared.statusLine }
    var brainOnline: Bool { BrainLink.shared.isOnline }

    // 和她说话
    var siri = "已添加"
    var actionButton = true
    var speechRate = 50.0
    var volume = 70.0
    var language = AnswerLanguage.followMe

    // 手机感知
    // The five that report are off until turned on (and persist); the switch is what asks iOS for permission.
    var senses = [
        Switch(title: "位置", detail: "3 分钟前 · 在家", isOn: ReportMemory.isOn(.location)),
        Switch(title: "运动和步数", detail: "今天 8,412 步", isOn: ReportMemory.isOn(.motion)),
        Switch(title: "健康", detail: "昨晚睡了 7 小时 12 分 · 健身 1 次", isOn: ReportMemory.isOn(.health)),
        Switch(title: "专注模式和闹钟", detail: "明早闹钟 07:30", isOn: ReportMemory.isOn(.focusAlarm)),
        Switch(title: "耳机和通话", detail: "AirPods · 20 分钟前断开", isOn: ReportMemory.isOn(.headphonesCalls)),
        Switch(title: "分享给 Jarvis", detail: "今天 1 次"),
    ] {
        didSet {
            let changes = senses.compactMap { s in ReportSense(title: s.title).map { ($0, s.isOn) } }
            Task { @MainActor in changes.forEach { Reporter.shared.setEnabled($0, $1) } }
        }
    }

    // 通知
    var notices = [Switch(title: "等你点头的事"), Switch(title: "提醒"), Switch(title: "她主动说的话")]
    var quietHours = "23:00 – 07:30"

    // 声音: the single source of truth is `Sound.shared`
    var soundOn: Bool {
        get {
            access(keyPath: \.soundOn)
            return Sound.shared.isOn
        }
        set {
            withMutation(keyPath: \.soundOn) { Sound.shared.isOn = newValue }
            if newValue { Sound.shared.play(.on) }
        }
    }

    // 数据
    var todayCount = 214
    var records = [
        Record(time: "21:12", kind: "位置", value: "在家"),
        Record(time: "20:58", kind: "运动", value: "静止"),
        Record(time: "18:31", kind: "健康", value: "力量训练 410 千卡"),
        Record(time: "18:30", kind: "位置", value: "健身房 → 路上"),
        Record(time: "17:40", kind: "位置", value: "到了健身房"),
        Record(time: "16:20", kind: "分享", value: "Lumen Labs 的职位"),
        Record(time: "13:00", kind: "专注模式", value: "开"),
        Record(time: "09:05", kind: "位置", value: "图书馆"),
        Record(time: "08:35", kind: "耳机", value: "AirPods 连上"),
        Record(time: "07:50", kind: "闹钟", value: "响了两次"),
    ]
    /// Called when the user confirms 删除今天的记录.
    var deleteToday: () -> Void = {}

    var version = "Jarvis 1.0 · 只属于你"
}
