import UIKit

/// The iPad's keys in the Mac's terms: HID usage → macOS virtual key code, UIKit modifiers → CGEventFlags bits.
enum RemoteKeys {
    static let returnKey = 36, delete = 51

    static func mac(_ usage: UIKeyboardHIDUsage) -> Int? { table[usage.rawValue] }

    static func flags(_ m: UIKeyModifierFlags) -> Int {
        var f = 0
        if m.contains(.alphaShift) { f |= 0x10000 }
        if m.contains(.shift) { f |= 0x20000 }
        if m.contains(.control) { f |= 0x40000 }
        if m.contains(.alternate) { f |= 0x80000 }
        if m.contains(.command) { f |= 0x100000 }
        return f
    }

    private static let table: [Int: Int] = {
        var t: [Int: Int] = [:]
        // a…z
        for (i, code) in [0, 11, 8, 2, 14, 3, 5, 4, 34, 38, 40, 37, 46, 45, 31, 35, 12, 15, 1, 17, 32, 9, 13, 7, 16, 6].enumerated() { t[0x04 + i] = code }
        // 1…9, 0
        for (i, code) in [18, 19, 20, 21, 23, 22, 26, 28, 25, 29].enumerated() { t[0x1E + i] = code }
        // F1…F12
        for (i, code) in [122, 120, 99, 118, 96, 97, 98, 100, 101, 109, 103, 111].enumerated() { t[0x3A + i] = code }
        let rest: [Int: Int] = [
            0x28: 36, 0x29: 53, 0x2A: 51, 0x2B: 48, 0x2C: 49,           // return, escape, delete, tab, space
            0x2D: 27, 0x2E: 24, 0x2F: 33, 0x30: 30, 0x31: 42,           // - = [ ] \
            0x33: 41, 0x34: 39, 0x35: 50, 0x36: 43, 0x37: 47, 0x38: 44, // ; ' ` , . /
            0x39: 57, 0x64: 10,                                         // caps lock, the ISO key
            0x4A: 115, 0x4B: 116, 0x4C: 117, 0x4D: 119, 0x4E: 121,      // home, page up, forward delete, end, page down
            0x4F: 124, 0x50: 123, 0x51: 125, 0x52: 126,                 // → ← ↓ ↑
        ]
        t.merge(rest) { $1 }
        return t
    }()
}
