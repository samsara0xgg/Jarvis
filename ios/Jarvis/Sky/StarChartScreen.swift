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
    @State private var pull: Double? = nil             // while a finger holds the talk panel's top edge
    @Environment(\.scenePhase) private var phase
    @Environment(\.accessibilityReduceMotion) private var reduce
    @State private var leftAt: Date?                   // when the app went to the background

    var body: some View {
        ZStack(alignment: .topLeading) {
            // behind the open panel the dial dims
            Color.black.opacity(0.35 * panelOpenness(panelTop(ui, pull))).frame(width: W * 3, height: H * 3).offset(x: -W, y: -H)
                .animation(pull == nil ? panelSpring(reduce, velocity: ui.panelRelease) : nil, value: ui.panelOpen).allowsHitTesting(false)
            topRow
            detail
                grabber
            Group { if ui.tlShow { DayTimelineView(engine: engine, ui: ui, pull: $pull).transition(.opacity) } }
                .animation(.easeOut(duration: 0.35), value: ui.tlShow)
            TalkPanel(engine: engine, ui: ui, pull: $pull)
            voiceNote
            AttachDim(model: ui.attach)
            if ui.tuning && ui.level == -1 { NearTune(engine: engine, done: { ui.tuning = false }) }
            composer
            AttachOverlay(model: ui.attach, kbLift: ui.kbLift)
            TapbackOverlay(engine: engine, ui: ui).animation(.easeOut(duration: 0.15), value: ui.tapback != nil)
        }
        .frame(width: W, height: H, alignment: .topLeading)
        .onChange(of: focused) { _, f in
            StallLog.mark(f ? "keyboard up" : "keyboard down"); ui.typing = f
            if !HomeModule.shared.isTimeline { if f { engine.openPanel() } else { engine.closePanel() } }       // under the dial the panel comes with the keyboard and goes with it; on the timeline the day stays, and the history is his to open
        }
        .onChange(of: phase) { _, p in
            // away for five minutes, it comes back to the star chart as it was first opened
            if p == .background { leftAt = Date() }
            else if p == .active, let t = leftAt { leftAt = nil; if Date().timeIntervalSince(t) > 300 { engine.comeBack() } }
        }
        .onAppear {
            engine.endEditing = { focused = false }
            StallLog.context = { [engine] in engine.stallContext() }
            engine.beginEditing = { focused = true }
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
            let lift = StarUI.lift(keyboardHeight: max(0, screenH - f.minY) / k)
            // the composer goes with the keyboard: as long as it takes, and on its curve (the system's own, a fast start that settles, is the one a keyboard asks for; any other is the plain curve it names)
            let d = (n.userInfo?[UIResponder.keyboardAnimationDurationUserInfoKey] as? Double) ?? 0.25, curve = (n.userInfo?[UIResponder.keyboardAnimationCurveUserInfoKey] as? Int) ?? 7
            withAnimation(Self.keyboardAnimation(duration: d, curve: curve)) { ui.kbLift = lift }
        }
        .sheet(isPresented: $ui.settingsOpen) { SettingsSheet(tuneNear: { ui.settingsOpen = false; engine.setLevel(-1); ui.tuning = true }) }
    }

    /// The keyboard's own motion as an animation: its curve 7 (a spring the system keeps to itself) as a bezier that starts fast and settles, the others as the plain curves they are.
    static func keyboardAnimation(duration d: Double, curve: Int) -> Animation {
        let d = d > 0 ? d : 0.25
        switch curve {
        case 0: return .easeInOut(duration: d)
        case 1: return .easeIn(duration: d)
        case 2: return .easeOut(duration: d)
        case 3: return .linear(duration: d)
        default: return .timingCurve(0.2, 0.9, 0.3, 1, duration: d)
        }
    }

    /// a tap on a notification that waits on him: the card it names under the dial, else the panel with what waits pinned
    private func openCards() {
        CardsRequest.pending = false
        if let id = CardsRequest.card { CardsRequest.card = nil; Task { await engine.openWaitingCard(id) } }
        else { engine.openPanel() }
    }

    // MARK: the top row: the day you are looking at, the scale, settings

    /// what the top row leaves between the date and the settings button on the timeline (its gaps, and the empty place of the scale, counted)
    private var headRoom: Double {
        let date = tlWidth(ui.dateText, size: 15, weight: .semibold) + (ui.talking ? 0 : ui.dateLabel.map { tlWidth(" · " + $0, size: 13) } ?? 0)
        let tidy = ui.tl.tidyShown ? TLTidyButton.width(ui.tl.tidy) + 8 : 0          // and the capsule of 理一下 beside the settings button
        return W - 30 - 36 - 3 * 8 - (30 + TLLayout.dateShift) - date - 8 - tidy
    }

    private var topRow: some View {
        let tlOn = HomeModule.shared.isTimeline          // the timeline in the dial's place: she stands in the corner, so the date starts further right, and there is no scale to pick
        return HStack(spacing: 8) {
            HStack(spacing: 0) {
                Text(ui.dateText).font(.system(size: 15, weight: .semibold)).foregroundStyle(ink).lineLimit(1).layoutPriority(1)
                if let l = ui.dateLabel, !ui.talking { Text(" · " + l).font(.system(size: 13)).foregroundStyle(muted).lineLimit(1).fixedSize() }      // short enough to fit beside the date; the talk's time takes its place
                Text("  " + ui.topTime).font(monoF(14)).foregroundStyle(ink).lineLimit(1)
                    .opacity(ui.talking && !tlOn ? 1 : 0).animation(.easeOut(duration: 0.25), value: ui.talking)
                    .frame(width: tlOn ? 0 : nil, alignment: .leading)
            }
            if tlOn { TLDayHead(tl: ui.tl, room: headRoom) }        // after the date: 今天 or the way back to it, and the day's weather
            Spacer(minLength: 0)
            SegControl(level: ui.level) { engine.setLevel(Double($0)) }
                .opacity(tlOn ? 0 : 1).allowsHitTesting(!tlOn)
                .frame(width: tlOn ? 0 : 148, alignment: .trailing)          // no scale to pick on the timeline: its room is the date's
            if tlOn && ui.tl.tidyShown { TLTidyButton(tl: ui.tl).transition(.opacity) }        // at the right, left of the settings button: the brain tidies today
            Button { ui.settingsOpen = true } label: {
                Image(systemName: "gearshape").font(.system(size: 18, weight: .regular)).foregroundStyle(Color(red: 214 / 255, green: 220 / 255, blue: 246 / 255).opacity(0.8))
                    .frame(width: 36, height: 36).glass(Circle())
                    .overlay(alignment: .topTrailing) {     // the day is being recorded (随身录音); its switch is in settings
                        if LifelogRecorder.shared.recording { Circle().fill(Color(red: 1, green: 92 / 255, blue: 92 / 255)).frame(width: 6, height: 6).offset(x: -3, y: 3) }
                    }
            }.buttonStyle(PressScale())
            .accessibilityLabel(LifelogRecorder.shared.recording ? "设置，正在随身录音" : "设置")
        }
        .padding(.leading, tlOn ? 30 + TLLayout.dateShift : 30).padding(.trailing, 30)
        .animation(.easeOut(duration: 0.35), value: tlOn)
        .animation(.easeOut(duration: 0.2), value: ui.tl.tidyShown)
        .frame(width: W, height: 38)
        .offset(y: 60)
    }

    // MARK: the panel under the dial

    private var detail: some View {
        let away = ui.talking || ui.typing || ui.panelOpen || ui.tuning || HomeModule.shared.isTimeline        // the timeline takes its place the way a talk does
        return VStack(alignment: .leading, spacing: 0) {
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
            .gesture(panelDrag(engine, $pull, opens: true, tap: false))
            Text(ui.dayText).font(monoF(12)).tracking(1.2).foregroundStyle(muted).lineLimit(1).padding(.top, 12)
                .opacity(ui.kbLift > 80 ? 0 : 1)
            if let line = ui.dayLine {
                Text(line).font(.system(size: 14)).lineSpacing(2).foregroundStyle(ink).lineLimit(2).padding(.top, 7)
                    .opacity(ui.kbLift > 80 ? 0 : 1)
            }
            Group {
                if ui.showSum { sum } else { rowList(ui.rows, hint: ui.emptyHint) }
            }
            .opacity(ui.detFast ? 0.4 : 1)
            .animation(.easeOut(duration: ui.detFast ? 0.2 : 0.35), value: ui.detFast)
        }
        .padding(.horizontal, 30)
        .frame(width: W, alignment: .topLeading)
        .id(ui.flip)
        .transition(.opacity.combined(with: .offset(y: 7)))
        .animation(.easeOut(duration: 0.42), value: ui.flip)
        // a talk takes the place of the detail: it goes up and out, and comes back when the talk has gone
        .opacity(away ? 0 : 1).offset(y: away && !reduce ? -6 : 0)
        .allowsHitTesting(!away)
        .animation(.easeOut(duration: away ? 0.2 : 0.35), value: away)
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

    // MARK: the grabber: pull the talk panel up over the dial

    private var grabber: some View {
        let hide = ui.typing || ui.panelOpen || ui.view == "month" || HomeModule.shared.isTimeline
        return Capsule().fill(Color(red: 214 / 255, green: 220 / 255, blue: 246 / 255).opacity(0.28)).frame(width: 36, height: 5)
            .frame(width: 120, height: 36)
            .contentShape(Rectangle())
            .gesture(panelDrag(engine, $pull, opens: true, tap: true))
            .opacity(hide ? 0 : 1 - ui.camE)      // close up it would lie across the road; the big time pulls the panel instead
            .allowsHitTesting(!hide && ui.camE <= 0.5)
            .animation(.easeOut(duration: 0.3), value: hide)
            .offset(x: (W - 120) / 2, y: 500)
            .accessibilityLabel("展开对话")
    }

    // MARK: talking to her

    /// the voice's one line, just over the composer: the model download, or what went wrong
    private var voiceNote: some View {
        let on = ui.voiceNote != nil
        return Text(ui.voiceNote ?? "").font(monoF(12)).tracking(1.2).foregroundStyle(muted).lineLimit(1)
            .frame(width: W - 60, alignment: .leading)
            .opacity(on ? 1 : 0)
            .animation(.easeOut(duration: 0.3), value: on)
            .allowsHitTesting(false)
            .offset(x: 30, y: 846 - ui.attach.rise - ui.kbLift)
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

    /// a chip of what he may say or ask, above the field
    private func suggestion(_ words: String, symbol: String? = nil) -> some View {
        HStack(spacing: 6) {
            if let symbol { Image(systemName: symbol).font(.system(size: 12.5, weight: .semibold)) }
            Text(words)
        }
        .font(.system(size: 14)).foregroundStyle(Color(red: 226 / 255, green: 230 / 255, blue: 250 / 255).opacity(0.92)).lineLimit(1).fixedSize()
        .padding(.horizontal, 14).frame(height: 34).background(Color(red: 18 / 255, green: 22 / 255, blue: 52 / 255).opacity(0.66), in: Capsule())
        .overlay(Capsule().stroke(Color.white.opacity(0.1), lineWidth: 0.5))
    }

    private var composer: some View {
        let at = ui.attach
        return VStack(spacing: 0) {
            AttachStrip(model: at)
            ZStack(alignment: .bottom) {
                AttachField(text: $ui.text, focused: $focused, model: at, send: sendComposer)
                    .onChange(of: ui.text) { _, t in if let c = AttachField.clip(t) { ui.text = c; engine.trouble("太长了，只留了前 2 万字") } }
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
                if HomeModule.shared.isTimeline && ui.tl.tidyShown {          // on today's timeline the first of them has the brain tidy the day: it sends her nothing, and the keyboard goes down
                    Button { ui.tl.tidyDay(); engine.endEditing?() } label: { suggestion(ui.tl.tidy == .running ? "在理…" : "理一下", symbol: "sparkles") }.buttonStyle(PressScale())
                }
                ForEach(Array(engine.talks.enumerated()), id: \.offset) { _, t in
                    Button { engine.send(t.q) } label: { suggestion(t.q) }.buttonStyle(PressScale())
                }
            }
            .frame(width: W - 40, alignment: .leading)
            .fixedSize(horizontal: false, vertical: true)
            .alignmentGuide(.top) { $0[.bottom] + 58 }
            .opacity(ui.chipsOn && !at.hasItems && !at.isOpen ? 1 : 0).offset(y: ui.chipsOn && !at.hasItems && !at.isOpen ? 0 : 8)
            .allowsHitTesting(ui.chipsOn && !at.hasItems && !at.isOpen)
            .animation(.easeOut(duration: 0.3), value: ui.chipsOn && !at.hasItems && !at.isOpen)
        }
        .opacity(ui.tl.renaming == nil ? 1 : 0).allowsHitTesting(ui.tl.renaming == nil)        // a place being renamed has the keyboard: the composer does not rise over its field
        .animation(.easeOut(duration: 0.15), value: ui.tl.renaming == nil)
        .offset(x: 20, y: H - StarUI.restGap - 48 - at.rise - ui.kbLift)
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
        configuration.label.scaleEffect(configuration.isPressed ? 0.95 : 1).animation(configuration.isPressed ? .snappy(duration: 0.12) : .smooth(duration: 0.25), value: configuration.isPressed)
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
            case .talk(let his, let hers, let start):
                VStack(alignment: .leading, spacing: 8) {
                    if !his.isEmpty { Text(his).font(.system(size: 14)).foregroundStyle(ink.opacity(0.86)).lineLimit(2) }
                    if !hers.isEmpty { Text(hers).font(.system(size: 14)).foregroundStyle(muted).lineLimit(2) }
                    Button { engine.showTalk(start) } label: {
                        Text("全部 ›").font(.system(size: 13.5, weight: .semibold)).foregroundStyle(ink).padding(.horizontal, 14).frame(height: 30).background(Color.white.opacity(0.1), in: Capsule())
                    }.buttonStyle(PressScale())
                }
                .padding(.horizontal, 14).padding(.vertical, 12).frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.white.opacity(0.05), in: RoundedRectangle(cornerRadius: 14))
                .overlay(RoundedRectangle(cornerRadius: 14).stroke(Color.white.opacity(0.08), lineWidth: 0.5))
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
