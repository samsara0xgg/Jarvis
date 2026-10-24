// What the voice files use from app files the harness does not compile (they need UIKit or SwiftUI).

/// Sky/ChatSearch.swift's: the topic his next words go on with; the harness never follows one.
enum TopicFollow {
    nonisolated(unsafe) static var id: String?
}
