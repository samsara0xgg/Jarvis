// words: legacy
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

    // 对话模型: kept on the brain (`/inherent/settings`), so these stay nil/empty until a brain that has the keys answers (an older brain: the rows hide)
    var conversationModel: String?
    var conversationEffort: String?
    var modelOptions: [String] = []
    var effortOptions: [String: [String]] = [:]
    static let modelNames = ["opus-fast": "Opus 5.5 快速", "opus": "Opus 5.5", "sonnet": "Sonnet 5.5", "haiku": "Haiku 5.5", "luna": "GPT-6 Luna"]
    static let effortNames = ["none": "关", "low": "低", "medium": "中", "high": "高"]
    var showsConversationModel: Bool { conversationModel != nil && conversationEffort != nil && !modelOptions.isEmpty }
    var effortChoices: [String] { conversationModel.flatMap { effortOptions[$0] } ?? [] }

    @MainActor func refreshConversation() async { await talkToBrain(nil) }

    /// Shows the pick at once, saves it, then takes the brain's answer (a model change can reset the effort); a failed save falls back to what the brain holds.
    @MainActor func saveConversation(model: String? = nil, effort: String? = nil) async {
        var changes: [String: String] = [:]
        if let model { conversationModel = model; changes["conversation_model"] = model }
        if let effort { conversationEffort = effort; changes["conversation_effort"] = effort }
        await talkToBrain(changes)
    }

    @MainActor private func talkToBrain(_ changes: [String: String]?) async {
        let page = try? await BrainLink.shared.call { try await BrainClient.settings(changes, bases: $0, token: $1) }
        if page == nil && changes != nil { await talkToBrain(nil); return }
        guard let page else { return }
        let values = page["values"] as? [String: Any] ?? [:], options = page["options"] as? [String: Any] ?? [:]
        conversationModel = values["conversation_model"] as? String
        conversationEffort = values["conversation_effort"] as? String
        modelOptions = options["conversation_model"] as? [String] ?? []
        effortOptions = options["conversation_effort"] as? [String: [String]] ?? [:]
    }

    // 和她说话
    var siri = "已添加"
    var actionButton = true
    /// Listen for "Jarvis" while the app is open. The voice session holds the switch (off by default); UserDefaults keeps it across launches.
    static let wakeKey = "voice.wakeInApp"
    var wakeInApp: Bool {
        get {
            access(keyPath: \.wakeInApp)
            return MainActor.assumeIsolated { VoiceSession.shared.wakeWordInApp }
        }
        set {
            withMutation(keyPath: \.wakeInApp) { MainActor.assumeIsolated { VoiceSession.shared.wakeWordInApp = newValue } }
            UserDefaults.standard.set(newValue, forKey: Self.wakeKey)
        }
    }
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

    // 随身录音: the recorder holds the switch; the line under it is the day's total and what waits for Wi-Fi
    var lifelogOn: Bool {
        get {
            access(keyPath: \.lifelogOn)
            return MainActor.assumeIsolated { LifelogRecorder.shared.recording }
        }
        set { withMutation(keyPath: \.lifelogOn) { MainActor.assumeIsolated { LifelogRecorder.shared.setOn(newValue) } } }
    }
    /// 今天录了 1 小时 12 分 · 3 段待传
    @MainActor func lifelogLine(now: Date = Date()) -> String {
        let r = LifelogRecorder.shared, u = LifelogUploader.shared
        let open = r.recording ? r.pieceStartedAt.map { max(0, now.timeIntervalSince($0)) } ?? 0 : 0
        var parts = [Self.recordedText(ms: LifelogStore.closedTodayMs + Int64(open * 1000))]
        if u.needsRepair { parts.append("需要重新配对才能上传") } else if u.waiting > 0 { parts.append("\(u.waiting) 段待传") }
        return parts.joined(separator: " · ")
    }
    /// 在家，Mac 在录，手机暂不留音频: why the phone does or does not keep the audio now (ADR 0292); empty while the switch is off
    @MainActor var lifelogReason: String { LifelogGate.shared.line }
    static func recordedText(ms: Int64) -> String {
        let m = Int(ms / 60_000)
        if m < 1 { return "今天还没录" }
        return m < 60 ? "今天录了 \(m) 分" : "今天录了 \(m / 60) 小时 \(m % 60) 分"
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

extension BrainClient {
    /// `GET /inherent/settings` (`changes` nil) or `POST /inherent/settings` with `{"changes": {...}}`; both answer `{values, options, restart_pending}`.
    static func settings(_ changes: [String: String]?, bases: [URL], token: String) async throws -> [String: Any] {
        let body = try changes.map { try JSONSerialization.data(withJSONObject: ["changes": $0]) }
        let r = try await send(changes == nil ? "GET" : "POST", "/inherent/settings", body: body, bases: bases, token: token, unauthorized: .revoked)
        guard let o = try? JSONSerialization.jsonObject(with: r.data) as? [String: Any] else { throw BrainError.garbled }
        return o
    }
}
