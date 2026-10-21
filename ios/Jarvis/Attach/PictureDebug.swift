// words: legacy
#if DEBUG
import UIKit

// `-scene pictures`: a tall picture, a message of three, and one the brain has swept, as the conversation's rows would carry them (the pictures are made here and kept
// as the phone keeps what it sent). `-viewer 2` (a picture number, 0 for the tall one) also opens the viewer on the three, at that picture.

extension StarEngine {
    func runPicturesScene() {
        setLevel(0)
        let cal = Cal.cur.cal, now = Date()
        func ts(_ minutesAgo: Double) -> String {
            let c = now.addingTimeInterval(-minutesAgo * 60)
            return TalkStream.dayId(c, cal) + "T" + TalkStream.clock(c, cal) + ":00"
        }
        func art(_ size: CGSize, hue: Double, label: String) -> Data {
            let f = UIGraphicsImageRendererFormat(); f.scale = 1
            return UIGraphicsImageRenderer(size: size, format: f).image { c in
                let g = CGGradient(colorsSpace: nil, colors: [UIColor(hue: hue, saturation: 0.55, brightness: 0.9, alpha: 1).cgColor, UIColor(hue: hue + 0.12, saturation: 0.7, brightness: 0.35, alpha: 1).cgColor] as CFArray, locations: [0, 1])!
                c.cgContext.drawLinearGradient(g, start: .zero, end: CGPoint(x: size.width, y: size.height), options: [])
                UIColor.white.withAlphaComponent(0.35).setStroke()
                for k in stride(from: 0.0, through: size.width, by: 100) { c.cgContext.stroke(CGRect(x: k, y: 0, width: 0.5, height: size.height)) }
                for k in stride(from: 0.0, through: size.height, by: 100) { c.cgContext.stroke(CGRect(x: 0, y: k, width: size.width, height: 0.5)) }
                (label as NSString).draw(at: CGPoint(x: 40, y: 40), withAttributes: [.font: UIFont.boldSystemFont(ofSize: size.width / 5), .foregroundColor: UIColor.white])
            }.jpegData(compressionQuality: 0.9)!
        }
        let made: [(String, Data)] = [("dbg-tall", art(CGSize(width: 900, height: 1600), hue: 0.55, label: "TALL")),
                                      ("dbg-a", art(CGSize(width: 1600, height: 1200), hue: 0.05, label: "1")),
                                      ("dbg-b", art(CGSize(width: 1200, height: 1200), hue: 0.3, label: "2")),
                                      ("dbg-c", art(CGSize(width: 1000, height: 1500), hue: 0.8, label: "3"))]
        func att(_ id: String, gone: Bool = false) -> RowAttachment { RowAttachment(id: id, kind: "image", name: id + ".jpg", gone: gone) }
        func row(_ n: Int, his: Bool, _ minutes: Double, _ text: String, _ files: [RowAttachment] = []) -> ConversationRow {
            ConversationRow(seq: n, source: his ? "allen" : "jarvis", ts: ts(minutes), text: text, id: "r\(n)", attachments: files)
        }
        let rows = [row(1, his: true, 40, "这张图里的招牌写的什么？", [att("dbg-tall")]),
                    row(2, his: false, 40, "<voice>招牌上写的是「老张面馆」，下面一行小字是营业时间。</voice>"),
                    row(3, his: true, 20, "", [att("dbg-a"), att("dbg-b"), att("dbg-c")]),
                    row(4, his: false, 20, "<voice>这三张都收到了，是同一条街的不同角度。</voice>"),
                    row(5, his: true, 5, "还有上周那张呢", [att("dbg-gone", gone: true)]),
                    row(6, his: false, 5, "<voice>那张图已经过期了，我这边也看不到了。</voice>")]
        Task { @MainActor in
            for (id, data) in made { await PictureStore.shared.keep(id: id, data: data) }
            Conversation.shared.debugSet(rows)
            self.openPanel(quiet: true)
            if let n = UserDefaults.standard.string(forKey: "viewer").flatMap(Int.init) {
                try? await Task.sleep(nanoseconds: 1_500_000_000)
                PictureViewer.present(["dbg-a", "dbg-b", "dbg-c"].map { ViewerPicture(id: $0) }, index: n)
            }
        }
    }
}
#endif
