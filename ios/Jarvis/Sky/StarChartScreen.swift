import SwiftUI
import UIKit

// Everything on the star chart that is text or a button, in the prototype's 440 × 956 design points (scaled to the screen).

private let ink = Color(red: 238 / 255, green: 240 / 255, blue: 251 / 255)
private let muted = Color(red: 196 / 255, green: 204 / 255, blue: 238 / 255).opacity(0.58)
private let navy = Color(red: 11 / 255, green: 13 / 255, blue: 26 / 255)
private let pale = Color(red: 238 / 255, green: 241 / 255, blue: 1)
private let amber = Color(red: 1, green: 217 / 255, blue: 176 / 255)

private func serif(_ size: CGFloat) -> Font {
    UIFont(name: "Instrument Serif", size: size) != nil ? .custom("Instrument Serif", size: size) : .system(size: size, design: .serif)
}
private func monoF(_ size: CGFloat, _ w: Font.Weight = .medium) -> Font {
    let face = w == .regular ? "IBMPlexMono-Regular" : "IBMPlexMono-Medium"
    return UIFont(name: face, size: size) != nil ? .custom(face, size: size) : .system(size: size, weight: w, design: .monospaced)
}

struct StarChartScreen: View {
    @State private var engine = StarEngine()

    var body: some View {
        GeometryReader { geo in
            let k = max(0.5, min(geo.size.width / W, geo.size.height / (H - 40)))
            let ox = (geo.size.width - W * k) / 2, oy = (geo.size.height - H * k) / 2
            ZStack(alignment: .topLeading) {
                StageRepresentable(engine: engine).ignoresSafeArea()
                StarChrome(engine: engine, ui: engine.ui, k: k)
                    .frame(width: W, height: H)
                    .coordinateSpace(name: "design")
                    .scaleEffect(k, anchor: .topLeading)
                    .offset(x: ox, y: oy)
                    .frame(width: W * k, height: H * k, alignment: .topLeading)
                    .offset(x: ox, y: oy)
            }
            .frame(width: geo.size.width, height: geo.size.height, alignment: .topLeading)
        }
        .ignoresSafeArea()
        .background(Color(red: 5 / 255, green: 6 / 255, blue: 13 / 255))
        .preferredColorScheme(.dark)
        .statusBarHidden(false)
    }
}

// MARK: - the chrome

struct StarChrome: View {
    let engine: StarEngine
    @Bindable var ui: StarUI
    let k: Double
    @State private var focused = false                 // the field's keyboard (Attach/AttachField)
    @State private var sheetY: Double? = nil           // while a finger holds the drawer
    private let SHUT = 880.0

    var body: some View {
        ZStack(alignment: .topLeading) {
            topRow
            detail
                .opacity(ui.drawerOpen ? 0 : 1)
                .animation(.easeOut(duration: 0.25), value: ui.drawerOpen)
            grabber
            caption
            voiceNote
            drawer
            AttachDim(model: ui.attach)
            composer
            AttachOverlay(model: ui.attach, kbLift: ui.kbLift)
        }
        .frame(width: W, height: H, alignment: .topLeading)
        .onChange(of: focused) { _, f in ui.typing = f }
        .onAppear {
            engine.endEditing = { focused = false }
            engine.watchVoice()
            if CardsRequest.pending { openCards() }
            #if DEBUG
            if let sc = UserDefaults.standard.string(forKey: "scene") {
                let delay = UserDefaults.standard.double(forKey: "sceneDelay")
                DispatchQueue.main.asyncAfter(deadline: .now() + (delay > 0 ? delay : 1.5)) { engine.runScene(sc) }
            }
            if let vs = UserDefaults.standard.string(forKey: "voiceScene") {
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { engine.runVoiceScene(vs) }
            }
            #endif
        }
        .onReceive(NotificationCenter.default.publisher(for: CardsRequest.notification)) { _ in openCards() }
        .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillChangeFrameNotification)) { n in
            guard let f = (n.userInfo?[UIResponder.keyboardFrameEndUserInfoKey] as? CGRect) else { return }
            let screenH = UIScreen.main.bounds.height
            let lift = max(0, screenH - f.minY) / k
            withAnimation(.easeOut(duration: 0.3)) { ui.kbLift = lift }
        }
        .sheet(isPresented: $ui.settingsOpen) { SettingsSheet() }
    }

    /// a tap on a notification that waits on him: the drawer with today's cards
    private func openCards() {
        CardsRequest.pending = false
        engine.setDrawer(true)
    }

    // MARK: the top row: the day you are looking at, the scale, settings

    private var topRow: some View {
        HStack(spacing: 8) {
            Text(ui.dateText).font(.system(size: 15, weight: .semibold)).foregroundStyle(ink).lineLimit(1)
            Spacer(minLength: 0)
            SegControl(level: ui.level) { engine.setLevel(Double($0)) }
            Button { ui.settingsOpen = true } label: {
                Image(systemName: "gearshape").font(.system(size: 18, weight: .regular)).foregroundStyle(Color(red: 214 / 255, green: 220 / 255, blue: 246 / 255).opacity(0.8))
                    .frame(width: 36, height: 36).glass(Circle())
            }.buttonStyle(PressScale())
            .accessibilityLabel("设置")
        }
        .padding(.horizontal, 30)
        .frame(width: W, height: 38)
        .offset(y: 60)
    }

    // MARK: the panel under the dial

    private var detail: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .bottom, spacing: 12) {
                Text(ui.hm).font(serif(ui.view == "day" ? 80 : 68)).foregroundStyle(ink).lineLimit(1).fixedSize()
                    .contentTransition(.identity).monospacedDigit()
                    .padding(.bottom, -6)
                if let t = ui.tagText {
                    Text(t).font(.system(size: 12, weight: .semibold)).foregroundStyle(navy).padding(.horizontal, 9).frame(height: 22).background(pale, in: Capsule()).padding(.bottom, 9)
                }
                Button { engine.goBack() } label: {
                    HStack(spacing: 6) {
                        Circle().fill(Color(red: 1, green: 243 / 255, blue: 214 / 255)).frame(width: 6, height: 6).shadow(color: Color(red: 1, green: 228 / 255, blue: 170 / 255).opacity(0.7), radius: 4)
                        Text("回到现在").font(.system(size: 12.5, weight: .semibold)).foregroundStyle(ink)
                    }.padding(.leading, 10).padding(.trailing, 12).frame(height: 26).background(Color.white.opacity(0.1), in: Capsule())
                }.buttonStyle(PressScale())
                .padding(.bottom, 7)
                .opacity(ui.showBack ? 1 : 0).scaleEffect(ui.showBack ? 1 : 0.92).offset(x: ui.showBack ? 0 : -10)
                .allowsHitTesting(ui.showBack)
                .animation(.spring(response: 0.4, dampingFraction: 0.78), value: ui.showBack)
            }
            .frame(height: 70, alignment: .bottom)
            .opacity(ui.kbLift > 80 ? 0 : 1)
            .contentShape(Rectangle())
            .gesture(drawerPull(opens: true, tap: false))
            Text(ui.dayText).font(monoF(12)).tracking(1.2).foregroundStyle(muted).lineLimit(1).padding(.top, 12)
                .opacity(ui.kbLift > 80 ? 0 : 1)
            Group {
                if ui.showSum { sum } else { rowList(ui.rows, hint: ui.emptyHint) }
            }
            .opacity(ui.talking || ui.typing ? 0 : ui.detFast ? 0.4 : 1).offset(y: ui.talking || ui.typing ? 8 : 0)
            .allowsHitTesting(!(ui.talking || ui.typing))
            .animation(.easeOut(duration: 0.35), value: ui.talking || ui.typing)
            .animation(.easeOut(duration: ui.detFast ? 0.2 : 0.35), value: ui.detFast)
        }
        .padding(.horizontal, 30)
        .frame(width: W, alignment: .topLeading)
        .id(ui.flip)
        .transition(.opacity.combined(with: .offset(y: 7)))
        .animation(.easeOut(duration: 0.42), value: ui.flip)
        .offset(y: 532)
    }

    private var sum: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(ui.sumMain).font(.system(size: 17)).lineSpacing(17 * 0.5 - 6).foregroundStyle(Color(red: 232 / 255, green: 236 / 255, blue: 252 / 255).opacity(0.9))
            if let st = ui.sumStat { Text(st).font(monoF(12.5)).tracking(0.25).foregroundStyle(Color(red: 1, green: 228 / 255, blue: 196 / 255).opacity(0.72)).padding(.top, 10) }
            FlowChips(chips: ui.chips).padding(.top, 16)
        }.padding(.top, 16)
    }

    private func rowList(_ rows: [RowModel], hint: String?) -> some View {
        VStack(spacing: 0) {
            if let h = hint {
                HStack { Color.clear.frame(width: 22); Text(h).font(.system(size: 12.5)).foregroundStyle(muted).lineLimit(1); Spacer() }
                    .frame(minHeight: 56).overlay(alignment: .top) { Rectangle().fill(hairline).frame(height: 0.5) }
            }
            ForEach(Array(rows.enumerated()), id: \.element.id) { i, r in
                RowView(row: r, engine: engine)
                    .transition(ui.rowsCalm ? .opacity.combined(with: .offset(y: 8)) : .identity)
            }
        }
        .padding(.top, 16)
        .environment(\.cardCompact, true)
        .id(ui.rowsKey)
        .animation(ui.rowsCalm ? .easeOut(duration: 0.5) : nil, value: ui.rowsKey)
    }

    // MARK: the drawer: every card of today, pulled up over the dial

    private var grabber: some View {
        Capsule().fill(Color(red: 214 / 255, green: 220 / 255, blue: 246 / 255).opacity(0.28)).frame(width: 36, height: 5)
            .frame(width: 120, height: 36)
            .contentShape(Rectangle())
            .gesture(drawerPull(opens: true, tap: true))
            .opacity(ui.talking || ui.typing || ui.drawerOpen || ui.view == "month" ? 0 : 1)
            .allowsHitTesting(!(ui.talking || ui.typing || ui.drawerOpen || ui.view == "month"))
            .animation(.easeOut(duration: 0.3), value: ui.talking || ui.typing || ui.drawerOpen)
            .offset(x: (W - 120) / 2, y: 500)
            .accessibilityLabel("展开今天的卡片")
    }

    private func drawerPull(opens: Bool, tap: Bool) -> some Gesture {
        DragGesture(minimumDistance: 0, coordinateSpace: .named("design"))
            .onChanged { v in
                if opens && engine.talk != nil { return }
                let moved = abs(v.location.y - v.startLocation.y) > 7
                if moved { sheetY = clamp(opens ? v.location.y - 116 : (ui.drawerOpen ? 0 : SHUT) + v.location.y - v.startLocation.y, 0, SHUT) }
            }
            .onEnded { v in
                if opens && engine.talk != nil { return }
                let dy = v.location.y - v.startLocation.y, vel = v.velocity.height
                let moved = sheetY != nil
                sheetY = nil
                if moved { engine.setDrawer(opens ? (dy < -40 || vel < -500) : !(dy > 40 || vel > 500)) }
                else if tap { engine.setDrawer(!ui.drawerOpen) }
            }
    }

    private var drawer: some View {
        let y = sheetY ?? (ui.drawerOpen ? 0 : SHUT)
        return VStack(spacing: 0) {
            Capsule().fill(Color(red: 214 / 255, green: 220 / 255, blue: 246 / 255).opacity(0.28)).frame(width: 36, height: 5)
                .frame(maxWidth: .infinity).frame(height: 38).contentShape(Rectangle())
                .gesture(DragGesture(minimumDistance: 0, coordinateSpace: .named("design"))
                    .onChanged { v in
                        if abs(v.location.y - v.startLocation.y) > 7 { sheetY = clamp(v.location.y - v.startLocation.y, 0, SHUT) }
                    }
                    .onEnded { v in
                        let dy = v.location.y - v.startLocation.y, vel = v.velocity.height, moved = sheetY != nil
                        sheetY = nil
                        if moved { engine.setDrawer(!(dy > 40 || vel > 500)) } else { engine.setDrawer(false) }
                    })
            ScrollView(showsIndicators: false) {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(ui.drawerGroups.enumerated()), id: \.element.id) { gi, g in
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            Text(g.name).font(.system(size: 13, weight: .semibold)).tracking(0.5).foregroundStyle(g.id == "等你的" ? amber : Color(red: 196 / 255, green: 204 / 255, blue: 238 / 255).opacity(0.66))
                            Text("\(g.rows.count)").font(monoF(11.5)).foregroundStyle(Color(red: 196 / 255, green: 204 / 255, blue: 238 / 255).opacity(0.42))
                        }.padding(.top, gi == 0 ? 4 : 22).padding(.bottom, 8)
                        ForEach(g.rows) { r in RowView(row: r, engine: engine) }
                    }
                    if !ui.history.isEmpty {
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            Text("对话").font(.system(size: 13, weight: .semibold)).tracking(0.5).foregroundStyle(Color(red: 196 / 255, green: 204 / 255, blue: 238 / 255).opacity(0.66))
                            Text("\(ui.history.count)").font(monoF(11.5)).foregroundStyle(Color(red: 196 / 255, green: 204 / 255, blue: 238 / 255).opacity(0.42))
                        }.padding(.top, ui.drawerGroups.isEmpty ? 4 : 22).padding(.bottom, 8)
                        ForEach(ui.history) { l in ConversationLineView(line: l) }
                    }
                }.padding(.horizontal, 30).padding(.bottom, 24)
            }
            .mask(LinearGradient(stops: [.init(color: .black, location: 0), .init(color: .black, location: 0.96), .init(color: .clear, location: 1)], startPoint: .top, endPoint: .bottom))
        }
        .padding(.bottom, 88)
        .frame(width: W, height: H - 104, alignment: .top)
        .background(UnevenRoundedRectangle(topLeadingRadius: 28, topTrailingRadius: 28).fill(.ultraThinMaterial).overlay(UnevenRoundedRectangle(topLeadingRadius: 28, topTrailingRadius: 28).fill(Color(red: 8 / 255, green: 10 / 255, blue: 24 / 255).opacity(0.72))))
        .overlay(alignment: .top) { UnevenRoundedRectangle(topLeadingRadius: 28, topTrailingRadius: 28).stroke(Color.white.opacity(0.1), lineWidth: 0.5) }
        .shadow(color: .black.opacity(0.4), radius: 25, y: -9)
        .offset(y: 104 + y)
        .opacity(y >= SHUT - 0.5 ? 0 : 1)
        .allowsHitTesting(ui.drawerOpen)
        .animation(sheetY == nil ? .spring(response: 0.4, dampingFraction: 0.88) : nil, value: y)
    }

    // MARK: talking to her

    private var caption: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(ui.capYou).font(.system(size: 16)).lineSpacing(16 * 0.45 - 4).lineLimit(2).frame(minHeight: 22, alignment: .topLeading)
                .foregroundStyle(Color(red: 214 / 255, green: 220 / 255, blue: 246 / 255).opacity(0.64))
            if !ui.capPics.isEmpty { SentItemsRow(items: ui.capPics) }
            herText
        }
        .frame(width: W - 64, alignment: .topLeading)
        .opacity(ui.capOut ? 0 : 1).offset(y: ui.capOut ? -6 : 0)
        .animation(.easeOut(duration: 0.35), value: ui.capOut)
        .allowsHitTesting(false)
        .offset(x: 32, y: 652)
    }
    /// the voice's one line, just over the composer: the model download, or what went wrong
    private var voiceNote: some View {
        let on = ui.voiceNote != nil && !ui.typing
        return Text(ui.voiceNote ?? "").font(monoF(12)).tracking(1.2).foregroundStyle(muted).lineLimit(1)
            .frame(width: W - 60, alignment: .leading)
            .opacity(on ? 1 : 0)
            .animation(.easeOut(duration: 0.3), value: on)
            .allowsHitTesting(false)
            .offset(x: 30, y: 846 - ui.attach.rise)
    }
    private var herText: Text {
        var a = AttributedString()
        for (i, ch) in ui.capHer.enumerated() {
            var s = AttributedString(String(ch))
            s.foregroundColor = Color.white.opacity(i < ui.capHerAlpha.count ? ui.capHerAlpha[i] : 0.32)
            a += s
        }
        return Text(a).font(.system(size: 23, weight: .medium))
    }

    // MARK: the composer: type to her, or tap the mic. A + at the left, what is attached above the words, a field that grows to five lines.

    /// words and attachments go together; with no words, the attachments are named
    private func sendComposer() {
        let words = ui.text.trimmingCharacters(in: .whitespacesAndNewlines)
        if BrainLink.shared.hasToken { return engine.sendToBrain(words) }
        let sent = ui.attach.take()
        if words.isEmpty && sent.isEmpty { return }
        // her reply to what was sent: a file is offered a first read, a picture asks what to do with it
        let reply = sent.isEmpty ? nil : sent[0].kind == .file ? "收到「\(sent[0].stem)」。要我先看一遍，挑重点讲给你听吗？" : "收到。要我记下来，还是帮你看看？"
        engine.send(words.isEmpty ? AttachModel.caption(sent) : words, reply: reply, pics: SentPics.items(sent))
    }
    private func composerTap() {
        ui.attach.close()
        if ui.attach.hasItems || !ui.text.trimmingCharacters(in: .whitespaces).isEmpty { sendComposer() } else { engine.micTap() }
    }

    private var composer: some View {
        let at = ui.attach
        return VStack(spacing: 0) {
            AttachStrip(model: at)
            ZStack(alignment: .bottom) {
                AttachField(text: $ui.text, focused: $focused, model: at, send: sendComposer)
                    .onChange(of: ui.text) { _, t in if t.count > 120 { ui.text = String(t.prefix(120)) } }
                HStack(spacing: 0) {
                    AttachPlusButton(model: at)
                    Spacer(minLength: 0)
                    MicButton(state: at.hasItems ? .send : ui.mic) { composerTap() }
                }.padding(.horizontal, 6).padding(.bottom, 6)
            }
            .frame(width: W - 40, height: at.fieldH)
        }
        .frame(width: W - 40, height: 48 + at.rise, alignment: .bottom)
        .glass(RoundedRectangle(cornerRadius: 24), focused: focused)
        .overlay(alignment: .topLeading) {
            WrapLayout(spacing: 8) {
                ForEach(Array(engine.talks.enumerated()), id: \.offset) { _, t in
                    Button { engine.send(t.q) } label: {
                        Text(t.q).font(.system(size: 14)).foregroundStyle(Color(red: 226 / 255, green: 230 / 255, blue: 250 / 255).opacity(0.92)).lineLimit(1).fixedSize()
                            .padding(.horizontal, 14).frame(height: 34).background(Color(red: 18 / 255, green: 22 / 255, blue: 52 / 255).opacity(0.66), in: Capsule())
                            .overlay(Capsule().stroke(Color.white.opacity(0.1), lineWidth: 0.5))
                    }.buttonStyle(PressScale())
                }
            }
            .frame(width: W - 40, alignment: .leading)
            .fixedSize(horizontal: false, vertical: true)
            .alignmentGuide(.top) { $0[.bottom] + 58 }
            .opacity(ui.chipsOn && !at.hasItems && !at.isOpen ? 1 : 0).offset(y: ui.chipsOn && !at.hasItems && !at.isOpen ? 0 : 8)
            .allowsHitTesting(ui.chipsOn && !at.hasItems && !at.isOpen)
            .animation(.easeOut(duration: 0.3), value: ui.chipsOn && !at.hasItems && !at.isOpen)
        }
        .offset(x: 20, y: H - 34 - 48 - at.rise - ui.kbLift)
    }
}

// MARK: - small pieces

private let hairline = Color(red: 157 / 255, green: 180 / 255, blue: 1).opacity(0.12)

/// chips that wrap onto the next line, like the prototype's flex-wrap
struct WrapLayout: Layout {
    var spacing: CGFloat = 8
    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let w = proposal.width ?? .infinity
        var x: CGFloat = 0, y: CGFloat = 0, rowH: CGFloat = 0, maxW: CGFloat = 0
        for s in subviews {
            let d = s.sizeThatFits(.unspecified)
            if x > 0 && x + d.width > w { x = 0; y += rowH + spacing; rowH = 0 }
            x += d.width + spacing; rowH = max(rowH, d.height); maxW = max(maxW, x - spacing)
        }
        return CGSize(width: maxW, height: y + rowH)
    }
    func placeSubviews(in b: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = b.minX, y = b.minY, rowH: CGFloat = 0
        for s in subviews {
            let d = s.sizeThatFits(.unspecified)
            if x > b.minX && x + d.width > b.maxX { x = b.minX; y += rowH + spacing; rowH = 0 }
            s.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(d)); x += d.width + spacing; rowH = max(rowH, d.height)
        }
    }
}

struct PressScale: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label.scaleEffect(configuration.isPressed ? 0.95 : 1).animation(.easeOut(duration: 0.2), value: configuration.isPressed)
    }
}

extension View {
    /// the frosted-glass look of the prototype's controls (a plain translucent fill: no blur to pay for on a moving sky)
    func glass<S: InsettableShape>(_ shape: S, focused: Bool = false) -> some View {
        background(shape.fill(Color.white.opacity(0.08)))
            .overlay(shape.strokeBorder(LinearGradient(colors: [Color.white.opacity(0.2), Color.white.opacity(0.06)], startPoint: .top, endPoint: .bottom), lineWidth: 0.6))
            .overlay(shape.strokeBorder(Color(red: 157 / 255, green: 180 / 255, blue: 1).opacity(focused ? 0.5 : 0), lineWidth: 1))
            .animation(.easeOut(duration: 0.25), value: focused)
    }
}

struct SegControl: View {
    let level: Int
    let pick: (Int) -> Void
    private let labels = ["近", "日", "周", "月"]
    var body: some View {
        ZStack(alignment: .leading) {
            Capsule().fill(pale).frame(width: 36, height: 28).offset(x: 2 + 36 * CGFloat(level + 1))
                .animation(.spring(response: 0.4, dampingFraction: 0.72), value: level)
            HStack(spacing: 0) {
                ForEach(0..<4, id: \.self) { i in
                    Button { pick(i - 1) } label: {
                        Text(labels[i]).font(.system(size: 13.5, weight: .medium))
                            .foregroundStyle(level + 1 == i ? navy : Color(red: 214 / 255, green: 220 / 255, blue: 246 / 255).opacity(0.64))
                            .frame(width: 36, height: 28).contentShape(Rectangle().inset(by: -4))
                    }.buttonStyle(.plain)
                }
            }.padding(.horizontal, 2)
        }
        .frame(width: 148, height: 32).glass(Capsule())
        .animation(.easeOut(duration: 0.3), value: level)
    }
}

struct MicButton: View {
    let state: MicState
    let tap: () -> Void
    @State private var ring = false
    var body: some View {
        Button(action: tap) {
            ZStack {
                Circle().fill(state == .mic ? Color.white.opacity(0.1) : pale)
                Image(systemName: "mic").font(.system(size: 17, weight: .regular)).opacity(state == .send ? 0 : 1).scaleEffect(state == .send ? 0.6 : 1)
                Image(systemName: "arrow.up").font(.system(size: 17, weight: .semibold)).opacity(state == .send ? 1 : 0).scaleEffect(state == .send ? 1 : 0.6)
                if state == .on {
                    Circle().stroke(Color(red: 157 / 255, green: 180 / 255, blue: 1).opacity(ring ? 0 : 0.55), lineWidth: 2).scaleEffect(ring ? 1.7 : 1)
                        .animation(.easeOut(duration: 1.7).repeatForever(autoreverses: false), value: ring)
                        .onAppear { ring = true }.onDisappear { ring = false }
                }
            }
            .frame(width: 36, height: 36)
            .foregroundStyle(state == .mic ? Color(red: 226 / 255, green: 230 / 255, blue: 250 / 255).opacity(0.88) : navy)
            .animation(.easeOut(duration: 0.25), value: state)
        }.buttonStyle(PressScale())
        .accessibilityLabel(state == .send ? "发送" : state == .on ? "打断" : "语音")
    }
}

struct FlowChips: View {
    let chips: [ChipModel]
    var body: some View {
        HStack(spacing: 18) {
            ForEach(chips) { c in
                HStack(spacing: 6) { MarkShape(kd: c.kd).frame(width: 14, height: 14); Text(c.text) }
            }
        }
        .font(.system(size: 13)).foregroundStyle(Color(red: 214 / 255, green: 220 / 255, blue: 246 / 255).opacity(0.72)).lineLimit(1)
    }
}

/// the little marks of a row: a lit star, an unlit star, a done ring, a waiting point, a moving line
struct MarkShape: View {
    let kd: Kd
    var body: some View {
        TimelineView(.animation(minimumInterval: 1 / 15, paused: kd != .wait)) { tl in
            let t = tl.date.timeIntervalSinceReferenceDate
            Canvas { c, size in
                let s = size.width / 16
                c.translateBy(x: size.width / 2, y: size.height / 2); c.scaleBy(x: s, y: s)
                func star() -> Path {
                    var p = Path(); p.move(to: CGPoint(x: 0, y: -5.2)); p.addQuadCurve(to: CGPoint(x: 5.2, y: 0), control: .zero); p.addQuadCurve(to: CGPoint(x: 0, y: 5.2), control: .zero)
                    p.addQuadCurve(to: CGPoint(x: -5.2, y: 0), control: .zero); p.addQuadCurve(to: CGPoint(x: 0, y: -5.2), control: .zero); return p
                }
                switch kd {
                case .lit:
                    c.drawLayer { l in l.addFilter(.shadow(color: Color(red: 1, green: 226 / 255, blue: 186 / 255).opacity(0.9), radius: 2.5)); l.fill(star(), with: .color(Color(red: 1, green: 243 / 255, blue: 223 / 255))) }
                case .next:
                    c.stroke(star(), with: .color(Color(red: 214 / 255, green: 222 / 255, blue: 1).opacity(0.72)), lineWidth: 0.9)
                case .done:
                    c.drawLayer { l in l.addFilter(.shadow(color: Color(red: 111 / 255, green: 224 / 255, blue: 180 / 255).opacity(0.6), radius: 2)); l.stroke(Path(ellipseIn: CGRect(x: -3.3, y: -3.3, width: 6.6, height: 6.6)), with: .color(Color(red: 139 / 255, green: 230 / 255, blue: 194 / 255)), lineWidth: 1.5) }
                case .wait:
                    let a = 0.62 + 0.38 * (0.5 + 0.5 * cos(t * 2 * .pi / 2.4))
                    c.opacity = a
                    c.drawLayer { l in l.addFilter(.shadow(color: Color(red: 1, green: 201 / 255, blue: 143 / 255).opacity(0.9), radius: 3)); l.fill(Path(ellipseIn: CGRect(x: -3.4, y: -3.4, width: 6.8, height: 6.8)), with: .color(Color(red: 1, green: 210 / 255, blue: 161 / 255))) }
                case .work:
                    c.drawLayer { l in
                        l.addFilter(.shadow(color: Color(red: 108 / 255, green: 156 / 255, blue: 1).opacity(0.8), radius: 2.5))
                        var p = Path(); p.move(to: CGPoint(x: -5.6, y: 0)); p.addLine(to: CGPoint(x: 2.8, y: 0))
                        l.stroke(p, with: .color(Color(red: 143 / 255, green: 179 / 255, blue: 1)), style: StrokeStyle(lineWidth: 1.8, lineCap: .round))
                        l.fill(Path(ellipseIn: CGRect(x: 1.3, y: -1.9, width: 3.8, height: 3.8)), with: .color(Color(red: 201 / 255, green: 216 / 255, blue: 1)))
                    }
                }
            }
        }
    }
}

struct RowView: View {
    let row: RowModel
    let engine: StarEngine
    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 12) {
                MarkShape(kd: row.kd).frame(width: 16, height: 16).frame(width: 22)
                VStack(alignment: .leading, spacing: 3) {
                    Text(row.title).font(.system(size: 16)).foregroundStyle(ink).lineLimit(1)
                    Text(row.meta).font(.system(size: 12.5)).foregroundStyle(muted).lineLimit(1)
                }.padding(.vertical, 9)
                Spacer(minLength: 0)
                switch row.rightStyle {
                case .look: Text(row.right).font(.system(size: 14, weight: .medium)).foregroundStyle(amber)
                case .mut: Text(row.right).font(monoF(12, .regular)).foregroundStyle(Color(red: 196 / 255, green: 204 / 255, blue: 238 / 255).opacity(0.55))
                case .src: Text(row.right).font(monoF(12, .regular)).foregroundStyle(Color(red: 1, green: 228 / 255, blue: 196 / 255).opacity(0.6))
                }
            }
            .frame(minHeight: 56)
            .contentShape(Rectangle())
            .onTapGesture { engine.rowTap(row.id) }
            if let ex = row.expand { ExpandView(ex: ex, engine: engine).padding(.leading, 34).padding(.bottom, 14).transition(.opacity.combined(with: .offset(y: 8))) }
        }
        .overlay(alignment: .top) { Rectangle().fill(hairline).frame(height: 0.5) }
        .animation(.easeOut(duration: 0.4), value: row.expand)
    }
}

struct ExpandView: View {
    let ex: Expand
    let engine: StarEngine
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            switch ex {
            case .draft(let t): para(Text(t)); btns(("改一下", "edit"), ("发送", "send"))
            case .call: btns(("晚 10 分钟", "later"), ("现在打", "call"))
            case .approve:
                para(Text("Jarvis 仓库 · 跑 ") + Text("tests/surface").font(monoF(12.5, .regular)).foregroundColor(Color(red: 201 / 255, green: 214 / 255, blue: 1)) + Text("，大约两分钟。"))
                btns(("不行", "deny"), ("允许", "allow"))
            case .hike: btns(("算了", "no"), ("去", "yes"))
            case .note(let t): para(Text(t))
            case .card(let c): WaitingCardView(card: c)
            }
        }
    }
    private func para(_ t: Text) -> some View {
        t.font(.system(size: 14)).lineSpacing(14 * 0.5 - 3).foregroundStyle(Color(red: 226 / 255, green: 230 / 255, blue: 250 / 255).opacity(0.84))
            .padding(.horizontal, 14).padding(.vertical, 12).frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.white.opacity(0.05), in: RoundedRectangle(cornerRadius: 14))
            .overlay(RoundedRectangle(cornerRadius: 14).stroke(Color.white.opacity(0.08), lineWidth: 0.5))
    }
    private func btns(_ a: (String, String), _ b: (String, String)) -> some View {
        HStack(spacing: 10) {
            Button { engine.rowAction(a.1) } label: { Text(a.0).font(.system(size: 15.5, weight: .semibold)).frame(maxWidth: .infinity).frame(height: 44).background(Color.white.opacity(0.1), in: Capsule()) }
                .buttonStyle(PressScale())
            Button { engine.rowAction(b.1) } label: { Text(b.0).font(.system(size: 15.5, weight: .semibold)).foregroundStyle(navy).frame(maxWidth: .infinity).frame(height: 44).background(pale, in: Capsule()) }
                .buttonStyle(PressScale())
        }.foregroundStyle(ink)
    }
}
