// words: legacy
import Observation
import SwiftUI

@Observable
final class ShareModel {
    enum Phase: Equatable { case editing, sending, failed(String), done(String) }

    var item: SharedItem?
    var note = ""
    var ask = false
    private(set) var phase = Phase.editing
    let close: () -> Void
    let cancel: () -> Void

    init(close: @escaping () -> Void, cancel: @escaping () -> Void) { self.close = close; self.cancel = cancel }

    var paired: Bool { BrainStore.pairing != nil }
    /// shown as soon as the sheet opens, so he knows before typing a note
    func noteUnpaired() { if !paired { phase = .failed(SharePayload.unpaired) } }

    @MainActor func send() async {
        guard let item, let body = SharePayload.body(item, note: note, ask: ask) else { phase = .failed("没有可以分享的内容"); return }
        guard let pairing = BrainStore.pairing else { phase = .failed(SharePayload.unpaired); return }
        phase = .sending
        do {
            let status = try await BrainClient.share(body, bases: pairing.bases, token: pairing.token)
            guard status == 200 else { phase = .failed(SharePayload.failure(status: status)); return }
            phase = .done(ask ? "已问她" : "她记下了")
            try? await Task.sleep(nanoseconds: 800_000_000)
            close()
        } catch let e as BrainError {
            phase = .failed(e.message)
        } catch {
            phase = .failed(BrainError.refused.message)
        }
    }
}

private enum Night {
    static let bg = Color(red: 14 / 255, green: 15 / 255, blue: 30 / 255)
    static let cell = Color(red: 28 / 255, green: 30 / 255, blue: 50 / 255)
    static let ink = Color(red: 238 / 255, green: 240 / 255, blue: 251 / 255)
    static let muted = Color(red: 196 / 255, green: 204 / 255, blue: 238 / 255).opacity(0.62)
    static let pale = Color(red: 238 / 255, green: 241 / 255, blue: 1)
    static let navy = Color(red: 11 / 255, green: 13 / 255, blue: 26 / 255)
    static let red = Color(red: 1, green: 107 / 255, blue: 107 / 255)
}

struct ShareView: View {
    @Bindable var model: ShareModel

    private var busy: Bool { model.phase == .sending }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("给 Jarvis").font(.system(size: 20, weight: .semibold)).foregroundStyle(Night.ink)
            preview
            TextField("", text: $model.note, prompt: Text("备注（可选）").foregroundStyle(Night.muted), axis: .vertical)
                .lineLimit(1...3).font(.system(size: 15)).foregroundStyle(Night.ink).tint(Night.pale)
                .padding(.horizontal, 14).padding(.vertical, 12).background(Night.cell, in: RoundedRectangle(cornerRadius: 14))
            Toggle(isOn: $model.ask) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("问她").font(.system(size: 15, weight: .medium)).foregroundStyle(Night.ink)
                    Text(model.ask ? "她会马上看，并回你" : "不打开，她先记下").font(.system(size: 12)).foregroundStyle(Night.muted)
                }
            }.tint(Night.pale)
            status
            HStack(spacing: 10) {
                Button { model.cancel() } label: { label("取消", main: false) }
                Button { Task { await model.send() } } label: { label("发送", main: true) }
                    .disabled(model.item == nil || !model.paired || busy)
                    .opacity(model.item == nil || !model.paired ? 0.4 : 1)
            }
        }
        .padding(20)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .background(Night.bg.ignoresSafeArea())
        .preferredColorScheme(.dark)
        .opacity(busy ? 0.7 : 1).disabled(busy)
        .task { model.noteUnpaired() }
    }

    @ViewBuilder private var preview: some View {
        VStack(alignment: .leading, spacing: 4) {
            if let item = model.item {
                if let url = item.url {
                    if let title = item.title, !title.isEmpty { Text(title).font(.system(size: 15, weight: .medium)).foregroundStyle(Night.ink).lineLimit(2) }
                    Text(url.absoluteString).font(.system(size: 12, design: .monospaced)).foregroundStyle(Night.muted).lineLimit(2)
                } else if let text = item.text {
                    Text(text).font(.system(size: 14)).foregroundStyle(Night.ink.opacity(0.84)).lineLimit(5)
                }
            } else {
                Text("正在读取…").font(.system(size: 14)).foregroundStyle(Night.muted)
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 12).frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.white.opacity(0.05), in: RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(Color.white.opacity(0.08), lineWidth: 0.5))
    }

    @ViewBuilder private var status: some View {
        switch model.phase {
        case .failed(let m): Text(m).font(.system(size: 13)).foregroundStyle(Night.red)
        case .done(let m): Text(m).font(.system(size: 13)).foregroundStyle(Night.pale)
        case .sending: ProgressView().tint(Night.pale)
        case .editing: EmptyView()
        }
    }

    private func label(_ text: String, main: Bool) -> some View {
        Text(text).font(.system(size: 15.5, weight: .semibold)).foregroundStyle(main ? Night.navy : Night.ink)
            .frame(maxWidth: .infinity).frame(height: 44).background(main ? Night.pale : Color.white.opacity(0.1), in: Capsule())
    }
}
