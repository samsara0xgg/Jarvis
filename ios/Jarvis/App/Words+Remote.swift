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
        static var talk: String { pick(zh: "按住说话，松手打进电脑", en: "Hold to talk; let go to type it on the Mac") }
        static var listening: String { pick(zh: "在听…", en: "Listening…") }
        static var noHearing: String { pick(zh: "现在听不到千问，等 Jarvis 连上大脑再试。", en: "Qwen can't hear right now; try again once Jarvis reaches your brain.") }
        static var trackpad: String { pick(zh: "触控板模式", en: "Trackpad mode") }
        static var launcher: String { pick(zh: "Raycast（⌥空格）", en: "Raycast (⌥Space)") }
        static var dock: String { pick(zh: "叫出程序坞", en: "Show the Dock") }
        static var keys: String { pick(zh: "常用键", en: "Shortcuts") }
        // the shortcut bar, in its order
        static var copy: String { pick(zh: "复制", en: "Copy") }
        static var pasteKey: String { pick(zh: "粘贴", en: "Paste") }
        static var undo: String { pick(zh: "撤销", en: "Undo") }
        static var redo: String { pick(zh: "重做", en: "Redo") }
        static var selectAll: String { pick(zh: "全选", en: "Select all") }
        static var enter: String { pick(zh: "回车", en: "Return") }
        static var escape: String { pick(zh: "Esc", en: "Esc") }
        static var backspace: String { pick(zh: "删除", en: "Delete") }
        static var tab: String { pick(zh: "Tab", en: "Tab") }
        static var switchApp: String { pick(zh: "切换应用", en: "Switch app") }
        static var find: String { pick(zh: "查找", en: "Find") }
        static var save: String { pick(zh: "保存", en: "Save") }
        static var newTab: String { pick(zh: "新标签", en: "New tab") }
        static var closeWindow: String { pick(zh: "关闭", en: "Close") }
        static var quitApp: String { pick(zh: "退出应用", en: "Quit app") }
    }
}
