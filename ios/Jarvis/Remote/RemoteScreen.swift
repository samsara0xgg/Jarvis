import AVFoundation
import SwiftUI
import UIKit

/// The remote view: his Mac's screen edge to edge, three small buttons in the corner (unlock while it is locked, keyboard, close).
struct RemoteScreen: View {
    let close: () -> Void
    @State private var live = RemoteLive()
    @State private var asking = false
    @State private var typed = ""
    @State private var pasteFailed = false
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
                if case .live = live.state { corner("keyboard", Words.Remote.keyboard) { live.canvas.toggleKeyboard() } }
                corner("xmark", Words.Remote.close, close)
            }
            Spacer()
        }
        .padding(12)
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
/// tap = click, hold = right click, one finger drag = drag, two fingers = scroll;
/// a trackpad or mouse moves the pointer, clicks, right-clicks and scrolls as it would on the Mac; keys go through as keys.
final class RemoteCanvas: UIView {
    var videoSize = CGSize.zero
    private let session: RemoteSession
    private let keyField = KeyField()
    private var held = false

    init(session: RemoteSession) {
        self.session = session
        super.init(frame: .zero)
        backgroundColor = .black
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
        let hold = UILongPressGestureRecognizer(target: self, action: #selector(holding))
        hold.allowedTouchTypes = direct
        let drag = UIPanGestureRecognizer(target: self, action: #selector(dragging))
        drag.maximumNumberOfTouches = 1
        drag.allowedTouchTypes = anyPointer
        drag.allowedScrollTypesMask = []
        let scroll = UIPanGestureRecognizer(target: self, action: #selector(scrolling))
        scroll.minimumNumberOfTouches = 2
        scroll.allowedTouchTypes = direct
        scroll.allowedScrollTypesMask = .all
        let hover = UIHoverGestureRecognizer(target: self, action: #selector(hovering))
        [tap, rightTap, hold, drag, scroll, hover].forEach(addGestureRecognizer)
    }

    required init?(coder: NSCoder) { fatalError() }

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

    private func mouse(_ t: String, _ p: CGPoint, button: Int = 0) {
        let r = videoRect
        guard r.width > 0 else { return }
        session.input(["t": t, "x": Double((p.x - r.minX) / r.width), "y": Double((p.y - r.minY) / r.height), "b": button])
    }

    private func click(_ p: CGPoint, button: Int) {
        mouse("move", p); mouse("down", p, button: button); mouse("up", p, button: button)
    }

    @objc private func tapped(_ g: UITapGestureRecognizer) { click(g.location(in: self), button: 0) }
    @objc private func rightTapped(_ g: UITapGestureRecognizer) { click(g.location(in: self), button: 1) }
    @objc private func holding(_ g: UILongPressGestureRecognizer) { if g.state == .began { click(g.location(in: self), button: 1) } }
    @objc private func hovering(_ g: UIHoverGestureRecognizer) { if !held { mouse("move", g.location(in: self)) } }

    @objc private func dragging(_ g: UIPanGestureRecognizer) {
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

    @objc private func scrolling(_ g: UIPanGestureRecognizer) {
        guard g.state == .changed, videoRect.width > 0 else { return }
        let d = g.translation(in: self)
        g.setTranslation(.zero, in: self)
        // the Mac's points per point here, its stream being at 2x
        let k = videoSize.width / 2 / videoRect.width
        session.input(["t": "scroll", "dx": Double(d.x * k), "dy": Double(d.y * k)])
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
    var hasText: Bool { true }
    func insertText(_ text: String) { canvas?.typed(text) }
    func deleteBackward() { canvas?.tapKey(RemoteKeys.delete) }

    override func pressesBegan(_ p: Set<UIPress>, with e: UIPressesEvent?) { if canvas?.keys(p, down: true) != true { super.pressesBegan(p, with: e) } }
    override func pressesEnded(_ p: Set<UIPress>, with e: UIPressesEvent?) { if canvas?.keys(p, down: false) != true { super.pressesEnded(p, with: e) } }
}
