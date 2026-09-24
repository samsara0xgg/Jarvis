import Foundation

/// Joins successive overlapping OCR frames. A revised tail replaces the previous
/// tail; it is never appended as a second utterance. All state stays in memory.
public struct CaptionTracker {
    private var transcript: [String] = []
    private var previous: [String] = []
    private var previousStart = 0
    private var lastOutput = ""

    public init() {}

    public mutating func ingest(_ text: String) -> String? {
        let words = text.split(whereSeparator: \.isWhitespace).map(String.init)
        guard !words.isEmpty else { return nil }
        let incoming = Array(words.suffix(180))
        guard incoming != previous else { return nil }

        let oldKeys = previous.map(Self.key)
        let newKeys = incoming.map(Self.key)
        var best = (length: 0, old: 0, new: 0)
        // Longest contiguous anchor tolerates a clipped first line, reflow,
        // speaker changes, and corrections before or after the anchor.
        var row = [Int](repeating: 0, count: newKeys.count + 1)
        for i in oldKeys.indices {
            var next = [Int](repeating: 0, count: newKeys.count + 1)
            for j in newKeys.indices where !oldKeys[i].isEmpty && oldKeys[i] == newKeys[j] {
                next[j + 1] = row[j] + 1
                if next[j + 1] > best.length {
                    best = (next[j + 1], i - next[j + 1] + 1, j - next[j + 1] + 1)
                }
            }
            row = next
        }

        let anchorStart = previousStart + best.old - best.new
        let exactShortPrefix = min(previous.count, incoming.count) > 0
            && Array(oldKeys.prefix(min(oldKeys.count, newKeys.count)))
                == Array(newKeys.prefix(min(oldKeys.count, newKeys.count)))
        if best.length >= 3 && anchorStart < 0 {
            // A panel can reveal older lines when expanded or scrolled back.
            // Those lines precede the known text; appending duplicates it.
            transcript = incoming
            previousStart = 0
        } else if (best.length >= 3 || exactShortPrefix), anchorStart >= 0, anchorStart <= transcript.count {
            transcript = Array(transcript.prefix(anchorStart)) + incoming
            previousStart = anchorStart
        } else {
            previousStart = transcript.count
            transcript += incoming
        }
        previous = incoming
        if transcript.count > 600 {
            let drop = transcript.count - 600
            transcript.removeFirst(drop)
            previousStart -= drop
        }
        let output = Self.recentContext(transcript)
        guard output != lastOutput else { return nil }
        lastOutput = output
        return output
    }

    private static func key(_ word: String) -> String {
        word.lowercased().filter { $0.isLetter || $0.isNumber }
    }

    private static func recentContext(_ words: [String]) -> String {
        // Keep up to three sentences, including the unfinished current sentence.
        var boundaries = 0
        var start = max(0, words.count - 110)
        for i in words.indices.reversed() where i < words.count - 1 {
            let word = words[i]
            let endsSentence = word.hasSuffix("!") || word.hasSuffix("?")
                || (word.hasSuffix(".") && !word.hasSuffix("..."))
            if endsSentence {
                boundaries += 1
                if boundaries == 3 { start = max(start, i + 1); break }
            }
        }
        return words[start...].joined(separator: " ")
    }
}
