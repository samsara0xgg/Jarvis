import Network
import SwiftUI
import UIKit

/// His Mac, driven from here (ADR 0406). The Mac runs Jarvis Remote (desktop/remote); `JarvisRemote pair` there puts a
/// `jarvis://remote?host=…&port=…&token=…` link on the clipboard, and Universal Clipboard brings it here to be pasted.
/// The link and his Mac's login password live in the Keychain, nowhere else.
@MainActor @Observable final class RemoteMac {
    static let shared = RemoteMac()

    private(set) var host = ""
    private(set) var port: UInt16 = 5960
    private(set) var token = ""
    var isPaired: Bool { !token.isEmpty }
    /// Whether Jarvis Remote answered the last knock (every 30 s while paired).
    private(set) var isReachable = false
    /// The stream's width (0 = the Mac chooses) and what gives when the link is short ("fps" keeps frames, "quality" keeps sharpness).
    /// The 画面 panel sets them; they are kept for the next connect.
    var width: Int { didSet { UserDefaults.standard.set(width, forKey: "remote-width") } }
    var prefer: String { didSet { UserDefaults.standard.set(prefer, forKey: "remote-prefer") } }

    private static let linkAccount = "remote-mac"
    private static let passwordAccount = "remote-mac-password"

    private init() {
        width = UserDefaults.standard.integer(forKey: "remote-width")
        prefer = UserDefaults.standard.string(forKey: "remote-prefer") ?? "fps"
        Task { while true { reload(); await probe(); try? await Task.sleep(for: .seconds(30)) } }
    }

    /// A launch in the background (a location wake) can come before the Keychain is readable after a restart and find no pairing,
    /// which then showed the pair card although the pairing was kept: it looks again until it finds one.
    private func reload() {
        if !isPaired, let link = Keychain.get(Self.linkAccount) { _ = adopt(link) }
    }

    /// Opens the remote view over everything, from wherever the app is.
    static func open() { shared.reload(); shared.present() }

    /// Takes a pairing link; false if the text is not one.
    func pair(_ text: String) -> Bool {
        guard adopt(text.trimmingCharacters(in: .whitespacesAndNewlines)) else { return false }
        Keychain.set(text.trimmingCharacters(in: .whitespacesAndNewlines), Self.linkAccount)
        Task { await probe() }
        return true
    }

    func unpair() {
        Keychain.delete(Self.linkAccount)
        host = ""; token = ""; isReachable = false
    }

    var password: String? { Keychain.get(Self.passwordAccount) }
    func savePassword(_ p: String) { Keychain.set(p, Self.passwordAccount) }
    func forgetPassword() { Keychain.delete(Self.passwordAccount) }

    private func adopt(_ link: String) -> Bool {
        guard let c = URLComponents(string: link), c.scheme == "jarvis", c.host == "remote",
              let h = c.queryItems?.first(where: { $0.name == "host" })?.value, !h.isEmpty,
              let t = c.queryItems?.first(where: { $0.name == "token" })?.value, !t.isEmpty else { return false }
        host = h; token = t
        port = c.queryItems?.first(where: { $0.name == "port" })?.value.flatMap(UInt16.init) ?? 5960
        return true
    }

    func probe() async {
        guard isPaired, let p = NWEndpoint.Port(rawValue: port) else { isReachable = false; return }
        let c = NWConnection(host: NWEndpoint.Host(host), port: p, using: .tcp)
        isReachable = await withCheckedContinuation { (k: CheckedContinuation<Bool, Never>) in
            let once = Once(k)
            c.stateUpdateHandler = { s in
                switch s {
                case .ready: once.resume(true)
                case .failed, .waiting: once.resume(false)
                default: break
                }
            }
            c.start(queue: .global())
            DispatchQueue.global().asyncAfter(deadline: .now() + 3) { once.resume(false) }
        }
        c.cancel()
    }

    private func present() {
        let scenes = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }
        let scene = scenes.first { $0.activationState == .foregroundActive } ?? scenes.first
        guard var top = (scene?.keyWindow ?? scene?.windows.first)?.rootViewController else { return }
        while let next = top.presentedViewController { top = next }
        if top is UIHostingController<RemoteScreen> { return }
        var host: UIHostingController<RemoteScreen>!
        host = UIHostingController(rootView: RemoteScreen(close: { host?.dismiss(animated: true) }))
        host.modalPresentationStyle = .fullScreen
        host.view.backgroundColor = .black
        top.present(host, animated: true)
    }
}

/// A continuation resumed by whichever of the connection and the timeout comes first.
private final class Once: @unchecked Sendable {
    private var k: CheckedContinuation<Bool, Never>?
    private let lock = NSLock()
    init(_ k: CheckedContinuation<Bool, Never>) { self.k = k }
    func resume(_ v: Bool) {
        lock.lock(); let k = self.k; self.k = nil; lock.unlock()
        k?.resume(returning: v)
    }
}
