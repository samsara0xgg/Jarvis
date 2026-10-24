import AppKit
import Network

// Jarvis Remote: his iPad sees this Mac's main display and drives it, over the tailnet only (ADR 0406).
//   JarvisRemote        serve on port 5960
//   JarvisRemote pair   put the pairing link on the clipboard (Universal Clipboard carries it to the iPad); the token never reaches the screen

setlinebuf(stdout)   // the LaunchAgent's log is a file
let port: NWEndpoint.Port = 5960
let tokenFile = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/Application Support/Jarvis Remote/token")

func token() -> String {
    if let t = try? String(contentsOf: tokenFile, encoding: .utf8), !t.isEmpty { return t.trimmingCharacters(in: .whitespacesAndNewlines) }
    var bytes = [UInt8](repeating: 0, count: 32)
    _ = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
    let t = Data(bytes).base64EncodedString().replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "=", with: "")
    try? FileManager.default.createDirectory(at: tokenFile.deletingLastPathComponent(), withIntermediateDirectories: true)
    FileManager.default.createFile(atPath: tokenFile.path, contents: Data(t.utf8), attributes: [.posixPermissions: 0o600])
    return t
}

/// This Mac's tailnet IPv4 address (100.64.0.0/10).
func tailnetAddress() -> String? {
    var head: UnsafeMutablePointer<ifaddrs>?
    guard getifaddrs(&head) == 0 else { return nil }
    defer { freeifaddrs(head) }
    var p = head
    while let i = p {
        if let a = i.pointee.ifa_addr, a.pointee.sa_family == UInt8(AF_INET) {
            let ip = a.withMemoryRebound(to: sockaddr_in.self, capacity: 1) { UInt32(bigEndian: $0.pointee.sin_addr.s_addr) }
            if ip >> 22 == (100 << 2 | 1) { return "\(ip >> 24).\(ip >> 16 & 255).\(ip >> 8 & 255).\(ip & 255)" }
        }
        p = i.pointee.ifa_next
    }
    return nil
}

/// Only the tailnet and this machine may knock: 100.64.0.0/10, Tailscale's fd7a:115c:a1e0::/48, loopback.
func allowed(_ e: NWEndpoint) -> Bool {
    guard case .hostPort(let host, _) = e else { return false }
    switch host {
    case .ipv4(let a):
        let b = [UInt8](a.rawValue)
        return (b[0] == 100 && b[1] & 0xC0 == 64) || b[0] == 127
    case .ipv6(let a):
        let b = [UInt8](a.rawValue)
        return b.starts(with: [0xfd, 0x7a, 0x11, 0x5c, 0xa1, 0xe0]) || a == .loopback
    default:
        return false
    }
}

if CommandLine.arguments.dropFirst().first == "pair" {
    guard let host = tailnetAddress() else { print("no tailnet address: is Tailscale up?"); exit(1) }
    let link = "jarvis://remote?host=\(host)&port=\(port.rawValue)&token=\(token())"
    NSPasteboard.general.clearContents()
    NSPasteboard.general.setString(link, forType: .string)
    print("pairing link for \(host):\(port.rawValue) copied; on the iPad: Jarvis › 控制电脑 › 粘贴配对")
    exit(0)
}

let secret = token()
let listener = try NWListener(using: .tcp, on: port)
listener.newConnectionHandler = { c in
    guard allowed(c.endpoint) else { c.cancel(); return }
    Session(c, secret: secret).begin()
}
listener.stateUpdateHandler = { if case .failed(let e) = $0 { print("listener failed: \(e)"); exit(1) } }
listener.start(queue: .main)
print("Jarvis Remote on :\(port.rawValue)")

NSApplication.shared.setActivationPolicy(.accessory)
NSApplication.shared.run()
