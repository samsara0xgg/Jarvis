import Foundation

extension Words {
    /// The fixed words of the link between the phone and the watch: what the phone says in the watch's day, and why a talk from the wrist did not go.
    /// Compiled into the phone and the watch.
    @MainActor enum Relay {
        private static func pick(zh: String, en: String) -> String { Words.pick(zh: zh, en: en) }

        // why a talk did not go (the reason comes to the wrist in the language the phone words in)
        static var phoneAway: String { pick(zh: "手机不在身边", en: "Your iPhone is out of reach") }
        static var notPaired: String { pick(zh: "还没和大脑配对，先在 Jarvis 里扫码配对。", en: "Not paired with the brain yet. Scan its code in Jarvis first.") }
        static var heardNothing: String { pick(zh: "没听清", en: "I didn't catch that") }
        static var tooLong: String { pick(zh: "这个问题太长了，说短一点。", en: "That was too long. Say it shorter.") }
        static var cannotListen: String { pick(zh: "这台设备不能在本地识别语音", en: "This device cannot recognize speech") }
        /// 标一下 that found nothing to mark, or did not go through
        static var nothingToMark: String { pick(zh: "这会儿没有可标的", en: "Nothing to mark right now") }
        static var notMarked: String { pick(zh: "没标上", en: "Not marked") }
        static var turnFailed: String { pick(zh: "这一轮没做成。", en: "That turn did not go through.") }
        static var micOff: String { pick(zh: "麦克风没打开", en: "The microphone is off for Jarvis") }
        /// an answer that did not come in time: the turn goes on and is in today's conversation
        static var working: String { pick(zh: "她还在忙，做完会记在今天的对话里。", en: "She is still on it. It will be in today's conversation.") }

        // the day
        static var waits: String { pick(zh: "等你", en: "Waiting for you") }
        static func stopsLeft(_ n: Int) -> String { pick(zh: "还有 \(n) 站", en: n == 1 ? "1 stop left" : "\(n) stops left") }
        static func toStop(_ name: String) -> String { pick(zh: "到 \(name)", en: "To \(name)") }
        static func nextStop(_ name: String) -> String { pick(zh: "下一站 \(name)", en: "Next: \(name)") }
        static var getOff: String { pick(zh: "下一站下车", en: "Get off at the next stop") }
        static var onTheWay: String { pick(zh: "在路上", en: "On the way") }
        static func route(_ r: String) -> String { pick(zh: "\(r) 路", en: "Route \(r)") }
    }
}
