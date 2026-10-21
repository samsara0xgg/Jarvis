// words: legacy
import SwiftUI
import UIKit
import UniformTypeIdentifiers

// The composer's words. One line to start, growing to five as you type (the box grows with it), and pictures or files
// pasted into it join the strip while words paste as words.

struct AttachField: View {
    static let minH = 48.0
    static let maxLines = 5.0
    static let font = UIFont.systemFont(ofSize: 16)
    static var inset: CGFloat { (CGFloat(minH) - font.lineHeight) / 2 }            // one line is exactly 48 tall
    static var maxH: Double { Double(2 * inset + CGFloat(maxLines) * font.lineHeight) }
    /// What the brain takes of typed words: 20,000 characters in a `say` over /phone/ws (jarvis/surface/terminal_events.py, counted in code points); POST /inherent/submit has no lower limit.
    static let maxChars = 20_000
    /// `t` cut to what the brain takes, or nil when it fits
    static func clip(_ t: String) -> String? {
        t.unicodeScalars.count > maxChars ? String(String.UnicodeScalarView(t.unicodeScalars.prefix(maxChars))) : nil
    }
    static let textWidth = W - 40 - 52 - 56                                         // the box, less the + and the send button

    @Binding var text: String
    @Binding var focused: Bool
    let model: AttachModel
    let send: () -> Void

    var body: some View {
        ZStack(alignment: .bottomLeading) {
            AttachTextView(text: $text, focused: $focused, model: model, send: send)
            if text.isEmpty {
                Text("跟她说…").font(.system(size: 16)).foregroundStyle(Color(red: 196 / 255, green: 204 / 255, blue: 238 / 255).opacity(0.5))
                    .frame(height: Self.minH, alignment: .leading).allowsHitTesting(false)
            }
        }
        .frame(width: Self.textWidth, height: model.fieldH)
        .padding(.leading, 52).padding(.trailing, 56)
        .onGeometryChange(for: CGRect.self) { $0.frame(in: .named("design")) } action: { model.frames.rects["field"] = $0 }
        #if DEBUG
        .onAppear { if let t = UserDefaults.standard.string(forKey: "attachText") { text = t } }
        .onChange(of: model.debugSend) { _, _ in send() }
        #endif
    }
}

struct AttachTextView: UIViewRepresentable {
    @Binding var text: String
    @Binding var focused: Bool
    let model: AttachModel
    let send: () -> Void

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeUIView(context: Context) -> FieldView {
        let v = FieldView()
        v.delegate = context.coordinator
        v.font = AttachField.font
        v.textColor = UIColor(red: 238 / 255, green: 240 / 255, blue: 251 / 255, alpha: 1)
        v.tintColor = UIColor(red: 157 / 255, green: 180 / 255, blue: 1, alpha: 1)
        v.backgroundColor = .clear
        v.textContainerInset = UIEdgeInsets(top: AttachField.inset, left: 0, bottom: AttachField.inset, right: 0)
        v.textContainer.lineFragmentPadding = 0
        v.showsVerticalScrollIndicator = false
        v.isScrollEnabled = false
        v.returnKeyType = .send
        v.autocapitalizationType = .none
        v.autocorrectionType = .no
        v.accessibilityLabel = "跟她说"
        v.onPaste = { [model] providers, insert in model.paste(providers, insert: insert) }
        let tap = UITapGestureRecognizer(target: context.coordinator, action: #selector(Coordinator.tapped))
        tap.cancelsTouchesInView = false; tap.delegate = context.coordinator
        v.addGestureRecognizer(tap)
        return v
    }

    /// the frame decides the size: the box is as tall as the lines need (`AttachModel.fieldH`), the text wraps inside it
    func sizeThatFits(_ proposal: ProposedViewSize, uiView: FieldView, context: Context) -> CGSize? {
        CGSize(width: proposal.width ?? AttachField.textWidth, height: proposal.height ?? AttachField.minH)
    }

    func updateUIView(_ v: FieldView, context: Context) {
        context.coordinator.parent = self
        if v.text != text { v.text = text; context.coordinator.measure(v) }
        // the keyboard follows `focused`: the engine drops it when she answers or the sky is touched
        if focused && !v.isFirstResponder { DispatchQueue.main.async { v.becomeFirstResponder() } }
        else if !focused && v.isFirstResponder { DispatchQueue.main.async { v.resignFirstResponder() } }
    }

    final class Coordinator: NSObject, UITextViewDelegate, UIGestureRecognizerDelegate {
        var parent: AttachTextView
        init(_ p: AttachTextView) { parent = p }

        func textViewDidChange(_ tv: UITextView) { parent.text = tv.text; measure(tv) }
        func textViewDidBeginEditing(_ tv: UITextView) { parent.focused = true }
        func textViewDidEndEditing(_ tv: UITextView) { parent.focused = false }

        func textView(_ tv: UITextView, shouldChangeTextIn range: NSRange, replacementText t: String) -> Bool {
            guard t == "\n" else { return true }
            parent.send()                                  // return sends, as it did; words wrap onto more lines by themselves
            return false
        }

        /// tapping the words puts the panel away
        @objc func tapped() { parent.model.close() }
        func gestureRecognizer(_ g: UIGestureRecognizer, shouldRecognizeSimultaneouslyWith o: UIGestureRecognizer) -> Bool { true }

        /// the box follows the number of lines, one to five
        func measure(_ tv: UITextView) {
            let w = tv.bounds.width > 0 ? tv.bounds.width : AttachField.textWidth        // the phone's box is textWidth wide; the iPad's follows its band
            let h = ceil(tv.sizeThatFits(CGSize(width: w, height: .greatestFiniteMagnitude)).height)
            tv.isScrollEnabled = h > AttachField.maxH
            let c = min(max(Double(h), AttachField.minH), AttachField.maxH), model = parent.model
            if c != model.fieldH { DispatchQueue.main.async { withAnimation(.easeOut(duration: 0.2)) { model.fieldH = c } } }
        }
    }

    /// a text view that treats a pasted picture or file as an attachment
    final class FieldView: UITextView {
        var onPaste: (([NSItemProvider], @escaping (String) -> Void) -> Void)?

        init() {
            super.init(frame: .zero, textContainer: nil)
            pasteConfiguration = UIPasteConfiguration(acceptableTypeIdentifiers: [UTType.image.identifier, UTType.item.identifier])
        }
        required init?(coder: NSCoder) { fatalError() }

        private func route(_ providers: [NSItemProvider]) {
            onPaste?(providers) { [weak self] s in self?.insertText(s) }
        }
        /// a text view says it cannot paste when the pasteboard holds no words, so ⌘V and the menu's Paste would never reach `paste(_:)` with only a picture or a file on it
        override func canPerformAction(_ action: Selector, withSender sender: Any?) -> Bool {
            if action == #selector(paste(_:)), UIPasteboard.general.hasImages || UIPasteboard.general.contains(pasteboardTypes: [UTType.fileURL.identifier]) { return true }
            return super.canPerformAction(action, withSender: sender)
        }
        override func paste(_ sender: Any?) {
            let providers = UIPasteboard.general.itemProviders
            if providers.contains(where: AttachModel.isAttachment) { route(providers) } else { super.paste(sender) }
        }
        override func paste(itemProviders: [NSItemProvider]) { route(itemProviders) }
    }
}
