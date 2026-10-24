import Foundation

extension Words {
    /// The fixed words of the phone's frame: the line under her on the settings page (Settings/SettingsComponents.swift) and the way back from its sub pages.
    @MainActor enum PhoneFrame {
        private static func pick(zh: String, en: String) -> String { Words.pick(zh: zh, en: en) }

        /// the brain answers: `latency` as the settings list says it ("42 ms"), or nil while it has not been measured
        static func brainConnected(latency: String?) -> String {
            let l = latency.map { pick(zh: " · 延迟 \($0)", en: " · \($0) latency") } ?? ""
            return pick(zh: "已连上大脑", en: "Connected to the brain") + l
        }
        static var brainUnpaired: String { pick(zh: "还没连上大脑", en: "Not connected to the brain") }
        static var brainPairing: String { pick(zh: "正在配对…", en: "Pairing…") }
        static var brainOffline: String { pick(zh: "大脑没有回应", en: "The brain isn't answering") }

        static var backToSettings: String { pick(zh: "返回设置", en: "Back to settings") }
    }
}
