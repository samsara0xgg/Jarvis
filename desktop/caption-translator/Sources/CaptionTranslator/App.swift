import SwiftUI
import AppKit
import Translation

@main
struct CaptionTranslatorApp: App {
    @StateObject private var model = CaptionModel()

    var body: some Scene {
        Window("字幕翻译", id: "captions") {
            CaptionView(model: model)
                .frame(minWidth: 460, minHeight: 380)
                .background(WindowSetup(pinned: model.pinned))
                .translationTask(model.translationConfig) { session in
                    await model.translate(using: session)
                }
                .onDisappear { Task { await model.stop() } }
        }
        .defaultSize(width: 580, height: 470)
        .commands {
            CommandGroup(replacing: .newItem) {}
            CommandMenu("翻译") {
                Button("选择窗口") { model.chooseWindow() }.keyboardShortcut("o")
                Button(model.isRunning ? "暂停翻译" : "开始翻译") {
                    Task { if model.isRunning { await model.stop() } else { await model.start() } }
                }.keyboardShortcut("r").disabled(!model.hasSource || model.isBusy || model.needsRegionSelection)
                Toggle("窗口置顶", isOn: $model.pinned).keyboardShortcut("p", modifiers: [.command, .shift])
            }
        }
    }
}

struct WindowSetup: NSViewRepresentable {
    let pinned: Bool
    func makeNSView(context: Context) -> NSView { NSView() }
    func updateNSView(_ view: NSView, context: Context) {
        DispatchQueue.main.async {
            guard let window = view.window else { return }
            window.level = pinned ? .floating : .normal
            window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
            window.isMovableByWindowBackground = true
        }
    }
}

struct CaptionView: View {
    @ObservedObject var model: CaptionModel

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 8) {
                Circle().fill(model.isRunning ? Color.green : Color.secondary).frame(width: 6, height: 6)
                    .accessibilityHidden(true)
                Text(model.isPreparing ? "正在准备本地翻译…" : model.status)
                    .font(.callout).foregroundStyle(.secondary)
                Spacer()
                Text("英语 → 简体中文").font(.callout).foregroundStyle(.secondary)
            }.padding(.horizontal, 24).padding(.top, 18).padding(.bottom, 20)

            if let error = model.errorMessage {
                VStack(alignment: .leading, spacing: 8) {
                    Label("需要处理", systemImage: "exclamationmark.triangle").font(.headline)
                    Text(error).font(.callout).textSelection(.enabled)
                    if model.needsRegionSelection {
                        Button("重新框选") { Task { await model.editRegion() } }.disabled(model.isBusy)
                    } else if model.translationFailed {
                        Button("准备本地翻译") { model.prepareLanguages() }.disabled(model.isPreparing)
                    } else {
                        Button("重新选择窗口") { model.chooseWindow() }.disabled(model.isBusy)
                    }
                }
                .padding(14).frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.orange.opacity(0.10), in: RoundedRectangle(cornerRadius: 10))
                .padding(.horizontal, 24).padding(.bottom, 14)
            }

            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    if model.translatedText.isEmpty {
                        VStack(alignment: .leading, spacing: 12) {
                            Text(model.isRunning ? "正在等待第一句字幕" : "看英文字幕，读中文翻译")
                                .font(.system(size: 24, weight: .semibold))
                            Text(model.isRunning
                                 ? "保持字幕窗口打开，译文会在这里更新。"
                                 : "选择一个窗口，再框出英文字幕。中文会留在这个置顶窗口里，随原文更新。")
                                .font(.body).foregroundStyle(.secondary).lineSpacing(5)
                            if !model.languageReady && !model.isPreparing {
                                Button("准备本地翻译") { model.prepareLanguages() }.padding(.top, 4)
                                Text("首次使用可能需要下载英中语言包。")
                                    .font(.callout).foregroundStyle(.secondary)
                            }
                        }.padding(.top, 10)
                    } else {
                        Text(model.translatedText)
                            .font(.system(size: model.fontSize, weight: .medium))
                            .lineSpacing(9).textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .accessibilityLabel("中文译文")
                            .accessibilityValue(model.translatedText)
                    }
                    if model.showEnglish && !model.sourceText.isEmpty {
                        VStack(alignment: .leading, spacing: 8) {
                            Text("英文原文").font(.callout).foregroundStyle(.secondary)
                            Text(model.translatedSource.isEmpty ? model.sourceText : model.translatedSource)
                                .font(.system(size: 14)).lineSpacing(5).foregroundStyle(.secondary)
                                .textSelection(.enabled)
                        }
                    }
                }.padding(.horizontal, 24).padding(.bottom, 24).frame(maxWidth: .infinity, alignment: .leading)
            }

            Divider()
            VStack(spacing: 12) {
                HStack(spacing: 6) {
                    Image(systemName: "macwindow").foregroundStyle(.secondary)
                    Text(model.sourceName).font(.callout).lineLimit(1).truncationMode(.middle)
                    Spacer(minLength: 8)
                    if model.canEditRegion {
                        Button("重新框选") { Task { await model.editRegion() } }
                            .font(.callout).buttonStyle(.link).disabled(model.isBusy)
                    }
                }
                HStack(spacing: 10) {
                    Button(model.hasSource ? "更换窗口" : "选择窗口") { model.chooseWindow() }
                        .disabled(model.isBusy)
                    if model.hasSource {
                        Button(model.isRunning ? "暂停翻译" : "开始翻译") {
                            Task { if model.isRunning { await model.stop() } else { await model.start() } }
                        }.buttonStyle(.borderedProminent).disabled(model.isBusy || model.needsRegionSelection)
                    }
                    Spacer()
                    Toggle(isOn: $model.pinned) { Image(systemName: model.pinned ? "pin.fill" : "pin") }
                        .toggleStyle(.button).help("窗口置顶").accessibilityLabel("窗口置顶")
                    Menu {
                        Toggle("显示英文原文", isOn: $model.showEnglish)
                        Button("增大字号") { model.fontSize = min(36, model.fontSize + 2) }
                            .disabled(model.fontSize >= 36)
                        Button("减小字号") { model.fontSize = max(16, model.fontSize - 2) }
                            .disabled(model.fontSize <= 16)
                        Divider()
                        Button("复制译文") {
                            NSPasteboard.general.clearContents()
                            NSPasteboard.general.setString(model.translatedText, forType: .string)
                        }.disabled(model.translatedText.isEmpty)
                    } label: { Image(systemName: "ellipsis") }
                    .menuStyle(.borderlessButton).frame(width: 24).help("显示与复制").accessibilityLabel("显示与复制")
                }.controlSize(.large)
                HStack {
                    Text("识别与翻译均在本机完成")
                    Spacer()
                    if let ms = model.translationMilliseconds { Text("翻译用时 \(Double(ms) / 1000, specifier: "%.1f") 秒") }
                }.font(.caption).foregroundStyle(.secondary)
            }.padding(.horizontal, 24).padding(.vertical, 16)
        }
        .sheet(isPresented: $model.showingCrop) {
            if let preview = model.preview {
                RegionView(image: preview, initialRegion: model.region) { selection in
                    model.confirmRegion(selection)
                    Task { await model.start() }
                }
            }
        }
    }
}
