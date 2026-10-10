import SwiftUI
import UIKit
import Observation
import ImageIO
import UniformTypeIdentifiers

// What the composer holds next to the words: photos and files, and the glass panel that picks them.
// The rest of the app reads `StarUI.attach`: `items` to see what is attached, `take()` to collect and clear them when the user sends.

struct AttachItem: Identifiable {
    enum Kind { case photo, file }
    let id = UUID()
    let kind: Kind
    var title: String                 // the file name, or what a screen reader says for a photo
    var thumbnail: UIImage?           // photos: a small copy for the strip, never the full picture
    var assetID: String?              // photos from the library: PHAsset.localIdentifier
    var url: URL?                     // files and pasted or camera pictures: the copy in the temp folder
    var ext: String { (title as NSString).pathExtension.lowercased() }
    var stem: String { (title as NSString).deletingPathExtension }
}

enum AttachPage { case menu, grid, camera }

/// a camera photo waiting for 重拍 / 使用照片
struct AttachShot {
    let preview: UIImage              // the frozen frame
    let thumb: UIImage                // the strip's copy
    let url: URL                      // the JPEG, in the temp folder
}

/// a copy of a picked photo on its way from where it was picked to its place in the strip
struct AttachFlight: Identifiable {
    let id: UUID
    let image: UIImage
    let from: CGRect
    let delay: Double
    var dest: CGRect?
}

/// where things are on screen (design space), rewritten while scrolling: kept out of observation so it costs no redraw
final class AttachFrames { var rects: [String: CGRect] = [:] }

enum AttachTemp {
    private static let prefix = "attach-"
    static func newFile(_ name: String) -> URL {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(prefix + UUID().uuidString, isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent(name)
    }
    /// removes a file made by `newFile`, with its folder
    static func discard(_ url: URL?) {
        guard let dir = url?.deletingLastPathComponent(), dir.lastPathComponent.hasPrefix(prefix) else { return }
        try? FileManager.default.removeItem(at: dir)
    }
}

enum AttachImage {
    /// a small copy, decoded straight at the small size (the full picture is never loaded)
    static func downsample(_ src: CGImageSource, maxPixel: Int) -> UIImage? {
        let o: [CFString: Any] = [kCGImageSourceCreateThumbnailFromImageAlways: true, kCGImageSourceCreateThumbnailWithTransform: true, kCGImageSourceThumbnailMaxPixelSize: maxPixel]
        return CGImageSourceCreateThumbnailAtIndex(src, 0, o as CFDictionary).map { UIImage(cgImage: $0) }
    }
    static func downsample(url: URL, maxPixel: Int) -> UIImage? { CGImageSourceCreateWithURL(url as CFURL, nil).flatMap { downsample($0, maxPixel: maxPixel) } }
    static func downsample(data: Data, maxPixel: Int) -> UIImage? { CGImageSourceCreateWithData(data as CFData, nil).flatMap { downsample($0, maxPixel: maxPixel) } }

    /// a file already in the temp folder becomes a photo (with its small copy) or a file chip
    static func item(copy url: URL) -> AttachItem {
        if let t = UTType(filenameExtension: url.pathExtension), t.conforms(to: .image), let th = downsample(url: url, maxPixel: 240) {
            return AttachItem(kind: .photo, title: url.lastPathComponent, thumbnail: th, url: url)
        }
        return AttachItem(kind: .file, title: url.lastPathComponent, url: url)
    }
}

@Observable
final class AttachModel {
    // what is attached
    var items: [AttachItem] = []
    var leaving: [AttachItem] = []              // after sending, the strip closes with its pictures still in it
    var flights: [AttachFlight] = []
    var hasItems: Bool { !items.isEmpty }
    // the box: the field row grows with the words (48 for one line), the strip adds 72 above them
    var fieldH = AttachField.minH
    var rise: Double { (hasItems ? 72 : 0) + fieldH - AttachField.minH }
    // the panel
    var isOpen = false
    var mounted = false                         // the panel's views exist: open, or still closing
    var page: AttachPage = .menu
    var picks: [String] = []                    // photo ids in the order they were tapped in the grid
    var shot: AttachShot?
    var importing = false
    // set from the environment by the overlay
    var reduceMotion = false
    var displayScale = 3.0

    #if DEBUG
    var debugSend = 0                           // AttachDebug: press send without a finger
    #endif
    @ObservationIgnored let photos = AttachPhotoLibrary()
    @ObservationIgnored let camera = AttachCamera()
    @ObservationIgnored let frames = AttachFrames()

    /// the camera runs only while a live camera tile or viewfinder can be seen
    func wantsCamera(active: Bool) -> Bool {
        active && isOpen && (page == .menu || (page == .camera && shot == nil))
    }

    func animate(_ duration: Double, _ body: () -> Void) {
        if reduceMotion { body() } else { withAnimation(SetMotion.easeOut(duration), body) }
    }

    // MARK: open, close

    func open() {
        guard !isOpen else { return }
        picks = []; page = .menu; shot = nil
        photos.refresh()
        mounted = true; isOpen = true
        Sound.shared.play(.open, gain: 0.5)
    }

    func close() {
        guard isOpen else { return }
        isOpen = false
        AttachTemp.discard(shot?.url); shot = nil
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.45) { [weak self] in if let self, !self.isOpen { self.mounted = false } }
    }

    func go(_ p: AttachPage) { animate(0.42) { page = p } }

    /// the viewfinder's ✕ goes back to the add menu (a picture just taken is dropped), not out of the whole panel
    func closeCamera() { AttachTemp.discard(shot?.url); shot = nil; go(.menu) }

    /// 文件: the panel goes away and the phone's own document picker opens
    func pickFiles() { close(); importing = true }

    // MARK: photos

    func togglePick(_ id: String) {
        if let k = picks.firstIndex(of: id) { picks.remove(at: k) } else { picks.append(id) }
    }

    /// the grid's 添加 N, or one photo from the rail
    func attachPhotos(_ ids: [String]) {
        guard !ids.isEmpty else { return }
        let rects = ids.map { frames.rects[$0] }
        close()
        Task { @MainActor in
            var list: [AttachItem] = []
            for id in ids {
                let img = await photos.thumbnail(id: id, side: 56, scale: displayScale)
                list.append(AttachItem(kind: .photo, title: "照片", thumbnail: img, assetID: id))
            }
            attach(list, from: rects)
        }
    }

    // MARK: camera

    func shoot() {
        guard shot == nil, !camera.capturing else { return }
        camera.capture { [weak self] s in
            guard let self else { return }
            guard let s, self.isOpen, self.page == .camera else { AttachTemp.discard(s?.url); return }
            withAnimation(.easeOut(duration: 0.2)) { self.shot = s }
        }
    }

    func redo() { AttachTemp.discard(shot?.url); withAnimation(.easeOut(duration: 0.2)) { shot = nil } }

    func useShot() {
        guard let s = shot else { return }
        var from: CGRect?                            // the strip's picture is the centre square of the viewfinder
        if let r = frames.rects["camera"] {
            let side = min(r.width, r.height)
            from = CGRect(x: r.midX - side / 2, y: r.midY - side / 2, width: side, height: side)
        }
        shot = nil                                   // the JPEG now belongs to the item
        close()
        attach([AttachItem(kind: .photo, title: "照片.jpg", thumbnail: s.thumb, url: s.url)], from: [from])
    }

    // MARK: files and pastes

    /// the system picker's answer: copy each file to the temp folder while the picker's access lasts (nothing is read into memory)
    func importFiles(_ result: Result<[URL], Error>) {
        guard case .success(let urls) = result, !urls.isEmpty else { return }
        Task.detached {
            var list: [AttachItem] = []
            for u in urls.prefix(10) {
                let scoped = u.startAccessingSecurityScopedResource()
                defer { if scoped { u.stopAccessingSecurityScopedResource() } }
                let copy = AttachTemp.newFile(u.lastPathComponent)
                if (try? FileManager.default.copyItem(at: u, to: copy)) != nil { list.append(AttachImage.item(copy: copy)) }
            }
            let done = list
            await MainActor.run { [self] in if !done.isEmpty { self.attach(done, from: []) } }
        }
    }

    /// pasted into the field: pictures and files join the strip, words are handed back for the field to take
    func paste(_ providers: [NSItemProvider], insert: @escaping (String) -> Void) {
        Task.detached {
            var list: [AttachItem] = []
            for p in providers.prefix(10) {
                if let it = await Self.load(p) { list.append(it) }
                else if p.canLoadObject(ofClass: NSString.self), let s = await Self.string(p) { await MainActor.run { insert(s) } }
            }
            let done = list
            await MainActor.run { [self] in
                guard !done.isEmpty else { return }
                let r = self.frames.rects["field"].map { CGRect(x: $0.minX + 52, y: $0.maxY - 48, width: 48, height: 48) }   // pictures leave from the field
                self.attach(done, from: done.map { _ in r })
            }
        }
    }

    private static func string(_ p: NSItemProvider) async -> String? {
        await withCheckedContinuation { c in
            _ = p.loadObject(ofClass: NSString.self) { s, _ in c.resume(returning: s as? String) }
        }
    }

    /// a picture or a file (not words, not a web link)
    static func isAttachment(_ p: NSItemProvider) -> Bool {
        p.hasItemConformingToTypeIdentifier(UTType.image.identifier) || p.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier)
            || (!p.hasItemConformingToTypeIdentifier(UTType.text.identifier) && !p.hasItemConformingToTypeIdentifier(UTType.url.identifier) && p.hasItemConformingToTypeIdentifier(UTType.data.identifier))
    }

    /// a picture is a photo (a small copy for the strip), any other file a chip with its name
    private static func load(_ p: NSItemProvider) async -> AttachItem? {
        guard isAttachment(p) else { return nil }
        // a file copied in Files comes as its address: that keeps its own name
        if p.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier),
           let u = await withCheckedContinuation({ (c: CheckedContinuation<URL?, Never>) in _ = p.loadObject(ofClass: URL.self) { u, _ in c.resume(returning: u) } }) {
            let scoped = u.startAccessingSecurityScopedResource()
            defer { if scoped { u.stopAccessingSecurityScopedResource() } }
            let copy = AttachTemp.newFile(u.lastPathComponent)
            if (try? FileManager.default.copyItem(at: u, to: copy)) != nil { return AttachImage.item(copy: copy) }
        }
        let isImage = p.hasItemConformingToTypeIdentifier(UTType.image.identifier)
        let uti = isImage ? UTType.image.identifier : (p.registeredTypeIdentifiers.first { UTType($0)?.conforms(to: .data) == true } ?? UTType.item.identifier)
        let suggested = p.suggestedName
        return await withCheckedContinuation { c in
            _ = p.loadFileRepresentation(forTypeIdentifier: uti) { url, _ in
                guard let url else { return c.resume(returning: nil) }
                var name = url.lastPathComponent
                if let s = suggested, !url.pathExtension.isEmpty { name = (s as NSString).deletingPathExtension + "." + url.pathExtension }   // the name the owner gave it
                let copy = AttachTemp.newFile(name)
                guard (try? FileManager.default.copyItem(at: url, to: copy)) != nil else { return c.resume(returning: nil) }   // the system's copy is gone when this returns
                c.resume(returning: AttachImage.item(copy: copy))
            }
        }
    }

    // MARK: the strip

    /// `rects`: where each one was on screen when it was picked, if it was; those fly into the strip
    func attach(_ list: [AttachItem], from rects: [CGRect?]) {
        for (n, it) in list.enumerated() where !reduceMotion && it.kind == .photo {
            if n < rects.count, let r = rects[n], let img = it.thumbnail {
                flights.append(AttachFlight(id: it.id, image: img, from: r, delay: Double(n) * 0.045))
            }
        }
        animate(0.42) { items += list }
        let ids = Set(list.map(\.id))
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in self?.flights.removeAll { ids.contains($0.id) } }   // never leave one hidden
    }

    func setDest(_ id: UUID, _ r: CGRect) {
        if let i = flights.firstIndex(where: { $0.id == id }), flights[i].dest != r { flights[i].dest = r }
    }
    func flightLanded(_ id: UUID) { flights.removeAll { $0.id == id } }

    func remove(_ id: UUID) {
        guard let k = items.firstIndex(where: { $0.id == id }) else { return }
        AttachTemp.discard(items[k].url)
        flights.removeAll { $0.id == id }
        animate(0.3) { _ = items.remove(at: k) }
    }

    /// what the user sends: returns what is attached and clears it
    @discardableResult
    func take() -> [AttachItem] {
        close()
        let list = items
        guard !list.isEmpty else { return [] }
        leaving = list; flights = []
        animate(0.42) { items = [] }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.45) { [weak self] in self?.leaving = [] }
        return list
    }

    /// the words that stand for the attachments when the user typed none
    static func caption(_ list: [AttachItem]) -> String {
        let p = list.filter { $0.kind == .photo }.count, f = list.count - p
        if p > 0 && f > 0 { return "发了 \(p) 张照片和 \(f) 个文件" }
        if p > 0 { return "发了 \(p) 张照片" }
        return f > 1 ? "发了 \(f) 个文件" : "发了一个文件"
    }
}
