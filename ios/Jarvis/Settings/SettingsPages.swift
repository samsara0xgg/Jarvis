// words: legacy
import SwiftUI

enum SettingsPage: Hashable {
    case main, pair, log

    var title: String {
        switch self {
        case .main: "设置"
        case .pair: "重新配对"
        case .log: "手机报给大脑的记录"
        }
    }
}

/// Position 0...1 of a CSS `animation-direction: alternate` loop at `date`, shaped by `curve`.
private func alternate(_ date: Date, period: Double, curve: UnitCurve) -> Double {
    let u = (date.timeIntervalSinceReferenceDate / period).truncatingRemainder(dividingBy: 2)
    return curve.value(at: u < 1 ? u : 2 - u)
}

// MARK: - Main list

struct SettingsMainPage: View {
    @Bindable var model: SettingsModel
    /// False while another page covers this one: the avatar then stops breathing.
    let isCurrent: Bool
    var tuneNear: () -> Void = {}
    let push: (SettingsPage) -> Void
    /// The home-screen icon: the glass one (primary, AppIcon) or the flat one (the alternate AppIconFlat, project.yml).
    @State private var flatIcon = UIApplication.shared.alternateIconName == "AppIconFlat"

    /// iOS confirms the change with its own alert; if it refuses, the switch goes back.
    private func pickIcon(flat: Bool) {
        guard flat != flatIcon else { return }
        flatIcon = flat
        Task {
            do { try await UIApplication.shared.setAlternateIconName(flat ? "AppIconFlat" : nil) } catch { flatIcon = !flat }
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            SetGroup { hero }
            SetSection(header: "首页") {
                SegmentedRow(title: "中间显示", options: ["星盘", "时间线"],
                             selection: Binding(get: { HomeModule.shared.isTimeline ? 1 : 0 },
                                                set: { HomeModule.shared.kind = $0 == 1 ? .timeline : .stars }))
                SegmentedRow(title: "App 图标", options: ["玻璃", "平面"],
                             selection: Binding(get: { flatIcon ? 1 : 0 }, set: { pickIcon(flat: $0 == 1) }))
            }
            SetSection(header: "大脑", footer: model.showsConversationModel ? "下一句起生效" : nil) {
                InfoRow(label: "连接的大脑", detail: model.brainName)
                InfoRow(label: "延迟", detail: model.latency)
                if model.showsConversationModel, let current = model.conversationModel, let effort = model.conversationEffort {
                    MenuRow(title: "对话模型", options: model.modelOptions.map { ($0, SettingsModel.modelNames[$0] ?? $0) }, selection: current) { id in
                        Task { await model.saveConversation(model: id) }
                    }
                    if model.effortChoices.count > 1 {
                        SegmentedRow(title: "思考力度", options: model.effortChoices.map { SettingsModel.effortNames[$0] ?? $0 },
                                     selection: Binding(get: { model.effortChoices.firstIndex(of: effort) ?? 0 },
                                                        set: { i in Task { await model.saveConversation(effort: model.effortChoices[i]) } }))
                    }
                }
                NavRow(label: "重新配对") { push(.pair) }
            }
            SetSection(header: "和她说话", footer: "「App 打开时喊 Jarvis 唤醒」开着时，App 在前台会一直听，比较费电。") {
                InfoRow(label: "嘿 Siri，叫 Jarvis", detail: model.siri)
                SwitchRow(title: "操作按钮叫她", isOn: $model.actionButton)
                SwitchRow(title: "App 打开时喊 Jarvis 唤醒", isOn: $model.wakeInApp)
                SliderRow(title: "说话速度", value: $model.speechRate) { Text("慢") } hi: { Text("快") }
                SliderRow(title: "音量", value: $model.volume) {
                    Image(systemName: "speaker.fill")
                } hi: {
                    Image(systemName: "speaker.wave.3.fill")
                }
                SegmentedRow(title: "回答语言", options: SettingsModel.AnswerLanguage.allCases.map(\.title),
                             selection: Binding(get: { model.language.rawValue },
                                                set: { model.language = .init(rawValue: $0) ?? .followMe }))
            }
            if DeviceRole.senses {          // an iPad senses and records nothing (ADR 0336): no switches for it
                SetSection(header: "手机感知", footer: "这些只发给你自己的大脑，随时可以关。") {
                    ForEach($model.senses) { $s in SwitchRow(title: s.title, detail: s.detail, isOn: $s.isOn) }
                }
                SetSection(header: "随身录音", footer: "只在 Wi-Fi 下上传。") {
                    SwitchRow(title: "随身录音", isOn: $model.lifelogOn)
                    TimelineView(.periodic(from: .now, by: 30)) { t in
                        VStack(alignment: .leading, spacing: 3) {
                            Text(model.lifelogLine(now: t.date))
                            if !model.lifelogReason.isEmpty { Text(model.lifelogReason) }
                        }
                        .font(.subheadline)
                        .foregroundStyle(LifelogUploader.shared.needsRepair ? SetColor.faint : SetColor.muted)
                        .frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 16).padding(.bottom, 11)
                    }
                }
            }
            SetSection(header: "通知") {
                ForEach($model.notices) { $s in SwitchRow(title: s.title, detail: s.detail, isOn: $s.isOn) }
                NavRow(label: "免打扰时段", detail: model.quietHours)
            }
            SetSection(header: "声音") {
                SwitchRow(title: "声音效果", isOn: $model.soundOn)
            }
            SetSection(header: "数据") {
                NavRow(label: "查看手机报给大脑的记录") { push(.log) }
                DeleteRow(count: model.todayCount, onDelete: model.deleteToday)
            }
            if !HomeModule.shared.isTimeline { SetSection { NavRow(label: "调整近景视角", action: tuneNear) } }       // the near view is the dial's
            Text(model.version).font(.footnote).foregroundStyle(SetColor.muted).padding(.top, 32)
        }
        .padding(.top, 14)
        .padding(.bottom, 56)
        .onChange(of: isCurrent, initial: true) { _, now in if now { BrainLink.shared.ping(); LifelogUploader.shared.refresh(); Task { await model.refreshConversation() } } }
    }

    private var hero: some View {
        HStack(spacing: 14) {
            Orb(animate: isCurrent)
            VStack(alignment: .leading, spacing: 2) {
                Text("Jarvis").font(.system(size: 20, weight: .semibold))
                HStack(spacing: 6) {
                    Circle().fill(model.brainOnline ? SetColor.mint : SetColor.faint).frame(width: 6, height: 6)
                        .shadow(color: SetColor.mint.opacity(model.brainOnline ? 0.8 : 0), radius: 3)
                    Text(model.statusLine).font(.subheadline).foregroundStyle(SetColor.muted)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(16)
        .accessibilityElement(children: .combine)
    }
}

/// 44 pt pearl with a breathing halo.
private struct Orb: View {
    let animate: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        TimelineView(.animation(paused: !animate || reduceMotion)) { ctx in
            let u = reduceMotion ? 1 : alternate(ctx.date, period: 4.4, curve: .easeInOut)
            ZStack {
                Circle()
                    .fill(RadialGradient(colors: [SetColor.glow.opacity(0.38), SetColor.glow.opacity(0)],
                                         center: .center, startRadius: 0, endRadius: 32))
                    .frame(width: 64, height: 64)
                    .opacity(reduceMotion ? 1 : 0.45 + 0.55 * u)
                    .scaleEffect(reduceMotion ? 1 : 0.92 + 0.14 * u)
                pearl
            }
        }
        .frame(width: 44, height: 44)
    }

    private var pearl: some View {
        Circle()
            .fill(RadialGradient(stops: [
                .init(color: .white, location: 0), .init(color: .white, location: 0.05),
                .init(color: SetColor.color(234, 241, 255), location: 0.22),
                .init(color: SetColor.color(180, 198, 255), location: 0.54),
                .init(color: SetColor.color(127, 141, 240), location: 0.84),
                .init(color: SetColor.color(106, 111, 224), location: 1),
            ], center: UnitPoint(x: 0.38, y: 0.32), startRadius: 0, endRadius: 40.5))
            .overlay {  // the inset shadows: a dark crescent below, a bright rim above
                ZStack {
                    Circle().strokeBorder(SetColor.color(76, 80, 200, 0.4), lineWidth: 10).blur(radius: 5).offset(y: -5)
                    Circle().strokeBorder(.white.opacity(0.8), lineWidth: 1).blur(radius: 0.5).offset(y: 1)
                }
                .clipShape(Circle())
            }
            .frame(width: 44, height: 44)
            .shadow(color: SetColor.glow.opacity(0.5), radius: 7)
    }
}

// MARK: - 重新配对

struct SettingsPairPage: View {
    /// The camera and the scan line only run while this page is on screen.
    let isCurrent: Bool
    let done: () -> Void

    private enum Phase: Equatable { case scan, claiming, paired(String), failed(String) }
    @State private var scanner = QRScanner()
    @State private var phase = Phase.scan
    private let link = BrainLink.shared

    var body: some View {
        VStack(spacing: 0) {
            Viewfinder(animate: isCurrent && phase == .scan, scanner: scanner, dim: phase != .scan).padding(.top, 28)
            caption
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 14)
        .padding(.bottom, 56)
        .onChange(of: isCurrent, initial: true) { _, now in if now { begin() } else { scanner.stop() } }
        .onChange(of: link.state) { _, s in
            guard phase == .claiming else { return }
            if case .paired(let h) = s { phase = .paired(h) } else if case .error(let m) = s { phase = .failed(m) }
        }
        .onDisappear { scanner.stop() }
    }

    private func begin() {
        phase = .scan
        scanner.onCode = { text in
            guard phase == .scan else { return }
            phase = .claiming
            scanner.stop()
            link.pair(scanned: text)
        }
        scanner.start()
        #if DEBUG
        // `-fakeScan '<what the QR holds>'` hands the page a scan after a moment, for the simulator
        let a = ProcessInfo.processInfo.arguments
        if let i = a.firstIndex(of: "-fakeScan"), i + 1 < a.count {
            let text = a[i + 1]
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { scanner.onCode?(text) }
        }
        #endif
    }

    @ViewBuilder private var caption: some View {
        switch phase {
        case .scan:
            switch scanner.availability {
            case .noCamera: note("这台设备没有摄像头", "在 iPhone 上，对准 Mac 设置里「手机」页上的二维码")
            case .denied:
                note("没有相机权限", "到「设置」里允许 Jarvis 使用相机，才能扫二维码")
                action("打开设置") { if let u = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(u) } }
            default: note("对准 Mac 设置里「手机」页上的二维码", "手机和 Mac 要在同一个 Tailscale 里")
            }
        case .claiming: note("正在配对…", nil)
        case .paired(let h):
            note("已配对", "连着 \(h)")
            action("完成", done)
        case .failed(let m):
            note(m, "在 Mac 上重新显示二维码，再扫一次")
            action("再扫一次") { begin() }
        }
    }

    private func note(_ title: String, _ sub: String?) -> some View {
        VStack(spacing: 8) {
            Text(title).font(.body).multilineTextAlignment(.center)
            if let sub { Text(sub).font(.subheadline).foregroundStyle(SetColor.muted).multilineTextAlignment(.center) }
        }
        .padding(.horizontal, 40).padding(.top, 28)
    }

    private func action(_ title: String, _ run: @escaping () -> Void) -> some View {
        Button(action: run) { Text(title).font(.body.weight(.semibold)).padding(.horizontal, 20).frame(height: 44).contentShape(Rectangle()) }
            .foregroundStyle(SetColor.link)
            .padding(.top, 12)
    }
}

/// A 260 pt viewfinder looking for the Mac's code: the camera's picture, corner brackets and a scan line sweeping back and forth.
/// Without a camera (the simulator) or without permission it is only the dotted night and the brackets.
private struct Viewfinder: View {
    let animate: Bool
    let scanner: QRScanner
    let dim: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private let side = 260.0
    private var box: RoundedRectangle { RoundedRectangle(cornerRadius: 18) }
    private var live: Bool { scanner.availability == .ready }
    private var blind: Bool { scanner.availability == .noCamera || scanner.availability == .denied }

    var body: some View {
        ZStack(alignment: .topLeading) {
            backdrop
            if live { CameraPreview(session: scanner.session).frame(width: side, height: side).opacity(dim ? 0.25 : 1).transition(.opacity) }
            if blind {
                Image(systemName: "camera.slash").font(.system(size: 28, weight: .light)).foregroundStyle(SetColor.faint)
                    .frame(width: side, height: side)
            }
            Brackets().stroke(SetColor.ink.opacity(0.92), style: StrokeStyle(lineWidth: 3.5, lineCap: .round, lineJoin: .round))
            if !blind {
                TimelineView(.animation(paused: !animate || reduceMotion)) { ctx in
                    let y = reduceMotion ? 0 : 154 * alternate(ctx.date, period: 2.6, curve: .bezier(startControlPoint: .init(x: 0.45, y: 0), endControlPoint: .init(x: 0.55, y: 1)))
                    scanLine.offset(y: 52 + y)
                }
            }
        }
        .animation(.easeOut(duration: 0.3), value: live)
        .frame(width: side, height: side)
        .clipShape(box)
        .overlay { box.strokeBorder(.white.opacity(0.07), lineWidth: 1) }
        .overlay { box.inset(by: -0.25).stroke(.black.opacity(0.6), lineWidth: 0.5) }
        .shadow(color: .black.opacity(0.6), radius: 14, y: 14)
        .accessibilityHidden(true)
    }

    /// Dot grid over a night-blue radial wash.
    private var backdrop: some View {
        ZStack {
            Rectangle()
                .fill(RadialGradient(stops: [
                    .init(color: SetColor.color(21, 26, 51), location: 0),
                    .init(color: SetColor.color(10, 12, 27), location: 0.56),
                    .init(color: SetColor.color(5, 6, 13), location: 1),
                ], center: .center, startRadius: 0, endRadius: 1.3 * side))
                .frame(width: 3 * side, height: 3 * side)
                .scaleEffect(y: 1.1 / 1.3, anchor: .center)  // 130% x 110% ellipse
                .offset(y: (0.38 - 0.5) * side)
            Canvas { ctx, size in
                var y = 6.5
                while y < size.height {
                    var x = 6.5
                    while x < size.width {
                        ctx.fill(Path(ellipseIn: CGRect(x: x - 1.2, y: y - 1.2, width: 2.4, height: 2.4)), with: .color(.white.opacity(0.06)))
                        x += 13
                    }
                    y += 13
                }
            }
        }
        .frame(width: side, height: side)
    }

    private var scanLine: some View {
        let clear = SetColor.link.opacity(0)
        return ZStack(alignment: .bottom) {
            Rectangle()
                .fill(LinearGradient(colors: [SetColor.glow.opacity(0), SetColor.glow.opacity(0.2)], startPoint: .top, endPoint: .bottom))
                .frame(height: 46)
                .mask(LinearGradient(stops: [.init(color: .clear, location: 0), .init(color: .black, location: 0.3),
                                             .init(color: .black, location: 0.7), .init(color: .clear, location: 1)],
                                     startPoint: .leading, endPoint: .trailing))
                .offset(y: -2)
            RoundedRectangle(cornerRadius: 1)
                .fill(LinearGradient(stops: [.init(color: clear, location: 0), .init(color: SetColor.link, location: 0.22),
                                             .init(color: .white, location: 0.5), .init(color: SetColor.link, location: 0.78),
                                             .init(color: clear, location: 1)],
                                     startPoint: .leading, endPoint: .trailing))
                .frame(height: 2)
                .shadow(color: SetColor.glow.opacity(0.6), radius: 7)
        }
        .frame(width: side - 92, height: 2, alignment: .bottom)
        .padding(.leading, 46)
    }
}

/// The four corner brackets, in the 260 x 260 space of the prototype's SVG.
private struct Brackets: Shape {
    func path(in rect: CGRect) -> Path {
        let k = rect.width / 260
        func pt(_ x: Double, _ y: Double) -> CGPoint { CGPoint(x: rect.minX + x * k, y: rect.minY + y * k) }
        var p = Path()
        for (a, corner, b) in [((52.0, 92.0), (52.0, 52.0), (92.0, 52.0)), ((168.0, 52.0), (208.0, 52.0), (208.0, 92.0)),
                               ((208.0, 168.0), (208.0, 208.0), (168.0, 208.0)), ((92.0, 208.0), (52.0, 208.0), (52.0, 168.0))] {
            p.move(to: pt(a.0, a.1))
            p.addArc(tangent1End: pt(corner.0, corner.1), tangent2End: pt(b.0, b.1), radius: 16 * k)
            p.addLine(to: pt(b.0, b.1))
        }
        return p
    }
}

// MARK: - 手机报给大脑的记录

struct SettingsLogPage: View {
    let model: SettingsModel

    var body: some View {
        SetSection(header: "今天", footer: "今天一共 \(model.todayCount) 条，这里只列最新的 \(model.records.count) 条。") {
            ForEach(model.records) { r in
                HStack(spacing: 12) {
                    Text(r.time).font(.system(size: 14, design: .monospaced)).foregroundStyle(SetColor.muted)
                        .frame(width: 48, alignment: .leading)
                    Text("\(Text("\(r.kind) ·").foregroundStyle(SetColor.muted)) \(r.value)").font(.body)
                    Spacer(minLength: 0)
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 11)
                .frame(minHeight: 44)
                .accessibilityElement(children: .combine)
            }
        }
        .padding(.top, 14)
        .padding(.bottom, 56)
    }
}
