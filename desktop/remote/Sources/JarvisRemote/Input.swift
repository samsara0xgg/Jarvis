import AppKit
import Foundation

/// What the iPad does, posted as if done here. Positions arrive as fractions of the main display (0…1, top-left origin);
/// key codes are macOS virtual key codes and `mods` the CGEventFlags bits, both already translated on the iPad.
///   {"t": "move", "x", "y"}            {"t": "down" | "up", "x", "y", "b": 0 left | 1 right}
///   {"t": "scroll", "dx", "dy"}        pixels, positive dy scrolls the content down toward him
///   {"t": "key", "code", "down", "mods"}   {"t": "text", "s"}
final class Input {
    private var held: CGMouseButton?
    private var lastDown = (at: Date.distantPast, where: CGPoint.zero, clicks: 0)

    func handle(_ j: [String: Any]) {
        switch j["t"] as? String {
        case "move":
            let p = point(j)
            switch held {
            case .left?: mouse(.leftMouseDragged, p, .left)
            case .right?: mouse(.rightMouseDragged, p, .right)
            default: mouse(.mouseMoved, p, .left)
            }
        case "down":
            let p = point(j), b: CGMouseButton = j["b"] as? Int == 1 ? .right : .left
            // A second press soon and close by is a double click; apps read the count from the event, not the timing.
            let near = hypot(p.x - lastDown.where.x, p.y - lastDown.where.y) < 6
            let clicks = Date().timeIntervalSince(lastDown.at) < NSEvent.doubleClickInterval && near ? lastDown.clicks + 1 : 1
            lastDown = (Date(), p, clicks)
            held = b
            mouse(b == .right ? .rightMouseDown : .leftMouseDown, p, b, clicks: clicks)
        case "up":
            let p = point(j), b: CGMouseButton = j["b"] as? Int == 1 ? .right : .left
            held = nil
            mouse(b == .right ? .rightMouseUp : .leftMouseUp, p, b, clicks: lastDown.clicks)
        case "scroll":
            let dy = Int32((j["dy"] as? Double ?? 0).rounded()), dx = Int32((j["dx"] as? Double ?? 0).rounded())
            CGEvent(scrollWheelEvent2Source: nil, units: .pixel, wheelCount: 2, wheel1: dy, wheel2: dx, wheel3: 0)?.post(tap: .cghidEventTap)
        case "key":
            guard let code = j["code"] as? Int, let e = CGEvent(keyboardEventSource: nil, virtualKey: CGKeyCode(code), keyDown: j["down"] as? Bool ?? true) else { return }
            e.flags = CGEventFlags(rawValue: UInt64(j["mods"] as? Int ?? 0))
            e.post(tap: .cghidEventTap)
        case "text":
            // The on-screen keyboard's characters, whatever the Mac's layout: typed as Unicode, 20 UTF-16 units per event (the event's limit).
            let units = Array((j["s"] as? String ?? "").utf16)
            for start in stride(from: 0, to: units.count, by: 20) {
                let chunk = Array(units[start ..< min(start + 20, units.count)])
                for down in [true, false] {
                    guard let e = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: down) else { continue }
                    e.flags = []
                    e.keyboardSetUnicodeString(stringLength: chunk.count, unicodeString: chunk)
                    e.post(tap: .cghidEventTap)
                }
            }
        default:
            break
        }
    }

    private func point(_ j: [String: Any]) -> CGPoint {
        let b = CGDisplayBounds(CGMainDisplayID())
        let x = min(max(j["x"] as? Double ?? 0, 0), 1), y = min(max(j["y"] as? Double ?? 0, 0), 1)
        return CGPoint(x: b.minX + x * (b.width - 1), y: b.minY + y * (b.height - 1))
    }

    private func mouse(_ type: CGEventType, _ p: CGPoint, _ b: CGMouseButton, clicks: Int = 0) {
        guard let e = CGEvent(mouseEventSource: nil, mouseType: type, mouseCursorPosition: p, mouseButton: b) else { return }
        if clicks > 0 { e.setIntegerValueField(.mouseEventClickState, value: Int64(clicks)) }
        e.post(tap: .cghidEventTap)
    }
}
