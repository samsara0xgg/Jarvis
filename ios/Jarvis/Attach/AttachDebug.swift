#if DEBUG
import SwiftUI
import UIKit

// Launch with `-attachScene 添加` (and optionally `-attachDelay 2`) to land on a state, for screenshots without touching the screen.
// 添加 · 相册 · 相机 · 相机循环 · 相机返回 · 拍后 · 带附件 · 带附件添加 · 粘贴 · 文件选择 · 发送
// `-attachText 一段很长的话` puts words in the field (to see it grow).

enum AttachDebug {
    static func run(_ m: AttachModel) {
        guard let s = UserDefaults.standard.string(forKey: "attachScene") else { return }
        let d = UserDefaults.standard.double(forKey: "attachDelay")
        DispatchQueue.main.asyncAfter(deadline: .now() + (d > 0 ? d : 1.2)) { apply(s, m) }
    }

    private static func after(_ t: Double, _ f: @escaping () -> Void) { DispatchQueue.main.asyncAfter(deadline: .now() + t, execute: f) }

    private static func apply(_ s: String, _ m: AttachModel) {
        switch s {
        case "添加": m.open()
        case "相册":
            m.open(); after(0.8) { m.go(.grid) }
            after(1.8) {
                for i in 0..<3 { if let a = m.photos.asset(at: i * 2) { m.togglePick(a.localIdentifier) } }
            }
        case "相机": m.open(); after(0.3) { m.go(.camera) }
        case "相机循环": cameraLoop(m)
        case "相机返回": m.open(); after(0.3) { m.go(.camera) }; after(1.5) { m.closeCamera() }
        case "拍后": m.open(); after(0.3) { m.go(.camera) }; after(1.0) { m.shoot() }
        case "带附件": m.attach(samples(), from: [])
        case "带附件添加": m.attach(samples(), from: []); after(0.6) { m.open() }
        case "粘贴": paste(m)
        case "文件选择": m.pickFiles()
        case "发送": m.attach(samples(), from: []); after(1.5) { m.debugSend += 1 }
        default: break
        }
    }

    /// open, camera, close, again: the camera must stay smooth however fast or slowly it is opened and closed (`-attachScene 相机循环`)
    private static func cameraLoop(_ m: AttachModel) {
        var n = 0
        func cycle() {
            guard n < 24 else { print("CAM cycles done"); return }
            n += 1
            let gap = [0.3, 0.9, 0.5, 1.2, 0.15][n % 5]
            m.open()
            after(gap) { m.go(.camera); after(gap + 0.4) { m.close(); after(0.5) { cycle() } } }
        }
        cycle()
    }

    private static func samples() -> [AttachItem] {
        var list = (0..<3).map { i in AttachItem(kind: .photo, title: "照片", thumbnail: gradient(Double(i) * 0.31)) }
        list.append(AttachItem(kind: .file, title: "简历 v2.pdf"))
        list.append(AttachItem(kind: .file, title: "二面准备.md"))
        return list
    }

    private static func gradient(_ hue: Double) -> UIImage {
        UIGraphicsImageRenderer(size: CGSize(width: 168, height: 168)).image { c in
            let cs = [UIColor(hue: hue, saturation: 0.6, brightness: 0.8, alpha: 1).cgColor, UIColor(hue: hue + 0.1, saturation: 0.7, brightness: 0.3, alpha: 1).cgColor]
            let g = CGGradient(colorsSpace: nil, colors: cs as CFArray, locations: [0, 1])!
            c.cgContext.drawLinearGradient(g, start: .zero, end: CGPoint(x: 168, y: 168), options: [])
        }
    }

    /// what a paste of a picture and a file hands to the field, built by hand: this checks the loading and the strip, not the system's menu
    private static func paste(_ m: AttachModel) {
        let png = NSItemProvider(object: gradient(0.55))
        let f = AttachTemp.newFile("资料包.pdf")
        try? Data("%PDF-1.4".utf8).write(to: f)
        guard let file = NSItemProvider(contentsOf: f) else { return }
        m.paste([png, file, NSItemProvider(object: "plain words" as NSString)]) { _ in }
    }
}
#endif
