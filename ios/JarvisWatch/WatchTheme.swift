import SwiftUI

/// The watch's few colours, the one table of 跨端UI标准 §7: ink, muted, faint, amber for what waits on him, warm white for now, 路上's blue.
enum WatchTheme {
    static let ink = Color(.sRGB, red: 238 / 255, green: 240 / 255, blue: 251 / 255)
    static let muted = Color(.sRGB, red: 196 / 255, green: 204 / 255, blue: 238 / 255, opacity: 0.62)
    static let faint = Color(.sRGB, red: 196 / 255, green: 204 / 255, blue: 238 / 255, opacity: 0.34)
    static let amber = Color(.sRGB, red: 1, green: 201 / 255, blue: 143 / 255)
    static let trip = Color(.sRGB, red: 196 / 255, green: 208 / 255, blue: 1)          // 路上: 该出门了, the ride
    static let warm = Color(.sRGB, red: 1, green: 240 / 255, blue: 220 / 255)
    static let icon = Color(.sRGB, red: 214 / 255, green: 220 / 255, blue: 246 / 255).opacity(0.82)
    static let darkInk = Color(.sRGB, red: 11 / 255, green: 13 / 255, blue: 26 / 255)
    static let track = ink.opacity(0.14)

    static func color(_ rgb: UInt32) -> Color {
        Color(.sRGB, red: Double((rgb >> 16) & 0xFF) / 255, green: Double((rgb >> 8) & 0xFF) / 255, blue: Double(rgb & 0xFF) / 255)
    }
}
