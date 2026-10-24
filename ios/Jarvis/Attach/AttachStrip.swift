// words: legacy
import SwiftUI
import UniformTypeIdentifiers

// The + at the left of the composer, and the strip of what is attached above the words.

enum AT {
    static let pale = Color(red: 238 / 255, green: 241 / 255, blue: 1)                  // #eef1ff
    static let navy = Color(red: 11 / 255, green: 13 / 255, blue: 26 / 255)             // #0b0d1a
    static let tile = Color(red: 16 / 255, green: 20 / 255, blue: 42 / 255)             // #10142a
    static let icon = Color(red: 214 / 255, green: 220 / 255, blue: 246 / 255).opacity(0.82)
    static let panelW = W - 40                                                          // as wide as the composer
    static func mono(_ size: CGFloat) -> Font {
        Look.figures(size, weight: .medium)
    }
    static func openSettings() {
        if let u = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(u) }
    }
}

struct AttachPlusButton: View {
    let model: AttachModel
    var body: some View {
        Button { if model.isOpen { model.close() } else { model.open() } } label: {
            Image(systemName: "plus").font(.system(size: 19, weight: .regular))
                .rotationEffect(.degrees(model.isOpen ? 45 : 0))
                .frame(width: 36, height: 36)
                .background(Circle().fill(Color.white.opacity(model.isOpen ? 0.18 : 0.1)))
                .foregroundStyle(model.isOpen ? Color.white : Color(red: 226 / 255, green: 230 / 255, blue: 250 / 255).opacity(0.88))
                .contentShape(Circle().inset(by: -5))
        }
        .buttonStyle(PressScale())
        .animation(model.reduceMotion ? nil : SetMotion.easeOut(0.38), value: model.isOpen)
        .accessibilityLabel("添加照片或文件")
    }
}

/// what is attached, in a row inside the composer's box (72 tall with its margins)
struct AttachStrip: View {
    let model: AttachModel

    var body: some View {
        let list = model.hasItems ? model.items : model.leaving
        if !list.isEmpty {
            ScrollViewReader { proxy in
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 8) {
                        ForEach(list) { it in
                            AttachChip(item: it, hidden: model.flights.contains { $0.id == it.id }) { model.remove(it.id) }
                                .id(it.id)
                                .onGeometryChange(for: CGRect.self) { $0.frame(in: .named("design")) } action: { model.setDest(it.id, $0) }
                                .transition(.scale(scale: 0.9).combined(with: .opacity))
                        }
                    }
                }
                .onChange(of: list.count) { _, _ in if let last = list.last { proxy.scrollTo(last.id, anchor: .trailing) } }
            }
            .frame(height: 56).padding(.top, 10).padding(.bottom, 6).padding(.horizontal, 10)
            .opacity(model.hasItems ? 1 : 0)
            .animation(model.hasItems ? .easeOut(duration: 0.3).delay(0.08) : .easeOut(duration: 0.16), value: model.hasItems)
        }
    }
}

struct AttachChip: View {
    let item: AttachItem
    let hidden: Bool                      // its copy is still flying in
    let remove: () -> Void

    var body: some View {
        Group { if item.kind == .photo { photo } else { file } }
            .opacity(hidden ? 0 : 1)
            .overlay(alignment: .topTrailing) {
                Button(action: remove) {
                    ZStack {
                        Circle().fill(Color(red: 10 / 255, green: 12 / 255, blue: 28 / 255).opacity(0.62))
                        Circle().strokeBorder(Color.white.opacity(0.22), lineWidth: 0.5)
                        Image(systemName: "xmark").font(.system(size: 9, weight: .heavy))
                    }
                    .frame(width: 20, height: 20).foregroundStyle(.white)
                    .frame(width: 28, height: 28).contentShape(Rectangle())
                }
                .buttonStyle(.plain).offset(x: 1, y: -1).opacity(hidden ? 0 : 1)
                .accessibilityLabel("移除 \(item.title)")
            }
    }

    private var photo: some View {
        ZStack {
            AT.tile
            if let t = item.thumbnail { Image(uiImage: t).resizable().scaledToFill() } else { Image(systemName: "photo").foregroundStyle(SetColor.muted) }
        }
        .frame(width: 56, height: 56).clipShape(RoundedRectangle(cornerRadius: 13))
        .overlay(RoundedRectangle(cornerRadius: 13).strokeBorder(Color.white.opacity(0.16), lineWidth: 0.5))
    }

    private var file: some View {
        HStack(spacing: 8) {
            AttachGlyph(ext: item.ext)
            VStack(alignment: .leading, spacing: 1) {
                Text(item.stem).font(.system(size: 13, weight: .medium)).foregroundStyle(SetColor.ink).lineLimit(1).frame(maxWidth: 72, alignment: .leading)
                Text(item.ext.isEmpty ? "文件" : String(item.ext.prefix(5)).uppercased()).font(AT.mono(10.5)).tracking(0.42).foregroundStyle(SetColor.muted).lineLimit(1)
            }
        }
        .padding(.leading, 10).padding(.trailing, 28).frame(height: 56).frame(maxWidth: 152)
        .background(Color.white.opacity(0.09)).clipShape(RoundedRectangle(cornerRadius: 13))
        .overlay(RoundedRectangle(cornerRadius: 13).strokeBorder(Color.white.opacity(0.16), lineWidth: 0.5))
    }
}

/// the type of a file as a small coloured glyph
struct AttachGlyph: View {
    let ext: String
    var body: some View {
        let g = Self.style(ext)
        Image(systemName: g.symbol).font(.system(size: 16, weight: .regular))
            .foregroundStyle(g.fg).frame(width: 32, height: 32)
            .background(g.bg, in: RoundedRectangle(cornerRadius: 9))
    }

    private static func style(_ ext: String) -> (symbol: String, fg: Color, bg: Color) {
        func c(_ r: Double, _ g: Double, _ b: Double, _ a: Double = 1) -> Color { Color(red: r / 255, green: g / 255, blue: b / 255, opacity: a) }
        let t = UTType(filenameExtension: ext)
        let plain = (c(207, 216, 255), c(157, 180, 255, 0.16))
        if ext == "pdf" { return ("doc.richtext", c(255, 184, 168), c(255, 124, 100, 0.16)) }
        if t?.conforms(to: .image) == true { return ("photo", c(169, 212, 255), c(108, 170, 255, 0.16)) }
        if ext == "md" || t?.conforms(to: .text) == true { return ("doc.text", c(200, 240, 216), c(111, 224, 180, 0.14)) }
        if t?.conforms(to: .audio) == true { return ("waveform", plain.0, plain.1) }
        if t?.conforms(to: .movie) == true { return ("film", plain.0, plain.1) }
        if t?.conforms(to: .archive) == true { return ("doc.zipper", plain.0, plain.1) }
        return ("doc", plain.0, plain.1)
    }
}
