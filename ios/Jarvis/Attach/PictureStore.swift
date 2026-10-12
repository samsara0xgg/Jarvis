import UIKit
import ImageIO
import Photos

// The pictures he sent, shown again in the conversation and opened full screen (ADR 0255). The phone keeps the full-size original of what it sent, in Caches and
// keyed by the attachment id the brain gave; a picture it does not have (another phone sent it, or iOS cleared Caches) is fetched from the brain's stored copy
// (`GET /inherent/attachments/{id}`) and kept the same way. Nothing here runs on the main thread but the cheap memory-cache lookups.

/// What a picture's id resolves to: a file on this phone to decode, the brain saying the file is swept, or no answer right now (it is asked again next time).
enum PictureSource: Equatable {
    case file(URL), gone, failed
}

/// The brain's answer to a fetch of a stored file.
enum PictureFetch {
    case bytes(Data), gone, failed
}

/// A picture ready to draw, or why there is none.
enum PictureLoad {
    case image(UIImage), gone, failed
}

actor PictureStore {
    static let shared = PictureStore(directory: FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0].appendingPathComponent("sent-pictures", isDirectory: true)) { id in
        do {
            let got = try await BrainLink.shared.call { try await BrainClient.attachment(id: id, bases: $0, token: $1) }
            return got.map { .bytes($0) } ?? .gone
        } catch { return .failed }
    }

    private let directory: URL
    private let fetch: @Sendable (String) async -> PictureFetch
    private var inflight: [String: Task<PictureSource, Never>] = [:]
    private var swept: Set<String> = []          // ids the brain said 404 to: not asked again until the app restarts
    nonisolated(unsafe) private let memory = NSCache<NSString, UIImage>()
    nonisolated(unsafe) private let shapes = NSCache<NSString, NSNumber>()

    init(directory: URL, fetch: @escaping @Sendable (String) async -> PictureFetch) {
        self.directory = directory; self.fetch = fetch
        memory.totalCostLimit = 48 * 1024 * 1024
    }

    /// the file of an id: a name made of safe characters only (the id is the brain's, but it ends up in a path)
    nonisolated func url(_ id: String) -> URL {
        directory.appendingPathComponent(String(id.map { $0.isLetter || $0.isNumber || $0 == "-" || $0 == "_" ? $0 : "_" }))
    }

    // MARK: keeping what he sent

    /// the original he picked or took, kept under the id the brain gave; a copy kept before is replaced
    func keep(id: String, copying source: URL) {
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let dest = url(id)
        try? FileManager.default.removeItem(at: dest)
        try? FileManager.default.copyItem(at: source, to: dest)
    }

    func keep(id: String, data: Data) {
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        try? data.write(to: url(id), options: .atomic)
    }

    /// the small copy he sent, for the place where the picture shows until its own file is read
    nonisolated func seed(id: String, image: UIImage) {
        memory.setObject(image, forKey: "\(id)@seed" as NSString)
        shapes.setObject(NSNumber(value: image.size.width / max(1, image.size.height)), forKey: id as NSString)
    }

    // MARK: finding it again

    /// Local copy first, else the brain's stored copy (fetched and kept), else nothing. `gone` is the row's own word that the brain has swept it: then it is not asked for.
    func source(id: String, gone: Bool) async -> PictureSource {
        let local = url(id)
        if FileManager.default.fileExists(atPath: local.path) { return .file(local) }
        if gone || swept.contains(id) { return .gone }
        if let running = inflight[id] { return await running.value }
        let task = Task<PictureSource, Never> {
            switch await fetch(id) {
            case .bytes(let data): keep(id: id, data: data); return .file(local)
            case .gone: swept.insert(id); return .gone
            case .failed: return .failed
            }
        }
        inflight[id] = task
        let got = await task.value
        inflight[id] = nil
        return got
    }

    /// The picture decoded at no more than `maxPixel` on its longer side (never the whole picture), off the main thread. Small sizes are remembered.
    func image(id: String, gone: Bool, maxPixel: Int) async -> PictureLoad {
        if let hit = peek(id: id, maxPixel: maxPixel, exact: true) { return .image(hit) }
        switch await source(id: id, gone: gone) {
        case .gone: return .gone
        case .failed: return .failed
        case .file(let file):
            guard let img = await Task.detached(priority: .userInitiated, operation: { AttachImage.downsample(url: file, maxPixel: maxPixel) }).value else { return .failed }
            if maxPixel <= 1000 { memory.setObject(img, forKey: "\(id)@\(maxPixel)" as NSString, cost: Int(img.size.width * img.size.height * img.scale * img.scale * 4)) }
            shapes.setObject(NSNumber(value: img.size.width / max(1, img.size.height)), forKey: id as NSString)
            return .image(img)
        }
    }

    // MARK: memory, read from the main thread

    /// The picture at that size when it was decoded before; else, unless `exact`, the small copy he sent.
    nonisolated func peek(id: String, maxPixel: Int, exact: Bool = false) -> UIImage? {
        memory.object(forKey: "\(id)@\(maxPixel)" as NSString) ?? (exact ? nil : memory.object(forKey: "\(id)@seed" as NSString))
    }

    /// width over height, once it is known from a picture decoded or seeded
    nonisolated func aspect(id: String) -> Double? { shapes.object(forKey: id as NSString)?.doubleValue }
}

extension AttachPhotoLibrary {
    /// the original of a picked photo, as the library holds it (any format ImageIO reads), for the copy the phone keeps
    func originalData(id: String) async -> Data? {
        guard let a = PHAsset.fetchAssets(withLocalIdentifiers: [id], options: nil).firstObject else { return nil }
        return await withCheckedContinuation { c in
            let o = PHImageRequestOptions()
            o.deliveryMode = .highQualityFormat; o.isNetworkAccessAllowed = true
            manager.requestImageDataAndOrientation(for: a, options: o) { data, _, _, _ in c.resume(returning: data) }
        }
    }
}
