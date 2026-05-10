import AppKit
import SwiftUI
import UniformTypeIdentifiers

struct NativeCardView: View {
  @ObservedObject var model: NativeCardModel

  @State private var hovering = false
  @State private var pillHovering = false
  @State private var dragStart: CGPoint?
  @State private var lastDragTranslation: CGSize = .zero
  @FocusState private var inputFocused: Bool

  var body: some View {
    ZStack(alignment: .topTrailing) {
      popoverLayer
      cardColumn
        .frame(width: NativeCardModel.cardWidth, alignment: .topTrailing)
        .onHover { value in
          hovering = value
          model.setHovering(value)
        }
    }
    .frame(width: NativeCardModel.panelWidth, alignment: .topTrailing)
    .background(Color.clear)
    .onChange(of: model.focusNonce) { _, _ in
      inputFocused = true
    }
    .onKeyPress(.escape) {
      model.handleEscape()
      return .handled
    }
    .onDrop(
      of: [UTType.fileURL.identifier, UTType.image.identifier, UTType.png.identifier, UTType.tiff.identifier],
      isTargeted: Binding(
        get: { model.isDropTarget },
        set: { model.setDropTarget($0) }
      )
    ) { providers in
      model.handleDrop(providers: providers)
    }
  }

  private var cardColumn: some View {
    VStack(spacing: 0) {
      historyPill
      cardShell
    }
  }

  private var cardShell: some View {
    VStack(spacing: 0) {
      historyStrip
      inputRow
      answerView
    }
    .frame(width: NativeCardModel.cardWidth)
    .background(cardBackground)
    .clipShape(RoundedRectangle(cornerRadius: 30, style: .continuous))
    .overlay(edgeStroke)
    .opacity(hovering || inputFocused ? 1.0 : 0.95)
    .animation(.easeInOut(duration: 0.22), value: hovering)
    .contentShape(RoundedRectangle(cornerRadius: 30, style: .continuous))
    .gesture(dragGesture)
  }

  private var inputRow: some View {
    HStack(alignment: model.isSubmitted ? .top : .center, spacing: model.isSubmitted ? 0 : 12) {
      if model.isListening && !model.isSubmitted {
        waveform
          .frame(width: 64, height: 22)
          .transition(.move(edge: .leading).combined(with: .opacity))
      } else if !model.isSubmitted {
        Circle()
          .fill(Color(red: 0.37, green: 0.78, blue: 1.0))
          .frame(width: 6, height: 6)
          .shadow(color: Color(red: 0.37, green: 0.78, blue: 1.0).opacity(0.8), radius: 4)
          .opacity(model.isListening ? 1 : 0)
      }

      HStack(spacing: model.isSubmitted ? 7 : 8) {
        if let image = model.stagedImage {
          attachmentChip(image)
        }

        if model.isSubmitted {
          Text(model.questionText.isEmpty ? model.inputText : model.questionText)
            .font(.system(size: 12.5, weight: .regular))
            .foregroundStyle(Color.white.opacity(0.56))
            .lineLimit(nil)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.top, 1)
        } else {
          NativeCardTextField(
            text: $model.inputText,
            placeholder: model.inputPlaceholder,
            isDisabled: model.inputDisabled,
            onEnterDown: { model.handleEnterDown(shortAction: model.submitInputText) },
            onEnterUp: { model.handleEnterUp() },
            onEscape: { model.handleEscape() },
            onPasteImage: { model.stageImageFromClipboard() }
          )
          .focused($inputFocused)
          .frame(height: 36)
        }
      }
      .frame(maxWidth: .infinity, alignment: .leading)

      if !model.stateLabel.isEmpty && !model.isSubmitted {
        statePill
      }
    }
    .padding(.top, model.isSubmitted ? 12 : 0)
    .padding(.bottom, model.isSubmitted ? 8 : 0)
    .padding(.leading, model.isSubmitted || model.isFollowupInput ? 38 : 24)
    .padding(.trailing, model.isSubmitted ? 92 : 24)
    .frame(minHeight: model.isSubmitted ? 38 : 64)
    .overlay(alignment: .topTrailing) {
      if !model.stateLabel.isEmpty && model.isSubmitted {
        statePill
          .padding(.top, 12)
          .padding(.trailing, 22)
      }
    }
    .animation(.easeInOut(duration: 0.32), value: model.isSubmitted)
    .animation(.easeInOut(duration: 0.32), value: model.isFollowupInput)
  }

  private var statePill: some View {
    HStack(spacing: 6) {
      TimelineView(.animation) { timeline in
        let pulse = statePulse(at: timeline.date)
        Circle()
          .fill(model.stateColor)
          .frame(width: 4.5, height: 4.5)
          .scaleEffect(model.stateVariant == .success ? 1 : pulse.scale)
          .shadow(color: model.stateColor.opacity(0.75), radius: 3)
          .opacity(model.stateVariant == .success ? 1 : pulse.opacity)
      }
      .frame(width: 4.5, height: 4.5)
      Text(model.stateLabel.uppercased())
        .font(.system(size: 9.5, weight: .regular, design: .monospaced))
        .tracking(1.3)
    }
    .foregroundStyle(model.stateColor)
    .opacity(model.stateLabel.isEmpty ? 0 : 0.92)
    .allowsHitTesting(false)
  }

  private var historyStrip: some View {
    let visibleHeight = historyViewportHeight
    let isVisible = visibleHeight > 0
    return ScrollViewReader { proxy in
      ScrollView(.vertical, showsIndicators: false) {
        VStack(spacing: 4) {
          ForEach(model.history) { turn in
            historyChip(turn)
              .id(turn.id)
          }
        }
        .padding(.horizontal, 22)
      }
      .frame(height: visibleHeight)
      .opacity(isVisible ? 1 : 0)
      .padding(.top, isVisible ? 8 : 0)
      .padding(.bottom, isVisible ? 6 : 0)
      .clipped()
      .onChange(of: model.history.count) { _, _ in
        if let last = model.history.last?.id {
          withAnimation(.easeOut(duration: 0.22)) {
            proxy.scrollTo(last, anchor: .bottom)
          }
        }
      }
    }
    .animation(.timingCurve(0.32, 0.94, 0.6, 1, duration: 0.38), value: model.isHistoryShown)
    .animation(.timingCurve(0.32, 0.94, 0.6, 1, duration: 0.38), value: model.historyCount)
  }

  private var historyViewportHeight: CGFloat {
    guard model.isHistoryShown else { return 0 }
    let visibleCount = min(max(model.historyCount, 0), 3)
    guard visibleCount > 0 else { return 0 }
    return CGFloat(visibleCount * 27 + (visibleCount - 1) * 4)
  }

  private func historyChip(_ turn: NativeHistoryTurn) -> some View {
    HStack(spacing: 6) {
      Text(turn.question)
        .foregroundStyle(Color.white.opacity(0.55))
        .lineLimit(1)
        .truncationMode(.tail)
        .frame(width: chipQuestionWidth(turn.question), alignment: .leading)
        .clipped()
      Text("→")
        .foregroundStyle(Color.white.opacity(0.35))
      Text(turn.answer.components(separatedBy: .newlines).first ?? turn.answer)
        .foregroundStyle(Color.white.opacity(0.78))
        .lineLimit(1)
        .truncationMode(.tail)
        .frame(maxWidth: .infinity, alignment: .leading)
    }
    .font(.system(size: 11, weight: .regular, design: .monospaced))
    .padding(.vertical, turn.fading ? 0 : 5)
    .padding(.horizontal, 12)
    .frame(maxWidth: .infinity, minHeight: turn.fading ? 0 : 27, maxHeight: turn.fading ? 0 : 27)
    .background(
      RoundedRectangle(cornerRadius: 12, style: .continuous)
        .fill(model.activeHistoryID == turn.id ? Color(red: 0.37, green: 0.78, blue: 1).opacity(0.13) : Color.white.opacity(0.05))
    )
    .overlay(alignment: .leading) {
      if model.activeHistoryID == turn.id || turn.fresh {
        RoundedRectangle(cornerRadius: 2)
          .fill(Color(red: 0.37, green: 0.78, blue: 1).opacity(turn.fresh ? 0.62 : 0.65))
          .frame(width: 2)
          .padding(.vertical, 7)
          .padding(.leading, 4)
      }
    }
    .opacity(turn.fading ? 0 : 1)
    .contentShape(Rectangle())
    .onTapGesture {
      model.showPopover(for: turn)
    }
    .onHover { value in
      if !value { model.schedulePopoverHide() }
    }
    .animation(.easeInOut(duration: 0.24), value: turn.fading)
  }

  private func chipQuestionWidth(_ text: String) -> CGFloat {
    let font = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
    let width = ceil((text as NSString).size(withAttributes: [.font: font]).width)
    return min(max(width, 1), 110)
  }

  private var answerView: some View {
    Group {
      if model.isSubmitted && !model.answerText.isEmpty {
        answerContent
          .padding(.leading, 38)
          .padding(.trailing, 34)
          .padding(.bottom, 22)
          .transition(.move(edge: .top).combined(with: .opacity))
      }
    }
    .frame(maxWidth: .infinity, alignment: .leading)
    .opacity(model.isFollowupEntering ? 0 : 1)
    .blur(radius: model.isFollowupEntering ? 1 : 0)
    .animation(.timingCurve(0.32, 0.94, 0.6, 1, duration: 0.50), value: model.answerText)
    .animation(.easeInOut(duration: 0.24), value: model.isFollowupEntering)
  }

  @ViewBuilder
  private var answerContent: some View {
    if answerShouldScroll {
      ScrollView(.vertical, showsIndicators: false) {
        NativeMarkdownText(markdown: model.answerText, characterBirthTimes: model.answerCharacterBirthTimes)
          .frame(maxWidth: .infinity, alignment: .leading)
      }
      .frame(maxHeight: 466, alignment: .top)
    } else {
      NativeMarkdownText(markdown: model.answerText, characterBirthTimes: model.answerCharacterBirthTimes)
    }
  }

  private var answerShouldScroll: Bool {
    model.answerText.count > 1800 || model.answerText.filter { $0 == "\n" }.count > 20
  }

  private var historyPill: some View {
    HStack(spacing: 0) {
      HStack(spacing: 7) {
        Text("⌃")
          .font(.system(size: 10, weight: .regular, design: .monospaced))
          .rotationEffect(model.isHistoryShown ? .degrees(180) : .degrees(0))
          .foregroundStyle(model.isHistoryShown ? Color(red: 0.37, green: 0.78, blue: 1) : Color.white.opacity(0.55))
        Text("\(model.historyCount)")
          .font(.system(size: 11, weight: .medium, design: .monospaced))
          .foregroundStyle(Color(red: 0.37, green: 0.78, blue: 1).opacity(0.85))
        Text("history")
          .font(.system(size: 10, weight: .regular, design: .monospaced))
          .foregroundStyle(Color.white.opacity(0.40))
      }
      .padding(.leading, 13)
      .padding(.trailing, 14)
      .frame(height: 35)
      .contentShape(Rectangle())
      .onTapGesture { model.toggleHistoryShown() }

      Rectangle()
        .fill(Color.white.opacity(0.16))
        .frame(width: 1, height: 14)

      Text("×")
        .font(.system(size: 13, weight: .regular, design: .monospaced))
        .foregroundStyle(Color.white.opacity(0.30))
        .frame(width: 32, height: 35)
        .contentShape(Rectangle())
        .onTapGesture { model.clearHistoryCascade() }
    }
    .background(
      Capsule(style: .continuous)
        .fill(Color(red: 0.086, green: 0.11, blue: 0.165).opacity(0.86))
        .background(.ultraThinMaterial, in: Capsule(style: .continuous))
        .overlay(Capsule(style: .continuous).stroke(Color.white.opacity(0.07), lineWidth: 1))
        .shadow(color: Color.black.opacity(0.55), radius: 16, y: 14)
    )
    .opacity(pillHovering ? 1 : 0)
    .scaleEffect(pillHovering ? 1 : 0.96, anchor: .bottom)
    .offset(y: pillHovering ? 0 : -8)
    .frame(height: NativeCardModel.pillReservedTop, alignment: .bottom)
    .onHover { pillHovering = $0 }
    .animation(.timingCurve(0.34, 1.4, 0.64, 1, duration: 0.36), value: pillHovering)
  }

  private var popoverLayer: some View {
    Group {
      if model.popoverVisible, let turn = model.activeHistoryTurn {
        VStack(alignment: .leading, spacing: 12) {
          Text(turn.question)
            .font(.system(size: 11, weight: .regular, design: .monospaced))
            .foregroundStyle(Color.white.opacity(0.55))
            .lineSpacing(4)
            .fixedSize(horizontal: false, vertical: true)
            .padding(.bottom, 12)
            .overlay(alignment: .bottom) {
              Rectangle().fill(Color.white.opacity(0.06)).frame(height: 1)
            }
          ScrollView(.vertical, showsIndicators: false) {
            Text(turn.answer)
              .font(.system(size: 13))
              .lineSpacing(4.5)
              .foregroundStyle(Color.white.opacity(0.94))
              .frame(maxWidth: .infinity, alignment: .leading)
              .textSelection(.enabled)
          }
          .frame(maxHeight: 410)
        }
        .padding(.top, 18)
        .padding(.horizontal, 22)
        .padding(.bottom, 20)
        .frame(width: NativeCardModel.popoverWidth, alignment: .topLeading)
        .background(cardBackground)
        .clipShape(RoundedRectangle(cornerRadius: 30, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 30, style: .continuous).stroke(Color.white.opacity(0.06), lineWidth: 0.5))
        .offset(x: -(NativeCardModel.cardWidth + NativeCardModel.popoverGap), y: NativeCardModel.pillReservedTop + model.selectedPopoverTop)
        .transition(.asymmetric(
          insertion: .offset(x: 8).combined(with: .scale(scale: 0.985, anchor: .trailing)).combined(with: .opacity),
          removal: .offset(x: 8).combined(with: .scale(scale: 0.985, anchor: .trailing)).combined(with: .opacity)
        ))
        .onHover { value in
          if value {
            model.cancelNativeFade()
            model.cancelPopoverHide()
          }
          else { model.schedulePopoverHide() }
        }
      }
    }
    .animation(.timingCurve(0.34, 1.4, 0.64, 1, duration: 0.32), value: model.activeHistoryID)
  }

  private var cardBackground: some View {
    ZStack {
      RoundedRectangle(cornerRadius: 30, style: .continuous)
        .fill(Color(red: 0.078, green: 0.098, blue: 0.149).opacity(0.95))
      RoundedRectangle(cornerRadius: 30, style: .continuous)
        .fill(.ultraThinMaterial)
        .opacity(0.35)
      if model.isThinking {
        TimelineView(.animation) { timeline in
          let t = timeline.date.timeIntervalSinceReferenceDate
          let wave = (sin(t * 2 * .pi / 0.85) + 1) / 2
          RadialGradient(
            colors: [Color(red: 0.37, green: 0.78, blue: 1).opacity(0.24), .clear],
            center: .center,
            startRadius: 12,
            endRadius: 190
          )
          .opacity(0.50 + wave * 0.42)
          .scaleEffect(0.98 + wave * 0.04)
        }
      }
    }
    .overlay(alignment: .top) {
      Rectangle()
        .fill(Color.white.opacity(0.14))
        .frame(height: 1)
        .padding(.horizontal, 30)
        .blur(radius: 0.2)
    }
  }

  private var edgeStroke: some View {
    RoundedRectangle(cornerRadius: 30, style: .continuous)
      .stroke(edgeColor, lineWidth: (model.isListening || model.isDropTarget || model.attachmentEdgeFlash || model.stateVariant == .warn || model.stateVariant == .error) ? 1.5 : 0.5)
      .shadow(color: edgeColor.opacity(model.isListening || model.isDropTarget || model.attachmentEdgeFlash ? 0.24 : 0), radius: model.attachmentEdgeFlash ? 14 : 10)
      .allowsHitTesting(false)
  }

  private var edgeColor: Color {
    if model.stateVariant == .error { return Color(red: 1, green: 0.42, blue: 0.42).opacity(0.85) }
    if model.stateVariant == .warn { return Color(red: 1, green: 0.72, blue: 0.30).opacity(0.85) }
    if model.isListening || model.isDropTarget || model.attachmentEdgeFlash { return Color(red: 0.37, green: 0.78, blue: 1).opacity(0.82) }
    return Color.white.opacity(0.06)
  }

  private var waveform: some View {
    TimelineView(.animation) { timeline in
      HStack(spacing: 3) {
        ForEach([0.30, 0.70, 1.00, 0.60, 0.40].indices, id: \.self) { idx in
          let base = [0.30, 0.70, 1.00, 0.60, 0.40][idx]
          let t = timeline.date.timeIntervalSinceReferenceDate
          let wave = (sin(t * 2 * .pi / 0.9 + Double(idx) * 0.7) + 1) / 2
          RoundedRectangle(cornerRadius: 2)
            .fill(Color(red: 0.37, green: 0.78, blue: 1))
            .frame(width: 3, height: 22 * base)
            .scaleEffect(y: 0.40 + wave * 0.60)
        }
      }
    }
  }

  private func statePulse(at date: Date) -> (scale: CGFloat, opacity: Double) {
    let duration = model.stateVariant == .thinking ? 0.8 : 1.4
    let wave = (sin(date.timeIntervalSinceReferenceDate * 2 * .pi / duration) + 1) / 2
    return (0.82 + CGFloat(wave) * 0.23, 0.40 + wave * 0.60)
  }

  private func attachmentChip(_ image: NativeImageAttachment) -> some View {
    HStack(spacing: 6) {
      RoundedRectangle(cornerRadius: model.isSubmitted ? 2.5 : 3, style: .continuous)
        .fill(LinearGradient(
          colors: [Color(red: 0.37, green: 0.78, blue: 1), Color(red: 0.62, green: 0.86, blue: 1)],
          startPoint: .topLeading,
          endPoint: .bottomTrailing
        ))
        .frame(width: model.isSubmitted ? 10 : 14, height: model.isSubmitted ? 10 : 14)
        .shadow(color: Color(red: 0.37, green: 0.78, blue: 1).opacity(0.32), radius: 5)
      Text(image.label)
        .lineLimit(1)
        .truncationMode(.tail)
      if !model.isSubmitted {
        Text(image.meta)
          .font(.system(size: 9.5, weight: .regular, design: .monospaced))
          .foregroundStyle(Color.white.opacity(0.55))
        Button("×") {
          model.clearStagedImage()
        }
        .buttonStyle(.plain)
        .foregroundStyle(Color.white.opacity(0.46))
      }
    }
    .font(.system(size: model.isSubmitted ? 9.5 : 11, weight: .regular, design: .monospaced))
    .foregroundStyle(Color.white.opacity(0.86))
    .padding(.vertical, model.isSubmitted ? 3 : 4)
    .padding(.leading, model.isSubmitted ? 5 : 6)
    .padding(.trailing, model.isSubmitted ? 6 : 7)
    .frame(maxWidth: model.isSubmitted ? 112 : 136)
    .background(
      RoundedRectangle(cornerRadius: 8, style: .continuous)
        .fill(Color(red: 0.37, green: 0.78, blue: 1).opacity(model.isSubmitted ? 0.08 : 0.12))
        .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous).stroke(Color(red: 0.37, green: 0.78, blue: 1).opacity(model.isSubmitted ? 0.22 : 0.30), lineWidth: 1))
    )
  }

  private var dragGesture: some Gesture {
    DragGesture(minimumDistance: 2)
      .onChanged { value in
        if dragStart == nil {
          dragStart = value.location
          lastDragTranslation = .zero
          model.beginDrag()
        }
        let dx = value.translation.width - lastDragTranslation.width
        let dy = value.translation.height - lastDragTranslation.height
        lastDragTranslation = value.translation
        model.movePanel(dx: dx, dy: -dy)
      }
      .onEnded { _ in
        dragStart = nil
        lastDragTranslation = .zero
      }
  }
}

struct NativeMarkdownText: View {
  let markdown: String
  var characterBirthTimes: [TimeInterval] = []

  var body: some View {
    let blocks = timedBlocks
    VStack(alignment: .leading, spacing: 6) {
      ForEach(blocks.indices, id: \.self) { index in
        let timed = blocks[index]
        blockView(
          timed.block,
          range: timed.range,
          followsHeading1: previousTextBlockIsHeading1(before: index, in: blocks)
        )
      }
    }
    .font(.system(size: 14.5))
    .lineSpacing(3)
    .foregroundStyle(Color.white.opacity(0.92))
    .textSelection(.enabled)
  }

  @ViewBuilder
  private func blockView(_ block: NativeAnswerBlock, range: Range<Int>?, followsHeading1: Bool) -> some View {
    switch block {
    case .spacer:
      Spacer().frame(height: 4)
    case .heading1(let text):
      inlineTextView(text, range: range, color: Color.white.opacity(0.98))
        .font(.system(size: 32, weight: .ultraLight))
    case .heading2(let text):
      inlineTextView(text, range: range, color: Color.white.opacity(0.98))
        .font(.system(size: 18, weight: .medium))
        .padding(.top, 8)
    case .heading3(let text):
      inlineTextView(text, range: range, color: Color.white.opacity(0.98))
        .font(.system(size: 16, weight: .medium))
        .padding(.top, 8)
    case .heading4(let text):
      inlineTextView(text, range: range, color: Color.white.opacity(0.55))
        .font(.system(size: 14, weight: .medium))
        .padding(.top, 8)
    case .bullet(let text):
      HStack(alignment: .top, spacing: 8) {
        Text("•").foregroundStyle(Color.white.opacity(0.35))
        inlineTextView(text, range: range, color: Color.white.opacity(0.88))
          .frame(maxWidth: .infinity, alignment: .leading)
      }
      .font(.system(size: 13.5))
      .foregroundStyle(Color.white.opacity(0.88))
    case .numbered(let marker, let text):
      HStack(alignment: .top, spacing: 8) {
        Text(marker)
          .foregroundStyle(Color.white.opacity(0.35))
          .frame(minWidth: 18, alignment: .trailing)
        inlineTextView(text, range: range, color: Color.white.opacity(0.88))
          .frame(maxWidth: .infinity, alignment: .leading)
      }
      .font(.system(size: 13.5))
      .foregroundStyle(Color.white.opacity(0.88))
    case .code(let code, let language):
      let lineCount = max(1, code.components(separatedBy: .newlines).count)
      ScrollView(.horizontal, showsIndicators: false) {
        NativeCodeText(code: code, language: language)
          .font(.system(size: 12.5, weight: .regular, design: .monospaced))
          .lineSpacing(6)
          .padding(.vertical, 12)
          .padding(.horizontal, 14)
          .frame(maxWidth: .infinity, alignment: .leading)
      }
      .frame(minHeight: CGFloat(lineCount) * 30 + 24, alignment: .topLeading)
      .background(
        RoundedRectangle(cornerRadius: 12, style: .continuous)
          .fill(Color.black.opacity(0.32))
      )
      .padding(.vertical, 10)
    case .paragraph(let text):
      inlineTextView(text, range: range, color: Color.white.opacity(followsHeading1 ? 0.55 : 0.92))
        .font(.system(size: followsHeading1 ? 13 : 14.5))
        .fixedSize(horizontal: false, vertical: true)
    case .display(let text):
      Text(text)
        .font(.system(size: 56, weight: .ultraLight))
        .foregroundStyle(Color.white.opacity(0.99))
        .lineLimit(1)
        .minimumScaleFactor(0.75)
        .shadow(color: Color.black.opacity(0.35), radius: 8, y: 1)
    case .displayLabel(let text):
      Text(text)
        .font(.system(size: 13))
        .foregroundStyle(Color.white.opacity(0.52))
        .padding(.top, 6)
    case .muted(let text):
      inlineTextView(text, range: range, color: Color.white.opacity(0.55))
        .font(.system(size: 12.5))
    case .blockquote(let text):
      HStack(alignment: .top, spacing: 12) {
        Rectangle()
          .fill(Color.white.opacity(0.18))
          .frame(width: 2)
        inlineTextView(text, range: range, color: Color.white.opacity(0.55))
          .font(.system(size: 14))
          .frame(maxWidth: .infinity, alignment: .leading)
      }
      .padding(.vertical, 4)
    case .rule:
      Rectangle()
        .fill(Color.white.opacity(0.10))
        .frame(height: 1)
        .padding(.vertical, 6)
    case .table(let headers, let rows):
      tableView(headers: headers, rows: rows)
    case .tool(let tool):
      toolRow(tool)
    case .choice(let options):
      HStack(spacing: 8) {
        ForEach(options.indices, id: \.self) { idx in
          optionPill(options[idx], kind: .choice)
        }
      }
      .padding(.top, 10)
    case .confirmGate(let buttons):
      HStack(spacing: 8) {
        ForEach(buttons.indices, id: \.self) { idx in
          optionPill(buttons[idx], kind: .confirm)
        }
      }
      .padding(.top, 10)
    case .tts(let style):
      ttsWaveform(style: style)
        .padding(.top, 10)
    }
  }

  private func inlineText(_ value: String) -> Text {
    if let attributed = try? AttributedString(
      markdown: value,
      options: AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
    ) {
      return Text(attributed)
    }
    return Text(value)
  }

  private func inlineTextView(_ value: String, range: Range<Int>?, color: Color) -> some View {
    NativeAnimatedInlineText(
      markdown: value,
      range: range,
      characterBirthTimes: characterBirthTimes,
      baseColor: color
    )
  }

  private var timedBlocks: [NativeTimedAnswerBlock] {
    NativeAnswerParser.timedBlocks(markdown)
  }

  private func previousTextBlockIsHeading1(before index: Int, in blocks: [NativeTimedAnswerBlock]) -> Bool {
    guard index > 0 else { return false }
    for previousIndex in stride(from: index - 1, through: 0, by: -1) {
      switch blocks[previousIndex].block {
      case .spacer:
        continue
      case .heading1:
        return true
      default:
        return false
      }
    }
    return false
  }

  private func toolRow(_ tool: NativeAnswerTool) -> some View {
    HStack(spacing: 10) {
      Text(tool.tag)
        .font(.system(size: 10, weight: .regular, design: .monospaced))
        .tracking(0.6)
        .textCase(.uppercase)
        .foregroundStyle(Color(red: 0.37, green: 0.78, blue: 1).opacity(0.85))
        .frame(minWidth: 56, alignment: .leading)
      Text(tool.name)
        .font(.system(size: 13))
        .foregroundStyle(Color.white.opacity(0.85))
        .lineLimit(1)
        .truncationMode(.tail)
        .frame(maxWidth: .infinity, alignment: .leading)
      if let progress = tool.progress {
        ZStack(alignment: .leading) {
          Capsule().fill(Color.white.opacity(0.10))
          Capsule()
            .fill(Color(red: 0.37, green: 0.78, blue: 1))
            .frame(width: 64 * progress)
        }
        .frame(width: 64, height: 3)
      }
      if !tool.status.isEmpty {
        Text(tool.status)
          .font(.system(size: 11, weight: .regular, design: .monospaced))
          .foregroundStyle(tool.statusColor)
      }
    }
    .padding(.vertical, 8)
    .overlay(alignment: .bottom) {
      Rectangle().fill(Color.white.opacity(0.06)).frame(height: 1)
    }
  }

  private func tableView(headers: [String], rows: [[String]]) -> some View {
    VStack(spacing: 0) {
      tableRow(headers, isHeader: true)
      ForEach(rows.indices, id: \.self) { idx in
        tableRow(rows[idx], isHeader: false)
      }
    }
    .font(.system(size: 13))
    .padding(.vertical, 6)
  }

  private func tableRow(_ cells: [String], isHeader: Bool) -> some View {
    HStack(alignment: .top, spacing: 0) {
      ForEach(cells.indices, id: \.self) { idx in
        inlineText(cells[idx])
          .font(.system(size: 13, weight: isHeader ? .medium : .regular))
          .foregroundStyle(isHeader ? Color.white.opacity(0.55) : Color.white.opacity(0.88))
          .frame(maxWidth: .infinity, alignment: .leading)
          .padding(.vertical, 6)
          .padding(.horizontal, 10)
      }
    }
    .overlay(alignment: .bottom) {
      Rectangle().fill(Color.white.opacity(0.10)).frame(height: 1)
    }
  }

  private func optionPill(_ option: NativeAnswerOption, kind: NativeAnswerOptionKind) -> some View {
    Text(option.label)
      .font(.system(size: 13, weight: .medium))
      .foregroundStyle(option.textColor(kind: kind))
      .lineLimit(2)
      .minimumScaleFactor(0.8)
      .frame(maxWidth: .infinity)
      .padding(.vertical, 10)
      .background(
        RoundedRectangle(cornerRadius: 10, style: .continuous)
          .fill(option.backgroundColor(kind: kind))
          .overlay(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
              .stroke(option.borderColor(kind: kind), lineWidth: 1)
          )
      )
      .opacity(option.faded ? 0.32 : 1)
      .scaleEffect(option.picked ? 1.02 : (option.faded ? 0.98 : 1))
  }

  private func ttsWaveform(style: NativeAnswerTTSStyle) -> some View {
    TimelineView(.animation) { timeline in
      HStack(spacing: 3) {
        ForEach(style.heights.indices, id: \.self) { idx in
          let t = timeline.date.timeIntervalSinceReferenceDate
          let wave = style.isDead ? 0 : (sin(t * 2 * .pi / style.period + Double(idx) * 0.35) + 1) / 2
          RoundedRectangle(cornerRadius: 2)
            .fill(style.color)
            .frame(width: 2.5, height: 16 * style.heights[idx])
            .scaleEffect(y: style.isDead ? 0.15 : 0.50 + wave * 0.50)
            .opacity(style.opacity)
        }
      }
      .frame(height: 16)
    }
  }
}

struct NativeAnimatedInlineText: View {
  let markdown: String
  let range: Range<Int>?
  let characterBirthTimes: [TimeInterval]
  let baseColor: Color

  private let fadeDuration: TimeInterval = 0.250

  var body: some View {
    TimelineView(.animation) { timeline in
      Text(attributedText(at: timeline.date.timeIntervalSinceReferenceDate))
    }
  }

  private func attributedText(at now: TimeInterval) -> AttributedString {
    var attributed = (try? AttributedString(
      markdown: markdown,
      options: AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
    )) ?? AttributedString(markdown)

    guard let range, !characterBirthTimes.isEmpty else {
      attributed.foregroundColor = baseColor
      return attributed
    }

    var index = attributed.startIndex
    var visibleOffset = range.lowerBound
    while index < attributed.endIndex, visibleOffset < range.upperBound {
      let next = attributed.index(afterCharacter: index)
      let birth = visibleOffset < characterBirthTimes.count ? characterBirthTimes[visibleOffset] : now - fadeDuration
      let age = max(0, now - birth)
      let opacity = min(1, max(0, age / fadeDuration))
      attributed[index..<next].foregroundColor = baseColor.opacity(opacity)
      index = next
      visibleOffset += 1
    }
    return attributed
  }
}

struct NativeCodeText: View {
  let code: String
  let language: String?

  var body: some View {
    Text(Self.highlighted(code, language: language))
  }

  private static func highlighted(_ code: String, language: String?) -> AttributedString {
    let nsCode = code as NSString
    let fullRange = NSRange(location: 0, length: nsCode.length)
    let attributed = NSMutableAttributedString(string: code)
    attributed.addAttributes([
      .foregroundColor: NSColor(calibratedRed: 1.0, green: 0.86, blue: 0.70, alpha: 0.92),
    ], range: fullRange)
    guard !code.isEmpty else { return AttributedString(attributed) }

    let keywordColor = NSColor(calibratedRed: 1.0, green: 0.45, blue: 0.52, alpha: 0.95)
    let stringColor = NSColor(calibratedRed: 0.56, green: 0.78, blue: 1.0, alpha: 0.95)
    let numberColor = NSColor(calibratedRed: 0.98, green: 0.70, blue: 0.42, alpha: 0.95)
    let commentColor = NSColor.white.withAlphaComponent(0.42)

    apply(pattern: keywordPattern(for: language), color: keywordColor, to: attributed, in: fullRange)
    apply(pattern: #"\b\d+(?:\.\d+)?\b"#, color: numberColor, to: attributed, in: fullRange)
    apply(pattern: #""(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'"#, color: stringColor, to: attributed, in: fullRange)
    apply(pattern: #"(?m)(#|//).*$"#, color: commentColor, to: attributed, in: fullRange)

    return AttributedString(attributed)
  }

  private static func keywordPattern(for language: String?) -> String {
    let normalized = language?.lowercased() ?? ""
    let words: [String]
    if normalized.hasPrefix("py") {
      words = ["and", "as", "async", "await", "class", "def", "elif", "else", "except", "False", "finally", "for", "from", "if", "import", "in", "is", "lambda", "None", "not", "or", "pass", "return", "True", "try", "while", "with", "yield"]
    } else if normalized == "swift" {
      words = ["actor", "as", "async", "await", "case", "catch", "class", "else", "enum", "false", "for", "func", "guard", "if", "import", "in", "let", "nil", "private", "return", "static", "struct", "switch", "throw", "throws", "true", "try", "var", "while"]
    } else if ["js", "jsx", "javascript", "ts", "tsx", "typescript"].contains(normalized) {
      words = ["async", "await", "case", "catch", "class", "const", "else", "export", "false", "for", "function", "if", "import", "let", "new", "null", "return", "switch", "throw", "true", "try", "undefined", "var", "while"]
    } else {
      words = ["async", "await", "case", "class", "const", "def", "else", "false", "for", "func", "function", "if", "let", "nil", "null", "return", "struct", "true", "var", "while"]
    }
    return #"(?i)\b("# + words.joined(separator: "|") + #")\b"#
  }

  private static func apply(
    pattern: String,
    color: NSColor,
    to attributed: NSMutableAttributedString,
    in range: NSRange
  ) {
    guard let regex = try? NSRegularExpression(pattern: pattern) else { return }
    regex.enumerateMatches(in: attributed.string, range: range) { match, _, _ in
      guard let match else { return }
      attributed.addAttribute(.foregroundColor, value: color, range: match.range)
    }
  }
}

struct NativeTimedAnswerBlock: Equatable {
  let offset: Int
  let block: NativeAnswerBlock
  let range: Range<Int>?
}

enum NativeAnswerBlock: Equatable {
  case spacer
  case heading1(String)
  case heading2(String)
  case heading3(String)
  case heading4(String)
  case bullet(String)
  case numbered(String, String)
  case code(String, language: String?)
  case paragraph(String)
  case display(String)
  case displayLabel(String)
  case muted(String)
  case blockquote(String)
  case rule
  case table(headers: [String], rows: [[String]])
  case tool(NativeAnswerTool)
  case choice([NativeAnswerOption])
  case confirmGate([NativeAnswerOption])
  case tts(NativeAnswerTTSStyle)
}

struct NativeAnswerTool: Equatable {
  var tag: String
  var name: String
  var status: String
  var state: String
  var progress: CGFloat?

  var statusColor: Color {
    if state.contains("done") { return Color(red: 0.55, green: 0.85, blue: 0.70).opacity(0.85) }
    if state.contains("fail") { return Color(red: 1, green: 0.42, blue: 0.42).opacity(0.85) }
    return Color.white.opacity(0.55)
  }
}

struct NativeAnswerOption: Equatable {
  var label: String
  var classes: Set<String>

  var picked: Bool { classes.contains("picked") }
  var faded: Bool { classes.contains("faded") }
  var allow: Bool { classes.contains("allow") }
  var deny: Bool { classes.contains("deny") }

  func textColor(kind: NativeAnswerOptionKind) -> Color {
    if picked && kind == .confirm && allow { return Color(red: 0.55, green: 0.85, blue: 0.70) }
    if picked && kind == .confirm && deny { return Color(red: 1, green: 0.60, blue: 0.60) }
    if picked { return Color(red: 0.84, green: 0.93, blue: 1) }
    return Color.white.opacity(0.82)
  }

  func backgroundColor(kind: NativeAnswerOptionKind) -> Color {
    if picked && kind == .confirm && allow { return Color(red: 0.55, green: 0.85, blue: 0.70).opacity(0.18) }
    if picked && kind == .confirm && deny { return Color(red: 1, green: 0.42, blue: 0.42).opacity(0.18) }
    if picked { return Color(red: 0.37, green: 0.78, blue: 1).opacity(0.18) }
    return Color.white.opacity(0.05)
  }

  func borderColor(kind: NativeAnswerOptionKind) -> Color {
    if picked && kind == .confirm && allow { return Color(red: 0.55, green: 0.85, blue: 0.70).opacity(0.55) }
    if picked && kind == .confirm && deny { return Color(red: 1, green: 0.42, blue: 0.42).opacity(0.55) }
    if picked { return Color(red: 0.37, green: 0.78, blue: 1).opacity(0.55) }
    return Color.white.opacity(0.10)
  }
}

enum NativeAnswerOptionKind {
  case choice
  case confirm
}

struct NativeAnswerTTSStyle: Equatable {
  var classes: Set<String>

  var isDead: Bool { classes.contains("dead") }
  var period: Double { classes.contains("ducked") ? 1.6 : 1.1 }
  var opacity: Double {
    if classes.contains("dead") { return 0.18 }
    if classes.contains("ducked") { return 0.32 }
    return 1
  }
  var color: Color {
    classes.contains("ducked") || classes.contains("dead")
      ? Color.white.opacity(0.40)
      : Color(red: 0.37, green: 0.78, blue: 1).opacity(0.85)
  }
  var heights: [Double] {
    [0.22, 0.56, 0.88, 0.64, 1.00, 0.76, 0.50, 0.28, 0.64, 0.88, 0.36, 0.56]
  }
}

enum NativeAnswerParser {
  static func timedBlocks(_ markdown: String) -> [NativeTimedAnswerBlock] {
    var cursor = 0
    return parse(markdown).enumerated().map { offset, block in
      let text = visibleText(for: block)
      let range: Range<Int>? = text.isEmpty ? nil : cursor..<(cursor + text.count)
      cursor += text.count
      return NativeTimedAnswerBlock(offset: offset, block: block, range: range)
    }
  }

  static func visibleText(_ markdown: String) -> String {
    parse(markdown).map { visibleText(for: $0) }.joined()
  }

  static func visibleText(for block: NativeAnswerBlock) -> String {
    switch block {
    case .heading1(let text),
         .heading2(let text),
         .heading3(let text),
         .heading4(let text),
         .bullet(let text),
         .paragraph(let text),
         .muted(let text),
         .blockquote(let text),
         .display(let text),
         .displayLabel(let text):
      return plainInlineText(text)
    case .numbered(_, let text):
      return plainInlineText(text)
    case .code(let code, _):
      return code
    case .table(let headers, let rows):
      return (headers + rows.flatMap { $0 }).map(plainInlineText).joined()
    case .tool(let tool):
      return "\(tool.tag)\(tool.name)\(tool.status)"
    case .choice(let options), .confirmGate(let options):
      return options.map(\.label).joined()
    case .tts, .rule, .spacer:
      return ""
    }
  }

  static func plainInlineText(_ value: String) -> String {
    if let attributed = try? AttributedString(
      markdown: value,
      options: AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
    ) {
      return String(attributed.characters)
    }
    return value
  }

  static func parse(_ markdown: String) -> [NativeAnswerBlock] {
    var result: [NativeAnswerBlock] = []
    var codeLines: [String] = []
    var codeLanguage: String?
    var inCode = false
    let lines = markdown.components(separatedBy: .newlines)
    var i = 0

    while i < lines.count {
      let raw = lines[i]
      let line = raw.trimmingCharacters(in: .whitespaces)
      if line.hasPrefix("```") {
        if inCode {
          result.append(.code(codeLines.joined(separator: "\n"), language: codeLanguage))
          codeLines.removeAll()
          codeLanguage = nil
          inCode = false
        } else {
          inCode = true
          codeLanguage = fenceLanguage(from: line)
          codeLines.removeAll()
        }
        i += 1
        continue
      }
      if inCode {
        codeLines.append(raw)
        i += 1
        continue
      }
      if isTableHeader(line),
         i + 1 < lines.count,
         isTableSeparator(lines[i + 1].trimmingCharacters(in: .whitespaces)) {
        let headers = tableCells(line)
        var rows: [[String]] = []
        i += 2
        while i < lines.count {
          let rowLine = lines[i].trimmingCharacters(in: .whitespaces)
          guard isTableRow(rowLine) else { break }
          rows.append(tableCells(rowLine))
          i += 1
        }
        result.append(.table(headers: headers, rows: rows))
        continue
      }
      if line.isEmpty {
        i += 1
        continue
      } else if let htmlBlock = parseHTMLPrimitive(line) {
        result.append(htmlBlock)
      } else if line.hasPrefix("# ") {
        result.append(.heading1(String(line.dropFirst(2))))
      } else if line.hasPrefix("## ") {
        result.append(.heading2(String(line.dropFirst(3))))
      } else if line.hasPrefix("### ") {
        result.append(.heading3(String(line.dropFirst(4))))
      } else if line.hasPrefix("#### ") {
        result.append(.heading4(String(line.dropFirst(5))))
      } else if line.hasPrefix("- ") {
        result.append(.bullet(String(line.dropFirst(2))))
      } else if let ordered = orderedListItem(line) {
        result.append(.numbered(ordered.marker, ordered.text))
      } else if line.hasPrefix("> ") {
        result.append(.blockquote(String(line.dropFirst(2))))
      } else if line == "---" || line == "***" || line == "___" {
        result.append(.rule)
      } else {
        var paragraphLines = [line]
        i += 1
        while i < lines.count {
          let next = lines[i].trimmingCharacters(in: .whitespaces)
          guard isParagraphContinuation(next) else { break }
          paragraphLines.append(next)
          i += 1
        }
        result.append(.paragraph(paragraphLines.joined(separator: "\n")))
        continue
      }
      i += 1
    }
    if inCode {
      result.append(.code(codeLines.joined(separator: "\n"), language: codeLanguage))
    }
    return result
  }

  private static func fenceLanguage(from line: String) -> String? {
    let raw = String(line.dropFirst(3)).trimmingCharacters(in: .whitespacesAndNewlines)
    let language = raw.split(whereSeparator: { $0.isWhitespace }).first.map(String.init) ?? ""
    return language.isEmpty ? nil : language.lowercased()
  }

  private static func isTableHeader(_ line: String) -> Bool {
    isTableRow(line) && !isTableSeparator(line)
  }

  private static func isTableRow(_ line: String) -> Bool {
    line.contains("|") && tableCells(line).count >= 2
  }

  private static func isTableSeparator(_ line: String) -> Bool {
    guard isTableRow(line) else { return false }
    let cells = tableCells(line)
    return cells.allSatisfy { cell in
      let stripped = cell.replacingOccurrences(of: ":", with: "")
      return !stripped.isEmpty && stripped.allSatisfy { $0 == "-" }
    }
  }

  private static func tableCells(_ line: String) -> [String] {
    var raw = line
    if raw.hasPrefix("|") { raw.removeFirst() }
    if raw.hasSuffix("|") { raw.removeLast() }
    return raw.split(separator: "|", omittingEmptySubsequences: false)
      .map { $0.trimmingCharacters(in: .whitespaces) }
  }

  private static func orderedListItem(_ line: String) -> (marker: String, text: String)? {
    let digits = line.prefix { $0.isNumber }
    guard !digits.isEmpty else { return nil }
    let afterDigits = line.dropFirst(digits.count)
    guard let delimiter = afterDigits.first, delimiter == "." || delimiter == ")" else { return nil }
    let afterDelimiter = afterDigits.dropFirst()
    guard afterDelimiter.first?.isWhitespace == true else { return nil }
    let text = afterDelimiter.drop { $0.isWhitespace }
    guard !text.isEmpty else { return nil }
    return ("\(digits).", String(text))
  }

  private static func isParagraphContinuation(_ line: String) -> Bool {
    if line.isEmpty { return false }
    if line.hasPrefix("# ") || line.hasPrefix("## ") || line.hasPrefix("### ") || line.hasPrefix("#### ") { return false }
    if line.hasPrefix("- ") || line.hasPrefix("> ") { return false }
    if line.hasPrefix("```") { return false }
    if line == "---" || line == "***" || line == "___" { return false }
    if orderedListItem(line) != nil { return false }
    if parseHTMLPrimitive(line) != nil { return false }
    return !(isTableHeader(line) || isTableSeparator(line))
  }

  private static func parseHTMLPrimitive(_ line: String) -> NativeAnswerBlock? {
    if line.contains("class=\"tool") || line.contains("class='tool") {
      return .tool(NativeAnswerTool(
        tag: extractClassText("tool-tag", in: line).ifEmpty("TOOL"),
        name: extractClassText("tool-name", in: line).ifEmpty(strippingTags(line)),
        status: extractClassText("tool-status", in: line),
        state: classes(fromFirstClassAttributeIn: line).joined(separator: " "),
        progress: extractWidthFraction(in: line)
      ))
    }
    if line.contains("class=\"choose") || line.contains("class='choose") {
      let options = extractElements(classNames: ["opt"], in: line)
        .map { NativeAnswerOption(label: strippingTags($0.html), classes: $0.classes) }
      return options.isEmpty ? nil : .choice(options)
    }
    if line.contains("class=\"confirm-gate") || line.contains("class='confirm-gate") {
      let buttons = extractElements(classNames: ["gate-btn"], in: line)
        .map { NativeAnswerOption(label: strippingTags($0.html), classes: $0.classes) }
      return buttons.isEmpty ? nil : .confirmGate(buttons)
    }
    if line.contains("class=\"tts") || line.contains("class='tts") {
      return .tts(NativeAnswerTTSStyle(classes: classes(fromFirstClassAttributeIn: line)))
    }
    if hasClass("display-label", in: line) {
      return .displayLabel(extractClassText("display-label", in: line).ifEmpty(strippingTags(line)))
    }
    if hasClass("display", in: line) {
      return .display(extractClassText("display", in: line).ifEmpty(strippingTags(line)))
    }
    if hasClass("muted", in: line) {
      return .muted(extractClassText("muted", in: line).ifEmpty(strippingTags(line)))
    }
    if line == "<hr>" || line == "<hr/>" || line == "<hr />" {
      return .rule
    }
    return nil
  }

  private static func hasClass(_ className: String, in html: String) -> Bool {
    classes(fromFirstClassAttributeIn: html).contains(className)
      || html.contains(" \(className)\"")
      || html.contains(" \(className)'")
      || html.contains("\"\(className) ")
      || html.contains("'\(className) ")
      || html.contains("\"\(className)\"")
      || html.contains("'\(className)'")
  }

  private static func extractClassText(_ className: String, in html: String) -> String {
    extractElements(classNames: [className], in: html).first.map { strippingTags($0.html) } ?? ""
  }

  private static func extractElements(classNames: [String], in html: String) -> [(html: String, classes: Set<String>)] {
    var output: [(String, Set<String>)] = []
    var searchStart = html.startIndex
    while let classRange = html.range(of: "class=", range: searchStart..<html.endIndex) {
      guard let quote = html[classRange.upperBound...].first,
            quote == "\"" || quote == "'" else {
        searchStart = classRange.upperBound
        continue
      }
      let classValueStart = html.index(after: classRange.upperBound)
      guard let classValueEnd = html[classValueStart...].firstIndex(of: quote) else { break }
      let classes = Set(html[classValueStart..<classValueEnd].split(separator: " ").map(String.init))
      guard classNames.contains(where: { classes.contains($0) }) else {
        searchStart = classValueEnd
        continue
      }
      guard let tagStart = html[..<classRange.lowerBound].lastIndex(of: "<"),
            let openEnd = html[classValueEnd...].firstIndex(of: ">") else {
        searchStart = classValueEnd
        continue
      }
      let tagNameStart = html.index(after: tagStart)
      let tagName = html[tagNameStart..<html.index(tagNameStart, offsetBy: html[tagNameStart...].prefix { !$0.isWhitespace && $0 != ">" }.count)]
      let close = "</\(tagName)>"
      guard let closeRange = html.range(of: close, range: openEnd..<html.endIndex) else {
        searchStart = openEnd
        continue
      }
      let innerStart = html.index(after: openEnd)
      output.append((String(html[innerStart..<closeRange.lowerBound]), classes))
      searchStart = closeRange.upperBound
    }
    return output
  }

  private static func classes(fromFirstClassAttributeIn html: String) -> Set<String> {
    guard let classRange = html.range(of: "class="),
          let quote = html[classRange.upperBound...].first,
          quote == "\"" || quote == "'" else {
      return []
    }
    let start = html.index(after: classRange.upperBound)
    guard let end = html[start...].firstIndex(of: quote) else { return [] }
    return Set(html[start..<end].split(separator: " ").map(String.init))
  }

  private static func extractWidthFraction(in html: String) -> CGFloat? {
    guard let widthRange = html.range(of: "width:", options: .caseInsensitive) else { return nil }
    let tail = html[widthRange.upperBound...]
    let numeric = tail.prefix { $0.isNumber || $0 == "." }
    guard let value = Double(numeric) else { return nil }
    if tail.dropFirst(numeric.count).trimmingCharacters(in: .whitespaces).hasPrefix("%") {
      return CGFloat(max(0, min(1, value / 100)))
    }
    return CGFloat(max(0, min(1, value / 64)))
  }

  static func strippingTags(_ html: String) -> String {
    var out = ""
    var inTag = false
    for ch in html {
      if ch == "<" {
        inTag = true
      } else if ch == ">" {
        inTag = false
      } else if !inTag {
        out.append(ch)
      }
    }
    return unescape(out).trimmingCharacters(in: .whitespacesAndNewlines)
  }

  private static func unescape(_ value: String) -> String {
    value
      .replacingOccurrences(of: "&nbsp;", with: " ")
      .replacingOccurrences(of: "&amp;", with: "&")
      .replacingOccurrences(of: "&lt;", with: "<")
      .replacingOccurrences(of: "&gt;", with: ">")
      .replacingOccurrences(of: "&quot;", with: "\"")
      .replacingOccurrences(of: "&#39;", with: "'")
  }
}

private extension String {
  func ifEmpty(_ fallback: String) -> String {
    isEmpty ? fallback : self
  }
}

struct NativeCardTextField: NSViewRepresentable {
  @Binding var text: String
  let placeholder: String
  let isDisabled: Bool
  let onEnterDown: () -> Void
  let onEnterUp: () -> Void
  let onEscape: () -> Void
  let onPasteImage: () -> Bool

  func makeNSView(context: Context) -> NativeTextField {
    let field = NativeTextField()
    field.delegate = context.coordinator
    field.isBordered = false
    field.isBezeled = false
    field.drawsBackground = false
    field.focusRingType = .none
    field.font = NSFont.systemFont(ofSize: 15, weight: .medium)
    field.textColor = NSColor.white.withAlphaComponent(0.96)
    field.placeholderString = placeholder
    field.onEnterDown = onEnterDown
    field.onEnterUp = onEnterUp
    field.onEscape = onEscape
    field.onPasteImage = onPasteImage
    return field
  }

  func updateNSView(_ nsView: NativeTextField, context: Context) {
    if nsView.stringValue != text {
      nsView.stringValue = text
    }
    nsView.placeholderString = placeholder
    nsView.isEnabled = !isDisabled
    nsView.onEnterDown = onEnterDown
    nsView.onEnterUp = onEnterUp
    nsView.onEscape = onEscape
    nsView.onPasteImage = onPasteImage
  }

  func makeCoordinator() -> Coordinator {
    Coordinator(text: $text)
  }

  final class Coordinator: NSObject, NSTextFieldDelegate {
    @Binding var text: String

    init(text: Binding<String>) {
      _text = text
    }

    func controlTextDidChange(_ obj: Notification) {
      guard let field = obj.object as? NSTextField else { return }
      text = field.stringValue
    }
  }
}

final class NativeTextField: NSTextField {
  var onEnterDown: (() -> Void)?
  var onEnterUp: (() -> Void)?
  var onEscape: (() -> Void)?
  var onPasteImage: (() -> Bool)?
  private var enterWasDown = false

  override func keyDown(with event: NSEvent) {
    if event.keyCode == 36 || event.keyCode == 76 {
      if !enterWasDown {
        enterWasDown = true
        onEnterDown?()
      }
      return
    }
    if event.keyCode == 53 {
      onEscape?()
      return
    }
    super.keyDown(with: event)
  }

  override func keyUp(with event: NSEvent) {
    if event.keyCode == 36 || event.keyCode == 76 {
      enterWasDown = false
      onEnterUp?()
      return
    }
    super.keyUp(with: event)
  }

  override func performKeyEquivalent(with event: NSEvent) -> Bool {
    let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
    if event.charactersIgnoringModifiers?.lowercased() == "v",
       flags.contains(.command) || flags.contains(.control),
       onPasteImage?() == true {
      return true
    }
    return super.performKeyEquivalent(with: event)
  }

  @objc func paste(_ sender: Any?) {
    if onPasteImage?() == true { return }
    currentEditor()?.paste(sender)
  }
}
