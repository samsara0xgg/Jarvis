// words: legacy
import UIKit
import ImageIO
import Photos

// What goes up with a turn (docs/spec.html, `POST /inherent/attachments`, ADR 0211): each picture shrunk to 1568 px wide and sent as JPEG, each text file as it is,
// one file to a request. The brain judges a file by its bytes and answers 415 for what it cannot read (a PDF); these are the limits it holds and the words for its refusals.

enum AttachUpload {
    static let maxPerTurn = 4
    static let maxImageBytes = 4 * 1024 * 1024
    static let maxImageBytesPerTurn = 8 * 1024 * 1024
    static let maxBody = maxImageBytes + 64 * 1024          // a body over this is refused without being read: no use sending one
    static let maxWidth = 1568.0

    struct Part {
        let name: String
        let mime: String
        let data: Data
        let isImage: Bool
    }

    /// a file that cannot go, in the owner's words
    struct Refused: Error { let message: String }

    /// The size to decode a picture at (ImageIO counts the longer side) so that it is at most `maxWidth` wide; its own size when it already is.
    static func maxPixel(width: Double, height: Double) -> Int {
        let k = width > maxWidth ? maxWidth / width : 1
        return Int((max(width, height) * k).rounded())
    }

    /// what the picture is called once it is a JPEG
    static func jpegName(_ title: String) -> String { (title as NSString).deletingPathExtension + ".jpg" }

    /// A picture as the brain takes it: flattened on white (a JPEG has no transparency), at a quality that fits in 4 MiB.
    static func jpeg(_ image: UIImage) -> Data? {
        let f = UIGraphicsImageRendererFormat()
        f.scale = 1; f.opaque = true
        let size = CGSize(width: image.size.width * image.scale, height: image.size.height * image.scale)
        let flat = UIGraphicsImageRenderer(size: size, format: f).image { c in
            UIColor.white.setFill(); c.fill(CGRect(origin: .zero, size: size))
            image.draw(in: CGRect(origin: .zero, size: size))
        }
        for q in [0.8, 0.6, 0.4, 0.25] { if let d = flat.jpegData(compressionQuality: q), d.count <= maxImageBytes { return d } }
        return nil
    }

    /// a picture file, decoded straight at the size it goes up at (the full picture is never held)
    static func shrunk(url: URL) -> UIImage? {
        guard let src = CGImageSourceCreateWithURL(url as CFURL, nil),
              let p = CGImageSourceCopyPropertiesAtIndex(src, 0, nil) as? [CFString: Any],
              let w = p[kCGImagePropertyPixelWidth] as? Double, let h = p[kCGImagePropertyPixelHeight] as? Double else { return nil }
        let turned = ((p[kCGImagePropertyOrientation] as? Int) ?? 1) >= 5          // the photo's own rotation swaps its sides
        return AttachImage.downsample(src, maxPixel: turned ? maxPixel(width: h, height: w) : maxPixel(width: w, height: h))
    }

    /// The parts of a turn, or the first thing that keeps it from going. Nothing is sent before every one is ready.
    static func parts(_ items: [AttachItem], photos: AttachPhotoLibrary) async throws -> [Part] {
        var out: [Part] = []
        for it in items {
            let name = clip(it.title)
            switch it.kind {
            case .photo:
                var image: UIImage?
                if let u = it.url { image = await Task.detached { shrunk(url: u) }.value }
                else if let id = it.assetID { image = await photos.image(id: id, maxWidth: maxWidth) }
                guard let img = image, let data = await Task.detached(operation: { jpeg(img) }).value else { throw Refused(message: "「\(name)」读不出来") }
                out.append(Part(name: jpegName(it.title), mime: "image/jpeg", data: data, isImage: true))
            case .file:
                guard let u = it.url, let data = try? Data(contentsOf: u, options: .mappedIfSafe), !data.isEmpty else { throw Refused(message: "「\(name)」读不出来") }
                if data.count > maxBody { throw Refused(message: "「\(name)」太大了，发小一点的") }
                out.append(Part(name: it.title, mime: "application/octet-stream", data: data, isImage: false))
            }
        }
        if let m = check(count: out.count, imageBytes: out.filter(\.isImage).reduce(0) { $0 + $1.data.count }) { throw Refused(message: m) }
        return out
    }

    /// the limits of one turn, or nil when it is within them
    static func check(count: Int, imageBytes: Int = 0) -> String? {
        if count > maxPerTurn { return "一次最多发 \(maxPerTurn) 个" }
        if imageBytes > maxImageBytesPerTurn { return "图片加起来超过 8 MB" }
        return nil
    }

    static func clip(_ s: String) -> String { s.count > 10 ? String(s.prefix(9)) + "…" : s }

    /// What the owner is told when the brain refuses a file (one line, short: it goes in the status line over the composer). Nothing is retried.
    static func refusal(status: Int, name: String) -> String {
        let n = clip(name)
        switch status {
        case 415: return (name as NSString).pathExtension.lowercased() == "pdf" ? "「\(n)」是 PDF，她读不了" : "「\(n)」她读不了，只收图片和文字"
        case 413: return "「\(n)」太大了，发小一点的"
        case 422: return "她现在读不了图片"
        case 501: return "这个大脑还没开放图片和文件"
        case 507: return "大脑存文件的地方满了"
        default: return "「\(n)」没发出去"
        }
    }

    /// The form `POST /inherent/attachments` reads: one `file`.
    static func multipart(_ part: Part, boundary: String) -> Data {
        var d = Data()
        let safe = part.name.replacingOccurrences(of: "[\"\\\\\r\n]", with: "_", options: .regularExpression)
        d.append(Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"file\"; filename=\"\(safe)\"\r\nContent-Type: \(part.mime)\r\n\r\n".utf8))
        d.append(part.data)
        d.append(Data("\r\n--\(boundary)--\r\n".utf8))
        return d
    }
}

extension AttachPhotoLibrary {
    /// a picked photo as wide as `maxWidth` allows, decoded at that size
    func image(id: String, maxWidth: Double) async -> UIImage? {
        guard let a = PHAsset.fetchAssets(withLocalIdentifiers: [id], options: nil).firstObject else { return nil }
        let w = Double(a.pixelWidth), h = Double(a.pixelHeight), k = w > maxWidth ? maxWidth / w : 1
        return await withCheckedContinuation { c in
            let o = PHImageRequestOptions()
            o.deliveryMode = .highQualityFormat; o.resizeMode = .exact; o.isNetworkAccessAllowed = true
            manager.requestImage(for: a, targetSize: CGSize(width: (w * k).rounded(), height: (h * k).rounded()), contentMode: .aspectFit, options: o) { img, _ in
                c.resume(returning: img)
            }
        }
    }
}
