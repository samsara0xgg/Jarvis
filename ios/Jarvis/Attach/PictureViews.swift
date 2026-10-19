import SwiftUI

// His pictures in the talk (ADR 0255): one picture as a rounded image keeping its shape (up to 220 pt wide, 280 pt high), several as a grid of 110 pt squares, a file as its name.
// A tap opens the picture full screen (Attach/PictureViewer). The size is known before the picture is read (its shape, else a square), so reading it moves no row.

enum PictureLayout {
    static let wide = 220.0, high = 280.0, square = 110.0, gap = 4.0

    /// the size of a lone picture: as wide as `wide` allows, no higher than `high` (or `tall`, when a place asks for less), in the picture's own shape; a square while the shape is unknown
    static func single(aspect: Double?, tall: Double = high) -> CGSize {
        guard let a = aspect, a > 0 else { return CGSize(width: 120, height: 120) }
        let w = min(wide, tall * a)
        return CGSize(width: max(88, w), height: min(tall, wide / a))
    }
}

/// The pictures and files of one line of his
struct SentPicturesView: View {
    let items: [SentItem]

    var body: some View {
        let pics = items.filter(\.isImage), files = items.filter { !$0.isImage }
        VStack(alignment: .trailing, spacing: 6) {
            if pics.count == 1 {
                PictureCell(item: pics[0], size: .none, open: { open(pics, 0) })
            } else if pics.count > 1 {
                VStack(alignment: .leading, spacing: PictureLayout.gap) {
                    ForEach(Array(stride(from: 0, to: pics.count, by: 2)), id: \.self) { r in
                        HStack(spacing: PictureLayout.gap) {
                            ForEach(Array(pics[r..<min(r + 2, pics.count)].enumerated()), id: \.element.id) { k, it in
                                PictureCell(item: it, size: PictureLayout.square, open: { open(pics, r + k) })
                            }
                        }
                    }
                }
            }
            if !files.isEmpty {
                HStack(spacing: 8) { ForEach(files.prefix(4)) { FileChip(title: $0.title) } }
            }
        }
    }

    /// the pictures that can be looked at (one the brain has swept has nothing to open unless it is the one tapped and this phone still has it), the viewer opened on the one tapped
    private func open(_ pics: [SentItem], _ at: Int) {
        let store = PictureStore.shared
        let list = pics.filter { !$0.gone || $0.id == pics[at].id }
        guard let i = list.firstIndex(where: { $0.id == pics[at].id }) else { return }
        PictureViewer.present(list.map { ViewerPicture(id: $0.attachment, gone: $0.gone, small: $0.attachment.flatMap { store.peek(id: $0, maxPixel: 0) } ?? $0.image) }, index: i)
    }
}

private struct FileChip: View {
    let title: String
    var body: some View {
        HStack(spacing: 5) {
            Image(systemName: "doc").font(.system(size: 12))
            Text(title).font(.system(size: 12)).lineLimit(1)
        }
        .foregroundStyle(SetColor.ink.opacity(0.85)).padding(.horizontal, 10).frame(height: 28).frame(maxWidth: 150)
        .background(Color.white.opacity(0.1), in: Capsule())
    }
}

/// One picture: the small copy in hand or the one decoded at its size, a dim square while there is none, and a note when the brain has swept it.
/// `size` is the side of a square, or nil for a lone picture in its own shape.
struct PictureCell: View {
    let item: SentItem
    let size: Double?
    var tall = PictureLayout.high           // how high a lone picture may be (the timeline's opened row takes 240)
    let open: () -> Void
    @State private var image: UIImage?
    @State private var outcome: PictureLoad?
    @Environment(\.displayScale) private var scale

    init(item: SentItem, size: Double?, tall: Double = PictureLayout.high, open: @escaping () -> Void) {
        self.item = item; self.size = size; self.tall = tall; self.open = open
        _image = State(initialValue: item.attachment.flatMap { PictureStore.shared.peek(id: $0, maxPixel: Self.pixels(size, tall, 3)) } ?? item.image)
    }

    /// the long side to decode at, for a display scale
    private static func pixels(_ size: Double?, _ tall: Double, _ scale: Double) -> Int { Int(((size ?? tall) * scale).rounded()) }

    private var shape: CGSize {
        if let s = size { return CGSize(width: s, height: s) }
        let a = item.image.map { Double($0.size.width / max(1, $0.size.height)) } ?? item.attachment.flatMap { PictureStore.shared.aspect(id: $0) }
        return PictureLayout.single(aspect: a, tall: tall)
    }

    var body: some View {
        let box = shape, swept: Bool = { if case .gone = outcome { return item.image == nil } else { return false } }()
        ZStack {
            Color.white.opacity(0.07)
            if let image {
                Image(uiImage: image).resizable().scaledToFill().frame(width: box.width, height: box.height).clipped()
            } else if swept {
                VStack(spacing: 5) {
                    Image(systemName: "photo").font(.system(size: 20))
                    Text("图片已过期").font(.system(size: 12))
                }.foregroundStyle(SetColor.muted)
            } else if case .failed = outcome {
                Image(systemName: "photo").font(.system(size: 20)).foregroundStyle(SetColor.muted)
            }
        }
        .frame(width: box.width, height: box.height)
        .clipShape(RoundedRectangle(cornerRadius: size == nil ? 16 : 12))
        .overlay(RoundedRectangle(cornerRadius: size == nil ? 16 : 12).strokeBorder(Color.white.opacity(0.14), lineWidth: 0.5))
        .contentShape(RoundedRectangle(cornerRadius: 12))
        .onTapGesture { if !swept { open() } }
        .accessibilityElement().accessibilityLabel(swept ? "图片已过期" : "图片").accessibilityAddTraits(swept ? [] : .isButton)
        .task(id: item.attachment ?? item.id) {
            guard let id = item.attachment else { return }
            let px = Self.pixels(size, tall, scale)
            if let hit = PictureStore.shared.peek(id: id, maxPixel: px, exact: true) { image = hit; return }
            let got = await PictureStore.shared.image(id: id, gone: item.gone, maxPixel: px)
            outcome = got
            if case .image(let img) = got { image = img }
        }
    }
}
