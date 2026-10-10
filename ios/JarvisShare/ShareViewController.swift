import SwiftUI
import UIKit
import UniformTypeIdentifiers

/// The share sheet's entry: finds the URL or the text in what was shared, and hosts the small sheet that sends it.
final class ShareViewController: UIViewController {
    override func viewDidLoad() {
        super.viewDidLoad()
        let model = ShareModel { [weak self] in self?.extensionContext?.completeRequest(returningItems: nil) } cancel: { [weak self] in
            self?.extensionContext?.cancelRequest(withError: NSError(domain: NSCocoaErrorDomain, code: NSUserCancelledError))
        }
        let host = UIHostingController(rootView: ShareView(model: model))
        host.view.backgroundColor = .clear
        addChild(host)
        host.view.frame = view.bounds
        host.view.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        view.addSubview(host.view)
        host.didMove(toParent: self)
        let items = (extensionContext?.inputItems as? [NSExtensionItem]) ?? []
        Task { model.item = await Self.sharedItem(in: items) }
    }

    /// the first URL among what was shared, else the first text; the page's title when the sender gave one
    private static func sharedItem(in items: [NSExtensionItem]) async -> SharedItem? {
        let providers = items.flatMap { $0.attachments ?? [] }
        let title = items.lazy.compactMap { ($0.attributedTitle ?? $0.attributedContentText)?.string }.first { !$0.isEmpty }
        if let p = providers.first(where: { $0.hasItemConformingToTypeIdentifier(UTType.url.identifier) }),
           let u = try? await p.loadItem(forTypeIdentifier: UTType.url.identifier) as? URL {
            return SharedItem(url: u, title: title)
        }
        if let p = providers.first(where: { $0.hasItemConformingToTypeIdentifier(UTType.plainText.identifier) }),
           let t = try? await p.loadItem(forTypeIdentifier: UTType.plainText.identifier) as? String {
            return SharedItem(text: t)
        }
        return nil
    }
}
