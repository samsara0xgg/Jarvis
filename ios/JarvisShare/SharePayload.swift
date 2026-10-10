import Foundation

// What the share sheet hands over and what `POST /inherent/share` takes (docs/spec.html: url, text or attachments, one of them; title only with a url;
// url up to 2000 characters, title 300, text 4000, note 1000; ask true makes it a turn). Compiled into the extension and the tests.

struct SharedItem: Equatable {
    var url: URL?
    var text: String?
    var title: String?

    /// A text that is just one web address is that address.
    init(url: URL? = nil, text: String? = nil, title: String? = nil) {
        self.title = title
        let t = text?.trimmingCharacters(in: .whitespacesAndNewlines)
        if url == nil, let t, !t.contains(where: \.isWhitespace), let u = URL(string: t), SharePayload.isWebAddress(u) {
            self.url = u; self.text = nil
        } else {
            self.url = url; self.text = t
        }
    }
}

enum SharePayload {
    static func isWebAddress(_ u: URL) -> Bool { ["http", "https"].contains(u.scheme?.lowercased()) && u.host != nil }

    private static func clip(_ s: String, _ n: Int) -> String { String(s.prefix(n)) }

    /// The request body for the item and the owner's note, or nil when there is nothing to send.
    /// An address the brain would refuse (not http(s), over 2000 characters) goes as text instead, and what is over a limit is cut rather than refused.
    static func body(_ item: SharedItem, note: String, ask: Bool) -> [String: Any]? {
        var body: [String: Any] = [:]
        if let u = item.url, isWebAddress(u), u.absoluteString.count <= 2000 {
            body["url"] = u.absoluteString
            if let title = item.title?.trimmingCharacters(in: .whitespacesAndNewlines), !title.isEmpty { body["title"] = clip(title, 300) }
        } else if let text = (item.text ?? item.url?.absoluteString)?.trimmingCharacters(in: .whitespacesAndNewlines), !text.isEmpty {
            body["text"] = clip(text, 4000)
        } else {
            return nil
        }
        let note = note.trimmingCharacters(in: .whitespacesAndNewlines)
        if !note.isEmpty { body["note"] = clip(note, 1000) }
        if ask { body["ask"] = true }
        return body
    }

    /// what the owner is told when the brain did not take it, by the status it answered with
    static func failure(status: Int) -> String {
        switch status {
        case 413: "内容太长，大脑没收"
        case 400, 422: "大脑没收这个内容"
        default: "大脑没有答应，稍后再试"
        }
    }

    static let unpaired = "还没连上大脑，先在 Jarvis 里配对"
}
