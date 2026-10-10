import Photos
import UIKit
import Observation

// The photo library behind the rail and the grid: newest first, small cached thumbnails only.

@Observable
final class AttachPhotoLibrary {
    enum Access { case undetermined, denied, limited, full }
    private(set) var access: Access
    private(set) var result: PHFetchResult<PHAsset>?
    @ObservationIgnored let manager = PHCachingImageManager()

    init() { access = Self.map(PHPhotoLibrary.authorizationStatus(for: .readWrite)) }

    private static func map(_ s: PHAuthorizationStatus) -> Access {
        switch s {
        case .authorized: return .full
        case .limited: return .limited
        case .notDetermined: return .undetermined
        default: return .denied
        }
    }

    var canRead: Bool { access == .full || access == .limited }
    var count: Int { result?.count ?? 0 }
    func asset(at i: Int) -> PHAsset? { guard let r = result, i >= 0, i < r.count else { return nil }; return r.object(at: i) }

    /// asks once (the system remembers the answer), then lists the pictures
    func refresh() {
        let s = PHPhotoLibrary.authorizationStatus(for: .readWrite)
        if s == .notDetermined {
            PHPhotoLibrary.requestAuthorization(for: .readWrite) { [weak self] _ in DispatchQueue.main.async { self?.refresh() } }
            return
        }
        access = Self.map(s)
        guard canRead else { result = nil; return }
        let o = PHFetchOptions()
        o.sortDescriptors = [NSSortDescriptor(key: "creationDate", ascending: false)]
        o.predicate = NSPredicate(format: "mediaType == %d", PHAssetMediaType.image.rawValue)
        result = PHAsset.fetchAssets(with: o)
    }

    /// a grid or rail tile's picture: the quick one first, then the sharp one
    func request(_ asset: PHAsset, side: CGFloat, scale: CGFloat, deliver: @escaping (UIImage) -> Void) -> PHImageRequestID {
        let o = PHImageRequestOptions()
        o.deliveryMode = .opportunistic; o.resizeMode = .fast
        return manager.requestImage(for: asset, targetSize: CGSize(width: side * scale, height: side * scale), contentMode: .aspectFill, options: o) { img, _ in
            if let img { DispatchQueue.main.async { deliver(img) } }
        }
    }

    /// the strip's small copy of a picked photo
    func thumbnail(id: String, side: CGFloat, scale: Double) async -> UIImage? {
        guard let a = PHAsset.fetchAssets(withLocalIdentifiers: [id], options: nil).firstObject else { return nil }
        return await withCheckedContinuation { c in
            let o = PHImageRequestOptions()
            o.deliveryMode = .highQualityFormat; o.resizeMode = .fast; o.isNetworkAccessAllowed = true
            manager.requestImage(for: a, targetSize: CGSize(width: side * scale, height: side * scale), contentMode: .aspectFill, options: o) { img, _ in
                c.resume(returning: img)
            }
        }
    }
}
