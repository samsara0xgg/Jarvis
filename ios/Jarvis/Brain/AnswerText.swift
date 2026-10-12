import Foundation

/// Her words as the brain writes them: every response row carries `<voice>…</voice><document>…</document>`, a lead-in line only `<voice>`,
/// an untagged answer counts as both (ADR 0040). The Mac companion's `visible()` and `spoken()` (desktop/resonance/src/model.ts, Companion.tsx), as the phone reads them.
enum AnswerText {
    /// What to show: a document is the whole answer and the voice only its spoken form, so once one is there show it alone; drop the tags and any half-streamed one.
    static func visible(_ reply: String) -> String {
        let from = reply.range(of: "<document>").map { String(reply[$0.lowerBound...]) } ?? reply
        return from
            .replacingOccurrences(of: "</voice>", with: "\n")
            .replacingOccurrences(of: "</?(voice|document)>", with: "", options: .regularExpression)
            .replacingOccurrences(of: "</?[a-z]*\\z", with: "", options: .regularExpression)
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// What she says aloud: the `<voice>` region when there is one, else the whole text; plain words, without markdown emphasis and code ticks.
    static func spoken(_ reply: String) -> String {
        var region = reply
        if let open = reply.range(of: "<voice>") {
            let rest = reply[open.upperBound...]
            region = String(rest[..<(rest.range(of: "</voice>")?.lowerBound ?? rest.endIndex)])
        }
        return visible(region)
            .replacingOccurrences(of: "\\[([^\\]]*)\\]\\([^)]*\\)", with: "$1", options: .regularExpression)             // a link says its title
            .replacingOccurrences(of: "https?://[!-~]*[A-Za-z0-9/_#=&%~-]", with: "", options: .regularExpression)         // a bare address is not read out
            .replacingOccurrences(of: "\\*\\*|`", with: "", options: .regularExpression)
    }

    /// Her words with inline marks read (`**bold**`, `[title](url)`) and every address marked as a link: the ones written as links, and bare `http(s)://` or `www.` ones
    /// the system finds (a file name like `main.py` is not an address), which show as their host. Code ticks keep their text as it is.
    static func linked(_ s: String) -> AttributedString {
        var a = (try? AttributedString(markdown: s, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))) ?? AttributedString(s)
        // the parser finds bare addresses too, and reads on through Chinese words as if they were part of one: those links are found again below
        for r in Array(a.runs) {
            guard let u = r.link?.absoluteString.removingPercentEncoding else { continue }
            let t = String(a[r.range].characters)
            if u == t || u == "http://" + t { a[r.range].link = nil }
        }
        let plain = String(a.characters)
        let found = (try? NSDataDetector(types: NSTextCheckingResult.CheckingType.link.rawValue))?.matches(in: plain, range: NSRange(plain.startIndex..., in: plain)) ?? []
        for m in found.reversed() {          // last first: a shorter label moves what comes after it
            // an address ends at the first character that is not ASCII
            guard let r = Range(m.range, in: plain), plain[r].contains("://") || plain[r].hasPrefix("www.") else { continue }
            let text = plain[r].prefix { $0.isASCII }.trimmingCharacters(in: CharacterSet(charactersIn: ".,;:!?)"))
            guard let url = URL(string: text.contains("://") ? text : "http://" + text),
                  let lo = AttributedString.Index(r.lowerBound, within: a), let hi = AttributedString.Index(plain.index(r.lowerBound, offsetBy: text.count), within: a),
                  !a[lo..<hi].runs.contains(where: { $0.link != nil || $0.inlinePresentationIntent?.contains(.code) == true }) else { continue }
            // a bare address shows as its host (a long one would wrap in the middle of a word) and opens the whole address
            var piece = AttributedString(host(url) ?? text)
            if let first = a[lo..<hi].runs.first { piece.mergeAttributes(first.attributes) }
            piece.link = url
            a.replaceSubrange(lo..<hi, with: piece)
        }
        return a
    }

    /// the address's host without `www.`, which is what a bare address shows as
    private static func host(_ url: URL) -> String? {
        guard let h = url.host, !h.isEmpty else { return nil }
        return h.hasPrefix("www.") ? String(h.dropFirst(4)) : h
    }

    /// A line before the answer: it has a voice part and no document, so more follows.
    static func isLeadIn(_ reply: String) -> Bool { reply.contains("<voice>") && !reply.contains("<document>") }
}

/// One block of a written answer.
enum AnswerBlock: Equatable {
    struct Item: Equatable { let marker: String; let text: String }
    case paragraph(String)
    case heading(String)
    case list([Item])                   // marker `•` or the number as written (`1.`)
    case code(String)
    case table([[String]])              // the header first; the `---` row is dropped
}

/// A written answer is markdown, but only as far as the brain writes it: paragraphs, `#` headings, `-` `*` `•` and `1.` lists, fenced code and `|` tables.
/// Inline bold, italic, code and links are left to `AttributedString(markdown:)`.
enum AnswerBlocks {
    static func parse(_ text: String) -> [AnswerBlock] {
        var out: [AnswerBlock] = []
        var para: [String] = [], list: [AnswerBlock.Item] = [], table: [[String]] = []
        func flush() {
            if !para.isEmpty { out.append(.paragraph(para.joined(separator: "\n"))); para = [] }
            if !list.isEmpty { out.append(.list(list)); list = [] }
            if !table.isEmpty { out.append(.table(table)); table = [] }
        }
        var code: [String]?
        for raw in text.components(separatedBy: "\n") {
            let line = raw.trimmingCharacters(in: CharacterSet(charactersIn: "\r"))
            let t = line.trimmingCharacters(in: .whitespaces)
            if t.hasPrefix("```") {
                if let c = code { out.append(.code(c.joined(separator: "\n"))); code = nil } else { flush(); code = [] }
                continue
            }
            if code != nil { code!.append(line); continue }
            if t.isEmpty { flush(); continue }
            if let h = t.firstMatch(#"^#{1,6}\s+(.*)$"#) { flush(); out.append(.heading(h[1])); continue }
            if t.hasPrefix("|") {
                if table.isEmpty { flush() }
                var cells = t.split(separator: "|", omittingEmptySubsequences: false).map { $0.trimmingCharacters(in: .whitespaces) }
                if cells.first == "" { cells.removeFirst() }
                if cells.last == "" { cells.removeLast() }
                if !cells.isEmpty && cells.allSatisfy({ $0.firstMatch(#"^:?-{2,}:?$"#) != nil }) { continue }
                table.append(cells)
                continue
            }
            if let m = t.firstMatch(#"^[-*•]\s+(.*)$"#) { if list.isEmpty { flush() }; list.append(.init(marker: "•", text: m[1])); continue }
            if let m = t.firstMatch(#"^(\d+[.)])\s+(.*)$"#) { if list.isEmpty { flush() }; list.append(.init(marker: m[1], text: m[2])); continue }
            if !list.isEmpty && raw.hasPrefix(" ") { list[list.count - 1] = .init(marker: list.last!.marker, text: list.last!.text + "\n" + t); continue }
            if !list.isEmpty || !table.isEmpty { flush() }
            para.append(t)
        }
        if let c = code { out.append(.code(c.joined(separator: "\n"))) }       // a fence still open: the answer is still arriving
        flush()
        return out
    }

    /// something besides plain paragraphs: it wants room, so the talk panel opens for it
    static func isRich(_ text: String) -> Bool {
        parse(text).contains { if case .paragraph = $0 { false } else { true } }
    }
}

private extension String {
    /// the groups of the first match of `pattern` (0 is all of it), or nil
    func firstMatch(_ pattern: String) -> [String]? {
        guard let re = try? NSRegularExpression(pattern: pattern), let m = re.firstMatch(in: self, range: NSRange(startIndex..., in: self)) else { return nil }
        return (0..<m.numberOfRanges).map { i in Range(m.range(at: i), in: self).map { String(self[$0]) } ?? "" }
    }
}
