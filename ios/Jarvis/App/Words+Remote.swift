import Foundation

extension Words {
    /// Every fixed string of the remote view (Remote/): his Mac, driven from here.
    @MainActor enum Remote {
        private static func pick(zh: String, en: String) -> String { Words.pick(zh: zh, en: en) }

        static var title: String { pick(zh: "控制电脑", en: "Control your Mac") }
        static var pairHow: String { pick(zh: "在 Mac 上运行 JarvisRemote pair，配对链接会到这台 iPad 的剪贴板，再点下面。", en: "Run JarvisRemote pair on the Mac; the pairing link reaches this iPad's clipboard. Then tap below.") }
        static var paste: String { pick(zh: "粘贴配对", en: "Paste pairing") }
        static var notALink: String { pick(zh: "剪贴板里不是配对链接。", en: "The clipboard holds no pairing link.") }
        static var connecting: String { pick(zh: "正在连电脑…", en: "Connecting…") }
        static var closed: String { pick(zh: "连不上电脑。它开着吗？Jarvis Remote 在跑吗？", en: "Can't reach the Mac. Is it on, and is Jarvis Remote running?") }
        static var retry: String { pick(zh: "重试", en: "Retry") }
        static var refused: String { pick(zh: "电脑不认这个配对了，重新配一次。", en: "The Mac no longer accepts this pairing. Pair again.") }
        static var repair: String { pick(zh: "重新配对", en: "Pair again") }
        static var close: String { pick(zh: "关闭", en: "Close") }
        static var keyboard: String { pick(zh: "键盘", en: "Keyboard") }
        static var unlock: String { pick(zh: "解锁", en: "Unlock") }
        static var passwordTitle: String { pick(zh: "Mac 的登录密码", en: "Your Mac's login password") }
        static var passwordNote: String { pick(zh: "只存在这台 iPad 的钥匙串里，解锁时发给你的 Mac。", en: "Kept only in this iPad's Keychain, and sent to your Mac to unlock it.") }
        static var password: String { pick(zh: "密码", en: "Password") }
        static var saveAndUnlock: String { pick(zh: "保存并解锁", en: "Save and unlock") }
        static var forget: String { pick(zh: "忘掉密码", en: "Forget password") }
        static var cancel: String { pick(zh: "取消", en: "Cancel") }
    }
}
