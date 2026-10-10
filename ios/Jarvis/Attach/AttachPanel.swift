import SwiftUI
import Photos

// The glass panel that grows out of the +: a menu, and in the same place a photo grid or a viewfinder (files go out to the system's picker).
// Sizes are the prototype's, in its 440 × 956 design points; the panel is as wide as the composer.

/// the screen steps back while the panel is open; a tap on it puts the panel away and goes no further
struct AttachDim: View {
    let model: AttachModel
    var body: some View {
        Color(red: 3 / 255, green: 4 / 255, blue: 12 / 255).opacity(0.42)
            .frame(width: W + 800, height: H + 800).offset(x: -400, y: -400)
            .contentShape(Rectangle()).onTapGesture { model.close() }
            .opacity(model.isOpen ? 1 : 0).animation(.easeOut(duration: 0.3), value: model.isOpen)
            .allowsHitTesting(model.isOpen)
            .accessibilityHidden(true)
    }
}

/// the panel, the flying copies, the document picker and the camera's on/off
struct AttachOverlay: View {
    @Bindable var model: AttachModel
    let kbLift: Double
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.displayScale) private var displayScale
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        ZStack(alignment: .topLeading) {
            if model.mounted { AttachPanel(model: model, kbLift: kbLift) }
            ForEach(model.flights) { AttachFlightView(flight: $0, model: model) }
        }
        .frame(width: W, height: H, alignment: .topLeading)
        .fileImporter(isPresented: $model.importing, allowedContentTypes: [.item], allowsMultipleSelection: true) { model.importFiles($0) }
        .onChange(of: model.wantsCamera(active: scenePhase == .active)) { _, on in model.camera.setActive(on) }
        .onChange(of: reduceMotion, initial: true) { _, r in model.reduceMotion = r }
        .onChange(of: displayScale, initial: true) { _, s in model.displayScale = s }
        #if DEBUG
        .onAppear { AttachDebug.run(model) }
        #endif
    }
}

/// a picked photo's copy, flying from where it was picked to its place in the strip
private struct AttachFlightView: View {
    let flight: AttachFlight
    let model: AttachModel
    @State private var go = false

    var body: some View {
        let r = go ? (flight.dest ?? flight.from) : flight.from
        Image(uiImage: flight.image).resizable().scaledToFill()
            .frame(width: r.width, height: r.height).clipShape(RoundedRectangle(cornerRadius: go ? 13 : 10))
            .position(x: r.midX, y: r.midY)
            .allowsHitTesting(false)
            .onChange(of: flight.dest) { _, d in if d != nil { start() } }
            .onAppear { if flight.dest != nil { start() } }
    }

    private func start() {
        guard !go else { return }
        let id = flight.id
        withAnimation(SetMotion.easeOut(0.46).delay(flight.delay)) { go = true } completion: { model.flightLanded(id) }
    }
}

// MARK: - the panel

private struct AttachPanel: View {
    let model: AttachModel
    let kbLift: Double
    @State private var appeared = false
    @State private var dragY = 0.0
    @Environment(\.accessibilityReduceMotion) private var reduce

    private var height: Double {
        let target: Double = switch model.page { case .menu: 141; case .grid: 392; case .camera: 320 }
        let avail = (H - 34 - kbLift - 48 - model.rise - 10 - 70).rounded(.down)       // the room above the box, less a margin
        return min(target, max(200, avail))
    }

    var body: some View {
        let h = height, shape = RoundedRectangle(cornerRadius: 24)
        ZStack(alignment: .top) {
            switch model.page {
            case .menu: AttachMenu(model: model).transition(Self.fade)
            case .grid: AttachGrid(model: model).transition(Self.fade)
            case .camera: AttachViewfinder(model: model).transition(Self.fade)
            }
        }
        .frame(width: AT.panelW, height: h, alignment: .top)
        .background(shape.fill(.ultraThinMaterial).overlay(shape.fill(Color(red: 12 / 255, green: 14 / 255, blue: 34 / 255).opacity(0.76))))
        .clipShape(shape)
        .overlay(shape.strokeBorder(LinearGradient(colors: [Color.white.opacity(0.2), Color.white.opacity(0.07)], startPoint: .top, endPoint: .bottom), lineWidth: 0.6))
        .shadow(color: .black.opacity(0.5), radius: 24, y: 16)
        .simultaneousGesture(dismissDrag)
        .scaleEffect(reduce || appeared ? 1 : 0.2, anchor: UnitPoint(x: 24 / AT.panelW, y: (h + 34) / h))     // grown out of the +
        .opacity(appeared ? 1 : 0)
        .offset(x: 20, y: H - 34 - kbLift - 58 - model.rise - h + dragY)
        .allowsHitTesting(model.isOpen)
        .accessibilityElement(children: .contain).accessibilityLabel("添加")
        .onAppear { grow(model.isOpen) }
        .onChange(of: model.isOpen) { _, on in if on { dragY = 0 }; grow(on) }
    }

    private func grow(_ on: Bool) {
        if reduce { withAnimation(.easeOut(duration: 0.2)) { appeared = on } }
        else { withAnimation(SetMotion.easeOut(on ? 0.38 : 0.3)) { appeared = on } }
    }

    private static let fade = AnyTransition.asymmetric(
        insertion: .opacity.animation(.easeOut(duration: 0.26).delay(0.1)), removal: .opacity.animation(.easeOut(duration: 0.16)))

    /// drag it down to put it away
    private var dismissDrag: some Gesture {
        DragGesture(minimumDistance: 7, coordinateSpace: .named("design"))
            .onChanged { v in
                guard model.page != .grid else { return }            // the grid's own scrolling comes first
                let t = v.translation
                if dragY > 0 || (t.height > 0 && t.height > abs(t.width)) { dragY = max(0, t.height) }
            }
            .onEnded { v in
                guard model.page != .grid else { return }
                if dragY > 60 || v.velocity.height > 600 { model.close() }
                else { withAnimation(SetMotion.easeOut(0.45)) { dragY = 0 } }
            }
    }
}

// MARK: menu: the camera and the newest photos, then 相机 · 照片 · 文件 side by side

private struct AttachMenu: View {
    let model: AttachModel

    var body: some View {
        let lib = model.photos
        VStack(spacing: 0) {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    cameraTile
                    if lib.canRead {
                        ForEach(0..<min(8, lib.count), id: \.self) { i in
                            if let a = lib.asset(at: i) { railPhoto(a) }
                        }
                    } else if lib.access == .denied {
                        AttachNote(text: "没有照片权限", compact: true)
                    }
                }.padding(12)
            }
            .frame(height: 88)
            HStack(spacing: 0) {
                option("camera", "相机", out: false, sep: false) { model.go(.camera) }
                option("photo", "照片", out: false, sep: true) { model.go(.grid) }
                option("doc", "文件", out: true, sep: true) { model.pickFiles() }
            }
            .frame(height: 53)
            .overlay(alignment: .top) { Rectangle().fill(SetColor.line).frame(height: 0.5) }
        }
    }

    private var cameraTile: some View {
        Button { model.go(.camera) } label: {
            ZStack {
                AttachFeed(camera: model.camera).scaleEffect(1.14)
                Color(red: 8 / 255, green: 10 / 255, blue: 24 / 255).opacity(0.3)
                Image(systemName: "camera").font(.system(size: 24, weight: .regular)).foregroundStyle(.white).shadow(color: .black.opacity(0.5), radius: 3, y: 1)
            }
            .frame(width: 64, height: 64).clipShape(RoundedRectangle(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.white.opacity(0.14), lineWidth: 0.5))
        }.buttonStyle(PressScale()).accessibilityLabel("相机")
    }

    private func railPhoto(_ a: PHAsset) -> some View {
        let id = a.localIdentifier
        return Button { model.attachPhotos([id]) } label: {
            AttachThumb(lib: model.photos, asset: a, side: 64)
                .frame(width: 64, height: 64).clipShape(RoundedRectangle(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.white.opacity(0.14), lineWidth: 0.5))
        }
        .buttonStyle(PressScale()).accessibilityLabel("添加照片")
        .onGeometryChange(for: CGRect.self) { $0.frame(in: .named("design")) } action: { model.frames.rects[id] = $0 }
        .onDisappear { model.frames.rects[id] = nil }
    }

    private func option(_ symbol: String, _ label: String, out: Bool, sep: Bool, _ act: @escaping () -> Void) -> some View {
        Button(action: act) {
            HStack(spacing: 8) {
                Image(systemName: symbol).font(.system(size: 19, weight: .regular)).foregroundStyle(AT.icon)
                Text(label).font(.system(size: 16)).foregroundStyle(SetColor.ink)
                if out { Image(systemName: "arrow.up.right").font(.system(size: 10, weight: .semibold)).foregroundStyle(SetColor.muted).padding(.leading, -3) }   // leaves the app, to the phone's Files
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity).contentShape(Rectangle())
        }
        .buttonStyle(RowPressStyle())
        .overlay(alignment: .leading) { if sep { Rectangle().fill(SetColor.line).frame(width: 0.5).padding(.vertical, 15) } }
    }
}

/// a small picture from the library, loaded when it scrolls into view and let go when it leaves
private struct AttachThumb: View {
    let lib: AttachPhotoLibrary
    let asset: PHAsset
    let side: CGFloat
    @Environment(\.displayScale) private var scale
    @State private var image: UIImage?
    @State private var req: PHImageRequestID?

    var body: some View {
        AT.tile.aspectRatio(1, contentMode: .fit)
            .overlay { if let image { Image(uiImage: image).resizable().scaledToFill() } }
            .clipped()
            .onAppear { req = lib.request(asset, side: side, scale: scale) { image = $0 } }
            .onDisappear { if let r = req { lib.manager.cancelImageRequest(r) }; req = nil; image = nil }
    }
}

/// a quiet line, and a button to the phone's settings
struct AttachNote: View {
    let text: String
    var compact = false

    var body: some View {
        let layout = compact ? AnyLayout(HStackLayout(spacing: 10)) : AnyLayout(VStackLayout(spacing: 12))
        layout {
            Text(text).font(.system(size: 13)).foregroundStyle(SetColor.muted).multilineTextAlignment(.center)
            Button { AT.openSettings() } label: {
                Text("打开设置").font(.system(size: 13, weight: .semibold)).foregroundStyle(AT.navy).padding(.horizontal, 12).frame(height: 28).background(AT.pale, in: Capsule())
            }.buttonStyle(PressScale())
        }
    }
}

// MARK: the photo grid: four columns, a number on each pick

private struct AttachGrid: View {
    let model: AttachModel

    var body: some View {
        let lib = model.photos
        VStack(spacing: 0) {
            header
            if lib.canRead {
                ScrollView(.vertical, showsIndicators: false) {
                    LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 2), count: 4), spacing: 2) {
                        ForEach(0..<lib.count, id: \.self) { i in
                            if let a = lib.asset(at: i) { AttachTile(model: model, asset: a) }
                        }
                    }
                    VStack(spacing: 8) {
                        Text("\(lib.count) 张照片").font(.system(size: 13)).foregroundStyle(SetColor.muted)
                        if lib.access == .limited { AttachNote(text: "只能看到你允许的照片", compact: true) }
                    }.padding(.top, 14).padding(.bottom, 22)
                }
            } else {
                Spacer(minLength: 0)
                AttachNote(text: lib.access == .undetermined ? "" : "要选照片，需要在设置里允许访问")
                Spacer(minLength: 0)
            }
        }
    }

    private var header: some View {
        let n = model.picks.count
        return ZStack {
            Text("最近项目").font(.system(size: 16, weight: .semibold)).foregroundStyle(SetColor.ink)
            HStack {
                Button { model.close() } label: { Text("取消").font(.system(size: 16)).padding(.horizontal, 10).frame(height: 44).frame(minWidth: 44) }
                    .buttonStyle(AttachHeaderButton())
                Spacer()
                Button { model.attachPhotos(model.picks) } label: {
                    Text(n > 0 ? "添加 \(n)" : "添加").font(.system(size: 16, weight: .semibold)).monospacedDigit()
                        .foregroundStyle(n > 0 ? AT.navy : Color(red: 196 / 255, green: 204 / 255, blue: 238 / 255).opacity(0.42))
                        .padding(.horizontal, 14).frame(height: 32)
                        .background(n > 0 ? AT.pale : Color.white.opacity(0.08), in: Capsule())
                        .contentShape(Capsule().inset(by: -6))
                }.buttonStyle(PressScale()).disabled(n == 0)
                .animation(.easeOut(duration: 0.25), value: n > 0)
            }.padding(.horizontal, 8)
        }.frame(height: 52)
    }
}

private struct AttachHeaderButton: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label.foregroundStyle(SetColor.link.opacity(configuration.isPressed ? 0.5 : 1)).animation(.easeOut(duration: 0.15), value: configuration.isPressed)
    }
}

private struct AttachTile: View {
    let model: AttachModel
    let asset: PHAsset

    var body: some View {
        let id = asset.localIdentifier, k = model.picks.firstIndex(of: id)
        Button { model.togglePick(id) } label: {
            AttachThumb(lib: model.photos, asset: asset, side: 98.5)
                .overlay { Color(red: 8 / 255, green: 10 / 255, blue: 24 / 255).opacity(k != nil ? 0.34 : 0) }
                .overlay { Rectangle().strokeBorder(Color(red: 238 / 255, green: 241 / 255, blue: 1).opacity(0.95), lineWidth: 2.5).opacity(k != nil ? 1 : 0) }
                .overlay(alignment: .topTrailing) {
                    ZStack {
                        Circle().fill(Color.black.opacity(0.14)).overlay(Circle().strokeBorder(Color.white.opacity(0.8), lineWidth: 1.5)).opacity(k == nil ? 1 : 0)
                        Text(k.map { "\($0 + 1)" } ?? "").font(.system(size: 13, weight: .semibold)).monospacedDigit().foregroundStyle(AT.navy)
                            .frame(width: 22, height: 22).background(Circle().fill(.white).shadow(color: .black.opacity(0.4), radius: 2.5, y: 1))
                            .scaleEffect(k != nil ? 1 : 0.5).opacity(k != nil ? 1 : 0)
                    }.frame(width: 22, height: 22).padding(6)
                }
                .animation(model.reduceMotion ? .easeOut(duration: 0.12) : .spring(response: 0.3, dampingFraction: 0.6), value: k != nil)
        }
        .buttonStyle(.plain)
        .accessibilityLabel("照片").accessibilityAddTraits(k != nil ? .isSelected : [])
        .onGeometryChange(for: CGRect.self) { $0.frame(in: .named("design")) } action: { model.frames.rects[id] = $0 }
        .onDisappear { model.frames.rects[id] = nil }
    }
}

// MARK: the viewfinder

/// the live feed, or a calm placeholder where there is no camera
struct AttachFeed: View {
    let camera: AttachCamera
    var body: some View {
        ZStack {
            Color(red: 5 / 255, green: 6 / 255, blue: 13 / 255)
            switch camera.status {
            case .unavailable:
                LinearGradient(colors: [Color(red: 6 / 255, green: 9 / 255, blue: 24 / 255), Color(red: 16 / 255, green: 21 / 255, blue: 58 / 255), Color(red: 29 / 255, green: 18 / 255, blue: 55 / 255)], startPoint: .top, endPoint: .bottom)
                RadialGradient(colors: [Color(red: 157 / 255, green: 180 / 255, blue: 1).opacity(0.22), .clear], center: UnitPoint(x: 0.3, y: 0.35), startRadius: 0, endRadius: 190)
            case .denied: EmptyView()
            case .idle: EmptyView()
            case .running: AttachCameraPreview(camera: camera)   // only once the session runs: attaching the layer to a starting session blocks the main thread
            }
        }
    }
}

private struct AttachViewfinder: View {
    let model: AttachModel
    @State private var flash = 0.0

    var body: some View {
        let cam = model.camera, frozen = model.shot != nil
        ZStack {
            AttachFeed(camera: cam)
            if let s = model.shot { Image(uiImage: s.preview).resizable().scaledToFill().frame(width: AT.panelW, height: 320).clipped() }
            VStack(spacing: 0) {
                LinearGradient(colors: [Color(red: 4 / 255, green: 5 / 255, blue: 14 / 255).opacity(0.5), .clear], startPoint: .top, endPoint: .bottom).frame(height: 88)
                Spacer(minLength: 0)
                LinearGradient(colors: [.clear, Color(red: 4 / 255, green: 5 / 255, blue: 14 / 255).opacity(0.62)], startPoint: .top, endPoint: .bottom).frame(height: 88)
            }.allowsHitTesting(false)
            if cam.status == .denied { AttachNote(text: "没有相机权限") }
            if cam.status == .unavailable && !frozen { Text("模拟器没有相机").font(.system(size: 12)).foregroundStyle(SetColor.muted).frame(maxHeight: .infinity, alignment: .center).offset(y: -58).allowsHitTesting(false) }
            Color.white.opacity(flash).allowsHitTesting(false)
            // top: close, flip
            HStack {
                roundButton("xmark", "关闭相机") { model.close() }
                Spacer()
                roundButton("arrow.triangle.2.circlepath.camera", "切换镜头") { cam.flip() }
                    .opacity(frozen || cam.status == .unavailable ? 0 : 1).allowsHitTesting(!frozen && cam.status != .unavailable)
            }.padding(12).frame(maxHeight: .infinity, alignment: .top)
            // bottom: the shutter, or 重拍 / 使用照片
            ZStack {
                Button { snap() } label: { Color.clear.frame(width: 64, height: 64).contentShape(Circle().inset(by: -8)) }
                    .buttonStyle(ShutterStyle()).accessibilityLabel("拍照")
                    .opacity(frozen ? 0 : 1).allowsHitTesting(!frozen)
                HStack(spacing: 10) {
                    Button { model.redo() } label: { after("重拍", dark: true) }.buttonStyle(PressScale()).frame(width: (AT.panelW - 42) * 0.4)
                    Button { model.useShot() } label: { after("使用照片", dark: false) }.buttonStyle(PressScale()).frame(width: (AT.panelW - 42) * 0.6)
                }.padding(.horizontal, 16).opacity(frozen ? 1 : 0).allowsHitTesting(frozen)
            }.padding(.bottom, 20).frame(maxHeight: .infinity, alignment: .bottom)
        }
        .frame(width: AT.panelW, height: 320).clipped()
        .onGeometryChange(for: CGRect.self) { $0.frame(in: .named("design")) } action: { model.frames.rects["camera"] = $0 }
        .onDisappear { model.frames.rects["camera"] = nil }
    }

    private func snap() {
        guard !model.camera.capturing else { return }
        withAnimation(nil) { flash = 0.9 }
        DispatchQueue.main.async { withAnimation(.easeOut(duration: 0.42)) { flash = 0 } }
        Sound.shared.play(.tick, gain: 0.8)
        model.shoot()
    }

    private func roundButton(_ symbol: String, _ label: String, _ act: @escaping () -> Void) -> some View {
        Button(action: act) {
            Image(systemName: symbol).font(.system(size: 17, weight: .regular)).foregroundStyle(.white)
                .frame(width: 40, height: 40)
                .background(Circle().fill(Color(red: 14 / 255, green: 16 / 255, blue: 34 / 255).opacity(0.5)).background(.ultraThinMaterial, in: Circle()))
                .overlay(Circle().strokeBorder(Color.white.opacity(0.24), lineWidth: 0.5))
                .contentShape(Circle().inset(by: -4))
        }.buttonStyle(PressScale()).accessibilityLabel(label)
    }

    private func after(_ t: String, dark: Bool) -> some View {
        Text(t).font(.system(size: 16, weight: .semibold)).foregroundStyle(dark ? Color.white : AT.navy)
            .frame(maxWidth: .infinity).frame(height: 48)
            .background(dark ? AnyShapeStyle(Color(red: 14 / 255, green: 16 / 255, blue: 34 / 255).opacity(0.5)) : AnyShapeStyle(AT.pale), in: Capsule())
            .overlay(Capsule().strokeBorder(Color.white.opacity(dark ? 0.24 : 0), lineWidth: 0.5))
    }
}

private struct ShutterStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        ZStack {
            Circle().strokeBorder(.white, lineWidth: 3).frame(width: 64, height: 64)
            Circle().fill(.white).frame(width: 52, height: 52).scaleEffect(configuration.isPressed ? 0.88 : 1).animation(SetMotion.easeOut(0.15), value: configuration.isPressed)
            configuration.label
        }
    }
}
