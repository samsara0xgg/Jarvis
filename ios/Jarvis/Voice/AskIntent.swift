import AppIntents
import SwiftUI

// 「问 Jarvis」: one question and one answer, for Siri and Shortcuts, without opening the app. Paired, it asks the brain (`POST /inherent/ask`) and says
// what she says; her written part, if she has one, is the result shown with it.

struct AskIntent: AppIntent {
    static let title: LocalizedStringResource = "问 Jarvis"
    static let description = IntentDescription("Ask Jarvis a question")
    static let openAppWhenRun = false

    @Parameter(title: "问题", requestValueDialog: "想问她什么？") var text: String

    /// what stops it, in the words Siri says
    struct Failure: Error, CustomLocalizedStringResourceConvertible {
        let words: String
        var localizedStringResource: LocalizedStringResource { "\(words)" }
    }

    @MainActor
    func perform() async throws -> some IntentResult & ReturnsValue<String> & ProvidesDialog & ShowsSnippetView {
        guard BrainLink.shared.hasToken else { throw Failure(words: "还没和大脑配对，先在 Jarvis 里扫码配对。") }
        guard let body = AskReply.body(text) else { throw Failure(words: "这个问题太长了，说短一点。") }
        let reply: AskReply
        do { reply = try await BrainLink.shared.call { try await BrainClient.ask(body, bases: $0, token: $1) } }
        catch let e as BrainError { throw Failure(words: e.message) }
        switch reply {
        case .answered(let spoken, let written):
            let said = AnswerText.spoken(spoken), shown = AnswerText.visible(written)
            let line = said.isEmpty ? AnswerText.visible(spoken) : said
            return .result(value: shown.isEmpty ? line : shown, dialog: IntentDialog(stringLiteral: line), view: AskSnippet(text: shown.isEmpty ? line : shown))
        case .working:
            let line = "她还在忙，做完会记在今天的对话里。"
            return .result(value: line, dialog: IntentDialog(stringLiteral: line), view: AskSnippet(text: line))
        case .failed:
            throw Failure(words: "这一轮没做成。")
        }
    }
}

private struct AskSnippet: View {
    let text: String
    var body: some View { Text(text).font(.body).frame(maxWidth: .infinity, alignment: .leading).padding() }
}
