import AVFoundation
import SwiftUI
import UIKit

/// The remote view: his Mac's screen edge to edge, small buttons in the top corner (unlock while it is locked, trackpad mode, keyboard, close)
/// and, under his thumb, hold to talk.
struct RemoteScreen: View {
    let close: () -> Void
    @State private var live = RemoteLive()
    @State private var asking = false
    @State private var typed = ""
    @State private var pasteFailed = false
    @State private var trackpad = false
    @State private var showKeys = true
    private var mac: RemoteMac { .shared }

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            if mac.isPaired {
                CanvasView(canvas: live.canvas).ignoresSafeArea()
                overlay
            } else {
                pairCard
            }
            controls
            if case .live = live.state { talk }
        }
        .statusBarHidden()
        .persistentSystemOverlays(.hidden)
        .onAppear {
            UIApplication.shared.isIdleTimerDisabled = true
            live.connect()
        }
        .onDisappear {
            UIApplication.shared.isIdleTimerDisabled = false
            live.session.stop()
        }
        .sheet(isPresented: $asking) { passwordSheet }
    }

    @ViewBuilder private var overlay: some View {
        switch live.state {
        case .connecting:
            ProgressView(Words.Remote.connecting).tint(.white).foregroundStyle(.white)
        case .closed:
            message(Words.Remote.closed, Words.Remote.retry) { live.connect() }
        case .refused:
            message(Words.Remote.refused, Words.Remote.repair) { mac.unpair() }
        case .live:
            EmptyView()
        }
    }

    private var controls: some View {
        VStack {
            HStack(spacing: 10) {
                Spacer()
                if case .live(locked: true) = live.state {
                    corner("lock.open", Words.Remote.unlock) {
                        if let p = mac.password { live.unlock(p) } else { asking = true }
                    }
                    .simultaneousGesture(LongPressGesture().onEnded { _ in asking = true })
                }
                if case .live = live.state {
                    corner(trackpad ? "hand.point.up.left.fill" : "cursorarrow.motionlines", Words.Remote.trackpad) {
                        trackpad.toggle()
                        live.canvas.trackpad = trackpad
                    }
                    corner("magnifyingglass", Words.Remote.launcher) { live.canvas.chord(49, mods: 0x80000) }   // ⌥Space: Raycast's own default
                    corner("dock.rectangle", Words.Remote.dock) { live.canvas.showDock() }
                    corner(showKeys ? "command.circle.fill" : "command", Words.Remote.keys) { showKeys.toggle() }
                    corner("keyboard", Words.Remote.keyboard) { live.canvas.toggleKeyboard() }
                }
                corner("xmark", Words.Remote.close, close)
            }
            Spacer()
        }
        .padding(12)
    }

    /// The keys he reaches for most when the iPad's own keyboard has no ⌘: edit, confirm, move between apps and windows.
    @MainActor private static var shortcuts: [(label: String, code: Int, mods: Int)] {
        let cmd = 0x100000, shift = 0x20000
        return [
            (Words.Remote.copy, 8, cmd), (Words.Remote.pasteKey, 9, cmd), (Words.Remote.undo, 6, cmd), (Words.Remote.redo, 6, cmd | shift),
            (Words.Remote.selectAll, 0, cmd), (Words.Remote.enter, 36, 0), (Words.Remote.backspace, 51, 0), (Words.Remote.escape, 53, 0),
            (Words.Remote.tab, 48, 0), (Words.Remote.switchApp, 48, cmd), (Words.Remote.find, 3, cmd), (Words.Remote.save, 1, cmd),
            (Words.Remote.newTab, 17, cmd), (Words.Remote.closeWindow, 13, cmd), (Words.Remote.quitApp, 12, cmd),
        ]
    }

    /// Hold to talk: what Qwen hears so far shows above the button; letting go types it into the Mac.
    private var talk: some View {
        VStack(spacing: 10) {
            Spacer()
            if let line = live.heard {
                Text(line.isEmpty ? Words.Remote.listening : line)
                    .font(.callout)
                    .foregroundStyle(.white)
                    .padding(.horizontal, 14).padding(.vertical, 8)
                    .background(.ultraThinMaterial, in: Capsule())
                    .frame(maxWidth: 520)
            }
            HStack(alignment: .bottom, spacing: 12) {
                if showKeys {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 8) {
                            ForEach(Self.shortcuts, id: \.label) { k in
                                Button(k.label) { live.canvas.chord(k.code, mods: k.mods) }
                                    .font(.callout.weight(.medium))
                                    .foregroundStyle(.white)
                                    .padding(.horizontal, 12).frame(height: 40)
                                    .background(.ultraThinMaterial, in: Capsule())
                            }
                        }
                    }
                    .frame(height: 60)
                }
                Spacer(minLength: 0)
                Image(systemName: live.heard == nil ? "mic.fill" : "waveform")
                    .font(.system(size: 22, weight: .semibold))
                    .frame(width: 60, height: 60)
                    .background(live.heard == nil ? AnyShapeStyle(.ultraThinMaterial) : AnyShapeStyle(Color.accentColor), in: Circle())
                    .foregroundStyle(.white)
                    .gesture(DragGesture(minimumDistance: 0)
                        .onChanged { _ in if live.heard == nil { live.startTalking() } }
                        .onEnded { _ in live.stopTalking() })
                    .accessibilityLabel(Words.Remote.talk)
            }
        }
        .padding(20)
    }

    private func corner(_ symbol: String, _ label: String, _ action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 15, weight: .semibold))
                .frame(width: 36, height: 36)
                .background(.ultraThinMaterial, in: Circle())
        }
        .foregroundStyle(.white)
        .accessibilityLabel(label)
    }

    private func message(_ text: String, _ button: String, _ action: @escaping () -> Void) -> some View {
        VStack(spacing: 14) {
            Text(text).multilineTextAlignment(.center)
            Button(button, action: action).buttonStyle(.borderedProminent)
        }
        .foregroundStyle(.white)
        .padding(24)
        .frame(maxWidth: 420)
    }

    private var pairCard: some View {
        VStack(spacing: 16) {
            Text(Words.Remote.title).font(.title2.weight(.semibold))
            Text(Words.Remote.pairHow).multilineTextAlignment(.center).foregroundStyle(.secondary)
            Button(Words.Remote.paste) {
                pasteFailed = !mac.pair(UIPasteboard.general.string ?? "")
                if !pasteFailed { live.connect() }
            }
            .buttonStyle(.borderedProminent)
            if pasteFailed { Text(Words.Remote.notALink).font(.footnote).foregroundStyle(.secondary) }
        }
        .foregroundStyle(.white)
        .padding(24)
        .frame(maxWidth: 420)
    }

    private var passwordSheet: some View {
        NavigationStack {
            Form {
                Section {
                    SecureField(Words.Remote.password, text: $typed)
                        .textContentType(.password)
                        .onSubmit(saveAndUnlock)
                } footer: { Text(Words.Remote.passwordNote) }
                if mac.password != nil {
                    Button(Words.Remote.forget, role: .destructive) { mac.forgetPassword(); asking = false }
                }
            }
            .navigationTitle(Words.Remote.passwordTitle)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button(Words.Remote.cancel) { asking = false } }
                ToolbarItem(placement: .confirmationAction) { Button(Words.Remote.saveAndUnlock, action: saveAndUnlock).disabled(typed.isEmpty) }
            }
        }
        .presentationDetents([.medium])
    }

    private func saveAndUnlock() {
        guard !typed.isEmpty else { return }
        mac.savePassword(typed)
        live.unlock(typed)
        typed = ""
        asking = false
    }
}

/// The session and its canvas, kept for as long as the view is open.
@MainActor @Observable final class RemoteLive {
    let session = RemoteSession()
    @ObservationIgnored lazy var canvas = RemoteCanvas(session: session)
    private(set) var state = RemoteSession.State.connecting

    init() {
        session.onState = { [weak self] in self?.state = $0 }
        session.onSize = { [weak self] in self?.canvas.videoSize = $0 }
    }

    func connect() {
        let mac = RemoteMac.shared
        guard mac.isPaired else { return }
        session.start(host: mac.host, port: mac.port, token: mac.token, curtain: true)
    }

    let voice = RemoteVoice()
    /// While he holds to talk: Qwen's words so far ("" before the first); nil otherwise.
    private(set) var heard: String?
    private var talking = false

    func startTalking() {
        guard voice.canHear else { heard = Words.Remote.noHearing; Task { try? await Task.sleep(for: .seconds(2)); heard = nil }; return }
        heard = ""
        do { try voice.start { [weak self] in self?.heard = $0 }; talking = true } catch { voice.cancel(); heard = nil }
    }

    func stopTalking() {
        guard talking else { return }
        talking = false
        Task {
            let words = await voice.finish()
            heard = nil
            if let words, !words.isEmpty { session.input(["t": "paste", "s": words]) }
        }
    }

    /// The password typed into the lock screen's field, then return.
    func unlock(_ password: String) {
        session.input(["t": "text", "s": password])
        session.input(["t": "key", "code": RemoteKeys.returnKey, "down": true, "mods": 0])
        session.input(["t": "key", "code": RemoteKeys.returnKey, "down": false, "mods": 0])
    }
}

private struct CanvasView: UIViewRepresentable {
    let canvas: RemoteCanvas
    func makeUIView(context: Context) -> RemoteCanvas { canvas }
    func updateUIView(_ v: RemoteCanvas, context: Context) {}
}

/// The Mac's screen, and every way of touching it:
/// tap = click, hold or two-finger tap = right click, one finger drag = drag, two fingers = scroll, pinch = zoom in on part of it;
/// in trackpad mode one finger pushes the pointer instead and a tap clicks where it is;
/// a trackpad or mouse moves the pointer, clicks, right-clicks and scrolls as it would on the Mac; keys go through as keys.
final class RemoteCanvas: UIView, UIGestureRecognizerDelegate {
    var videoSize = CGSize.zero
    /// One finger pushes the pointer, as on a trackpad, instead of touching where it lands.
    var trackpad = false
    private let session: RemoteSession
    private let keyField = KeyField()
    private var held = false
    private var cursor = CGPoint(x: 0.5, y: 0.5)   // the pointer, as fractions of the Mac's screen
    private var zoom: CGFloat = 1
    private var pan = CGPoint.zero
    private var pinchAt = CGPoint.zero
    private var pinching = false
    private var pinch: UIPinchGestureRecognizer!
    private var scroll: UIPanGestureRecognizer!
    private var lastTouch = UITouch.TouchType.direct   // a finger, or the trackpad's click: trackpad mode only takes over fingers

    override func touchesBegan(_ touches: Set<UITouch>, with event: UIEvent?) {
        if let t = touches.first { lastTouch = t.type }
        super.touchesBegan(touches, with: event)
    }

    init(session: RemoteSession) {
        self.session = session
        super.init(frame: .zero)
        backgroundColor = .black
        clipsToBounds = true
        layer.addSublayer(session.layer)
        keyField.canvas = self
        addSubview(keyField)

        let direct = [NSNumber(value: UITouch.TouchType.direct.rawValue)]
        let anyPointer = [NSNumber(value: UITouch.TouchType.direct.rawValue), NSNumber(value: UITouch.TouchType.indirectPointer.rawValue)]

        let tap = UITapGestureRecognizer(target: self, action: #selector(tapped))
        tap.allowedTouchTypes = anyPointer
        let rightTap = UITapGestureRecognizer(target: self, action: #selector(rightTapped))
        rightTap.buttonMaskRequired = .secondary
        // a finger has no buttons: left to itself this recogniser took plain taps and turned them into right clicks
        rightTap.allowedTouchTypes = [NSNumber(value: UITouch.TouchType.indirectPointer.rawValue)]
        let twoTap = UITapGestureRecognizer(target: self, action: #selector(twoTapped))
        twoTap.numberOfTouchesRequired = 2
        twoTap.allowedTouchTypes = direct
        let hold = UILongPressGestureRecognizer(target: self, action: #selector(holding))
        hold.allowedTouchTypes = direct
        let drag = UIPanGestureRecognizer(target: self, action: #selector(dragging))
        drag.maximumNumberOfTouches = 1
        drag.allowedTouchTypes = anyPointer
        drag.allowedScrollTypesMask = []
        scroll = UIPanGestureRecognizer(target: self, action: #selector(scrolling))
        scroll.minimumNumberOfTouches = 2
        scroll.maximumNumberOfTouches = 2
        scroll.allowedTouchTypes = direct
        scroll.allowedScrollTypesMask = .all
        scroll.delegate = self
        pinch = UIPinchGestureRecognizer(target: self, action: #selector(pinched))
        pinch.allowedTouchTypes = direct
        pinch.delegate = self
        let swipe = UIPanGestureRecognizer(target: self, action: #selector(swiped))
        swipe.minimumNumberOfTouches = 3
        swipe.allowedTouchTypes = direct
        let hover = UIHoverGestureRecognizer(target: self, action: #selector(hovering))
        [tap, rightTap, twoTap, hold, drag, scroll, pinch, swipe, hover].forEach(addGestureRecognizer)
    }

    required init?(coder: NSCoder) { fatalError() }

    // iPadOS reads three fingers left and right as undo and redo, which swallowed the desktop swipes
    override var editingInteractionConfiguration: UIEditingInteractionConfiguration { .none }

    // a pinch is two fingers: with three it took the swipe and zoomed instead
    override func gestureRecognizerShouldBegin(_ g: UIGestureRecognizer) -> Bool {
        g === pinch ? g.numberOfTouches == 2 : super.gestureRecognizerShouldBegin(g)
    }

    // two fingers can scroll and pinch at once; the scroll stands still while the pinch moves the picture
    func gestureRecognizer(_ a: UIGestureRecognizer, shouldRecognizeSimultaneouslyWith b: UIGestureRecognizer) -> Bool {
        (a === pinch && b === scroll) || (a === scroll && b === pinch)
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        CATransaction.begin(); CATransaction.setDisableActions(true)
        session.layer.frame = bounds
        CATransaction.commit()
    }

    override var canBecomeFirstResponder: Bool { true }
    override func didMoveToWindow() {
        super.didMoveToWindow()
        if window != nil { becomeFirstResponder() }
    }

    func toggleKeyboard() {
        if keyField.isFirstResponder { becomeFirstResponder() } else { keyField.becomeFirstResponder() }
    }

    // MARK: pointer

    private var videoRect: CGRect { videoSize.width > 0 ? AVMakeRect(aspectRatio: videoSize, insideRect: bounds) : .zero }

    /// A point on this view, as fractions of the Mac's screen, through the zoom.
    private func fraction(_ p: CGPoint) -> CGPoint? {
        let r = videoRect, c = CGPoint(x: bounds.midX, y: bounds.midY)
        guard r.width > 0 else { return nil }
        let q = CGPoint(x: (p.x - c.x - pan.x) / zoom + c.x, y: (p.y - c.y - pan.y) / zoom + c.y)
        return CGPoint(x: min(max((q.x - r.minX) / r.width, 0), 1), y: min(max((q.y - r.minY) / r.height, 0), 1))
    }

    private func send(_ t: String, _ f: CGPoint, button: Int = 0) {
        cursor = f
        session.input(["t": t, "x": Double(f.x), "y": Double(f.y), "b": button])
    }

    private func mouse(_ t: String, _ p: CGPoint, button: Int = 0) {
        if let f = fraction(p) { send(t, f, button: button) }
    }

    private func click(_ f: CGPoint, button: Int) {
        send("move", f); send("down", f, button: button); send("up", f, button: button)
    }

    /// Where a touch acts: where it lands, or in trackpad mode wherever the pointer already is.
    private func target(_ g: UIGestureRecognizer) -> CGPoint? {
        trackpad && lastTouch == .direct ? cursor : fraction(g.location(in: self))
    }

    @objc private func tapped(_ g: UITapGestureRecognizer) { if let f = target(g) { click(f, button: 0) } }
    @objc private func rightTapped(_ g: UITapGestureRecognizer) { if let f = fraction(g.location(in: self)) { click(f, button: 1) } }
    @objc private func twoTapped(_ g: UITapGestureRecognizer) { if let f = target(g) { click(f, button: 1) } }
    @objc private func holding(_ g: UILongPressGestureRecognizer) { if g.state == .began, let f = target(g) { click(f, button: 1) } }
    @objc private func hovering(_ g: UIHoverGestureRecognizer) { if !held { mouse("move", g.location(in: self)) } }

    @objc private func dragging(_ g: UIPanGestureRecognizer) {
        if trackpad && lastTouch == .direct { return push(g) }
        let p = g.location(in: self)
        switch g.state {
        case .began:
            // the press lands where the finger first touched, not where the pan was recognised
            let start = CGPoint(x: p.x - g.translation(in: self).x, y: p.y - g.translation(in: self).y)
            held = true
            mouse("move", start); mouse("down", start); mouse("move", p)
        case .changed: mouse("move", p)
        default: held = false; mouse("up", p)
        }
    }

    /// Trackpad mode: the finger's travel moves the pointer, faster than the finger so one stroke crosses the screen.
    private func push(_ g: UIPanGestureRecognizer) {
        let r = videoRect
        guard g.state == .changed, r.width > 0 else { return }
        let d = g.translation(in: self)
        g.setTranslation(.zero, in: self)
        let speed: CGFloat = 1.6   // ponytail: fixed pointer speed; add acceleration if long strokes feel slow
        let f = CGPoint(x: min(max(cursor.x + d.x * speed / (r.width * zoom), 0), 1), y: min(max(cursor.y + d.y * speed / (r.height * zoom), 0), 1))
        send("move", f)
    }

    @objc private func scrolling(_ g: UIPanGestureRecognizer) {
        guard g.state == .changed, videoRect.width > 0 else { return }
        let d = g.translation(in: self)
        g.setTranslation(.zero, in: self)
        guard !pinching else { return }
        // the Mac's points per point here, its stream being at 2x
        let k = videoSize.width / 2 / (videoRect.width * zoom)
        session.input(["t": "scroll", "dx": Double(d.x * k), "dy": Double(d.y * k)])
    }

    /// Three fingers as on his Mac's trackpad: up is Mission Control, down the app's windows, left and right the next and previous desktop.
    @objc private func swiped(_ g: UIPanGestureRecognizer) {
        guard g.state == .ended else { return }
        let d = g.translation(in: self)
        guard max(abs(d.x), abs(d.y)) > 40 else { return }
        let arrow = abs(d.y) > abs(d.x) ? (d.y < 0 ? 126 : 125) : (d.x < 0 ? 124 : 123)   // ↑ ↓ → ←: swiping left brings the desktop on the right
        chord(arrow, mods: 0x40000)
    }

    /// The pointer to the bottom edge, where a hidden Dock rises.
    func showDock() { send("move", CGPoint(x: 0.5, y: 1)) }

    /// One key with modifiers held, pressed and let go.
    func chord(_ code: Int, mods: Int) {
        session.input(["t": "key", "code": code, "down": true, "mods": mods])
        session.input(["t": "key", "code": code, "down": false, "mods": mods])
    }

    /// Zoom between 1x and 4x, keeping what is under the fingers under them; back at 1x the picture fits again.
    @objc private func pinched(_ g: UIPinchGestureRecognizer) {
        let c = CGPoint(x: bounds.midX, y: bounds.midY), at = g.location(in: self)
        switch g.state {
        case .began:
            pinching = true
            pinchAt = at
        case .changed:
            // the picture point that was under the fingers, before this step
            let q = CGPoint(x: (pinchAt.x - c.x - pan.x) / zoom + c.x, y: (pinchAt.y - c.y - pan.y) / zoom + c.y)
            zoom = min(max(zoom * g.scale, 1), 4)
            g.scale = 1
            pan = zoom == 1 ? .zero : CGPoint(x: at.x - c.x - (q.x - c.x) * zoom, y: at.y - c.y - (q.y - c.y) * zoom)
            pinchAt = at
            CATransaction.begin(); CATransaction.setDisableActions(true)
            session.layer.setAffineTransform(CGAffineTransform(translationX: pan.x, y: pan.y).scaledBy(x: zoom, y: zoom))
            CATransaction.commit()
        default:
            pinching = false
        }
    }

    // MARK: keys

    /// True when every press was the Mac's to have.
    func keys(_ presses: Set<UIPress>, down: Bool) -> Bool {
        var all = true
        for press in presses {
            guard let k = press.key else { all = false; continue }
            if let code = RemoteKeys.mac(k.keyCode) {
                session.input(["t": "key", "code": code, "down": down, "mods": RemoteKeys.flags(k.modifierFlags)])
            } else if !(0xE0 ... 0xE7).contains(k.keyCode.rawValue) {
                // ponytail: a modifier pressed alone is not sent, so ⌘-click is not possible; send flagsChanged if he misses it
                all = false
            }
        }
        return all
    }

    override func pressesBegan(_ p: Set<UIPress>, with e: UIPressesEvent?) { if !keys(p, down: true) { super.pressesBegan(p, with: e) } }
    override func pressesEnded(_ p: Set<UIPress>, with e: UIPressesEvent?) { if !keys(p, down: false) { super.pressesEnded(p, with: e) } }
    override func pressesCancelled(_ p: Set<UIPress>, with e: UIPressesEvent?) { if !keys(p, down: false) { super.pressesCancelled(p, with: e) } }

    fileprivate func typed(_ text: String) {
        if text == "\n" { tapKey(RemoteKeys.returnKey) } else { session.input(["t": "text", "s": text]) }
    }

    fileprivate func tapKey(_ code: Int) {
        session.input(["t": "key", "code": code, "down": true, "mods": 0])
        session.input(["t": "key", "code": code, "down": false, "mods": 0])
    }
}

/// The on-screen keyboard's target: what it types goes to the Mac as text.
/// ponytail: plain UIKeyInput, so the iPad's own Chinese input cannot compose here; a hardware keyboard drives the Mac's input method instead
private final class KeyField: UIView, UIKeyInput {
    weak var canvas: RemoteCanvas?
    var autocorrectionType = UITextAutocorrectionType.no
    var autocapitalizationType = UITextAutocapitalizationType.none
    var spellCheckingType = UITextSpellCheckingType.no
    var smartQuotesType = UITextSmartQuotesType.no
    var smartDashesType = UITextSmartDashesType.no

    override var canBecomeFirstResponder: Bool { true }
    override var editingInteractionConfiguration: UIEditingInteractionConfiguration { .none }
    var hasText: Bool { true }
    func insertText(_ text: String) { canvas?.typed(text) }
    func deleteBackward() { canvas?.tapKey(RemoteKeys.delete) }

    override func pressesBegan(_ p: Set<UIPress>, with e: UIPressesEvent?) { if canvas?.keys(p, down: true) != true { super.pressesBegan(p, with: e) } }
    override func pressesEnded(_ p: Set<UIPress>, with e: UIPressesEvent?) { if canvas?.keys(p, down: false) != true { super.pressesEnded(p, with: e) } }
}
