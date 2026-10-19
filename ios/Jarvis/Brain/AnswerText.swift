import Foundation

/// Her words as the brain writes them: every response row carries `<voice>…</voice><document>…</document>`, a lead-in line only `<voice>`,
/// an untagged answer counts as both (ADR 0040). The Mac companion's `visible()` and `spoken()` (desktop/resonance/src/model.ts, Companion.tsx), as the phone reads them.
enum AnswerText {
    /// What to show: a document is the whole answer and the voice only its spoken form, so once one is there show it alone; drop the tags and any half-streamed one.
    static func visible(_ text: String) -> String {
        let reply = withoutHTML(text)
        let from = reply.range(of: "<document>").map { String(reply[$0.lowerBound...]) } ?? reply
        return from
            .replacingOccurrences(of: "</voice>", with: "\n")
            .replacingOccurrences(of: "</?(voice|document)>", with: "", options: .regularExpression)
            .replacingOccurrences(of: "</?[a-z]*\\z", with: "", options: .regularExpression)
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// Stray HTML she wrote (this app shows none): `<br>` is a line break and the other common tags go, their text stays; code in backticks is left as it is.
    /// The same rule as the brain's `jarvis/shared/html_tags.py`, for rows written before the brain applied it.
    static func withoutHTML(_ s: String) -> String {
        guard s.contains("<") else { return s }
        let names = "p|b|i|u|em|strong|span|div|ul|ol|li|h[1-6]|a|hr|font"
        let pattern = "(```.*?```|`[^`\\n]*`)|(<br(?:\\s[^<>\\n]*)?/?>)|(</?(?:\(names))(?:\\s[^<>\\n]*)?/?>)"
        guard let re = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive, .dotMatchesLineSeparators]) else { return s }
        var out = s
        for m in re.matches(in: s, range: NSRange(s.startIndex..., in: s)).reversed() {
            guard m.range(at: 1).location == NSNotFound, let r = Range(m.range, in: out) else { continue }
            out.replaceSubrange(r, with: m.range(at: 2).location == NSNotFound ? "" : "\n")
        }
        return out
    }

    /// the `<voice>` region when there is one, else the whole text
    private static func voicePart(_ reply: String) -> String {
        guard let open = reply.range(of: "<voice>") else { return reply }
        let rest = reply[open.upperBound...]
        return String(rest[..<(rest.range(of: "</voice>")?.lowerBound ?? rest.endIndex)])
    }

    /// What she says aloud: the `<voice>` region when there is one, else the whole text; plain words, without markdown emphasis and code ticks.
    static func spoken(_ reply: String) -> String {
        visible(voicePart(reply))
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

    /// `s` as plain text, for copying: the marks are gone; a link keeps its address after its title, and a bare address (shown as its host) is the whole address
    static func plain(_ s: String) -> String {
        let a = linked(s)
        return a.runs.map { r in
            let t = String(a[r.range].characters)
            guard let url = r.link else { return t }
            let u = url.absoluteString
            return t == u || t == host(url) ? u : "\(t) (\(u))"
        }.joined()
    }

    /// the address's host without `www.`, which is what a bare address shows as
    private static func host(_ url: URL) -> String? {
        guard let h = url.host, !h.isEmpty else { return nil }
        return h.hasPrefix("www.") ? String(h.dropFirst(4)) : h
    }

    /// What is written says more than she says aloud: there is a document, and it is not her voice part again (whitespace aside). An answer with no document, or a line before one, is said whole.
    static func saysMore(_ reply: String) -> Bool {
        guard reply.contains("<document>") else { return false }
        func bare(_ s: String) -> String { s.filter { !$0.isWhitespace } }
        return bare(visible(voicePart(reply))) != bare(visible(reply))
    }

    /// A line before the answer: it has a voice part and no document, so more follows.
    static func isLeadIn(_ reply: String) -> Bool { reply.contains("<voice>") && !reply.contains("<document>") }
}

/// One block of a written answer.
enum AnswerBlock: Equatable {
    struct Item: Equatable { let marker: String; let text: String; var level = 0 }        // `level`: how far it is nested under the item before it
    case paragraph(String)
    case heading(String)
    case list([Item])                   // marker `•` or the number as written (`1.`)
    case code(String)
    case table([[String]])              // the header first; the `---` row is dropped
    case quote(String)                  // `>` lines, the marks dropped
    case rule                           // `---`, `***` or `___`
}

/// A written answer is markdown, but only as far as the brain writes it: paragraphs, `#` headings, `-` `*` `•` and `1.` lists (nested by their indent), `>` quotes,
/// rules, fenced code and `|` tables.
/// Inline bold, italic, code and links are left to `AttributedString(markdown:)`.
enum AnswerBlocks {
    static func parse(_ text: String) -> [AnswerBlock] {
        var out: [AnswerBlock] = []
        var para: [String] = [], list: [AnswerBlock.Item] = [], table: [[String]] = [], quote: [String] = []
        var indents: [Int] = []             // the indents of the list levels open now
        /// the nesting of an item indented by `indent`: one level more than the nearest shallower item, whatever the step (2 spaces, 4, a tab)
        func level(_ indent: Int) -> Int {
            while let last = indents.last, last > indent { indents.removeLast() }
            if indents.last != indent { indents.append(indent) }
            return indents.count - 1
        }
        func flush() {
            if !para.isEmpty { out.append(.paragraph(para.joined(separator: "\n"))); para = [] }
            if !list.isEmpty { out.append(.list(list)); list = [] }
            if !table.isEmpty { out.append(.table(table)); table = [] }
            if !quote.isEmpty { out.append(.quote(quote.joined(separator: "\n"))); quote = [] }
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
            if t.firstMatch(#"^([-*_])(\s*\1){2,}$"#) != nil { flush(); out.append(.rule); continue }
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
            if let m = t.firstMatch(#"^>+\s?(.*)$"#) { if quote.isEmpty { flush() }; quote.append(m[1]); continue }
            let indent = line.prefix { $0 == " " || $0 == "\t" }.reduce(0) { $0 + ($1 == "\t" ? 4 : 1) }
            if let m = t.firstMatch(#"^[-*•]\s+(.*)$"#) { if list.isEmpty { flush(); indents = [] }; list.append(.init(marker: "•", text: m[1], level: level(indent))); continue }
            if let m = t.firstMatch(#"^(\d+[.)])\s+(.*)$"#) { if list.isEmpty { flush(); indents = [] }; list.append(.init(marker: m[1], text: m[2], level: level(indent))); continue }
            if !list.isEmpty && raw.hasPrefix(" ") { list[list.count - 1] = .init(marker: list.last!.marker, text: list.last!.text + "\n" + t, level: list.last!.level); continue }
            if !list.isEmpty || !table.isEmpty || !quote.isEmpty { flush() }
            para.append(t)
        }
        if let c = code { out.append(.code(c.joined(separator: "\n"))) }       // a fence still open: the answer is still arriving
        flush()
        return out
    }

    /// What the blocks show, as plain text for copying: no markup, a list keeps its markers and nesting, a table its cells apart by tabs.
    static func plain(_ text: String) -> String {
        parse(text).map { b -> String in
            switch b {
            case .paragraph(let s), .heading(let s), .quote(let s): AnswerText.plain(s)
            case .list(let items): items.map { String(repeating: "  ", count: $0.level) + $0.marker + " " + AnswerText.plain($0.text) }.joined(separator: "\n")
            case .code(let s): s
            case .table(let rows): rows.map { $0.map(AnswerText.plain).joined(separator: "\t") }.joined(separator: "\n")
            case .rule: "---"
            }
        }.joined(separator: "\n\n")
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
