import AppKit
import SwiftUI
import UniformTypeIdentifiers

struct NativeCardView: View {
  private static let historyChipHeight: CGFloat = 29.25

  @ObservedObject var model: NativeCardModel

  @State private var hovering = false
  @State private var pillHovering = false
  @State private var dragBlocked = false
  @State private var lastDragScreenLocation: NSPoint?
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
    .onChange(of: model.inputDisabled) { _, disabled in
      if disabled {
        inputFocused = false
        NSApp.keyWindow?.makeFirstResponder(nil)
      }
    }
    .onChange(of: model.activeHistoryID) { _, _ in
      pillHovering = false
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
    HStack(alignment: model.isSubmitted ? .top : .center, spacing: inputRowSpacing) {
      if !model.isSubmitted && !model.isFollowupInput {
        Circle()
          .fill(Color(red: 0.37, green: 0.78, blue: 1.0))
          .frame(width: 6, height: 6)
          .shadow(color: Color(red: 0.37, green: 0.78, blue: 1.0).opacity(0.8), radius: 4)
          .opacity(0)
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
            onPasteImage: { model.stageImageFromClipboard() },
            onDropFileURLs: { model.stageDroppedFileURLs($0) }
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
    .padding(.leading, inputRowLeadingPadding)
    .padding(.trailing, model.isSubmitted ? 92 : 24)
    .frame(minHeight: inputRowMinHeight)
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

  private var inputRowSpacing: CGFloat {
    model.isSubmitted || model.isFollowupInput ? 0 : 12
  }

  private var inputRowLeadingPadding: CGFloat {
    if model.isSubmitted { return 38 }
    if model.isFollowupInput { return 40 }
    return 24
  }

  private var inputRowMinHeight: CGFloat {
    if model.isSubmitted { return 38 }
    if model.isFollowupInput { return 57 }
    return 64
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
    return CGFloat(visibleCount - 1) * 4 + CGFloat(visibleCount) * Self.historyChipHeight
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
    .padding(.vertical, turn.fading ? 0 : 6)
    .padding(.horizontal, 12)
    .frame(maxWidth: .infinity, minHeight: turn.fading ? 0 : Self.historyChipHeight, maxHeight: turn.fading ? 0 : Self.historyChipHeight)
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
      if model.isSubmitted {
        if model.answerText.isEmpty {
          Color.clear
            .frame(height: answerBottomPadding + 4)
        } else {
          answerContent
            .padding(.leading, 38)
            .padding(.trailing, 34)
            .padding(.bottom, answerBottomPadding)
            .transition(.move(edge: .top).combined(with: .opacity))
        }
      }
    }
    .frame(maxWidth: .infinity, alignment: .leading)
    .opacity(model.isFollowupEntering ? 0 : 1)
    .blur(radius: model.isFollowupEntering ? 1 : 0)
    .animation(.easeInOut(duration: 0.24), value: model.isFollowupEntering)
  }

  @ViewBuilder
  private var answerContent: some View {
    if answerShouldScroll {
      ScrollView(.vertical, showsIndicators: false) {
        NativeMarkdownText(markdown: model.answerText, characterBirthTimes: model.answerCharacterBirthTimes)
          .frame(maxWidth: .infinity, alignment: .leading)
      }
      .frame(maxHeight: 461, alignment: .top)
    } else {
      NativeMarkdownText(markdown: model.answerText, characterBirthTimes: model.answerCharacterBirthTimes)
    }
  }

  private var answerShouldScroll: Bool {
    model.answerText.count > 1800 || model.answerText.filter { $0 == "\n" }.count > 20
  }

  private var answerBottomPadding: CGFloat {
    model.answerText.contains("```") ? 29 : 22
  }

  private var historyPill: some View {
    ZStack(alignment: .bottom) {
      Rectangle()
        .fill(Color.clear)
        .frame(width: historyPillHoverWidth, height: NativeCardModel.pillReservedTop)
        .contentShape(Rectangle())

      if pillHovering {
        historyPillBody
          .transition(.offset(y: -5).combined(with: .scale(scale: 0.985, anchor: .bottom)).combined(with: .opacity))
      }
    }
    .frame(height: NativeCardModel.pillReservedTop, alignment: .bottom)
    .onHover { pillHovering = $0 }
    .animation(
      pillHovering
        ? .timingCurve(0.18, 0.88, 0.24, 1, duration: 0.24)
        : .easeInOut(duration: 0.16),
      value: pillHovering
    )
  }

  private var historyPillHoverWidth: CGFloat {
    let countWidth = ceil(("\(model.historyCount)" as NSString).size(
      withAttributes: [.font: NSFont.monospacedSystemFont(ofSize: 11, weight: .medium)]
    ).width)
    return 93 + countWidth + 32
  }

  private var historyPillBody: some View {
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
  }

  private var popoverLayer: some View {
    Group {
      if model.popoverVisible, let turn = model.activeHistoryTurn {
        let answerViewportHeight = NativePopoverSizing.answerViewportHeight(for: turn)
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
          .frame(height: answerViewportHeight)
        }
        .padding(.top, NativePopoverSizing.topPadding)
        .padding(.horizontal, NativePopoverSizing.horizontalPadding)
        .padding(.bottom, NativePopoverSizing.bottomPadding)
        .frame(width: NativeCardModel.popoverWidth, alignment: .topLeading)
        .fixedSize(horizontal: false, vertical: true)
        .background(cardBackground)
        .clipShape(RoundedRectangle(cornerRadius: 30, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 30, style: .continuous).stroke(Color.white.opacity(0.06), lineWidth: 0.5))
        .offset(x: -(NativeCardModel.cardWidth + NativeCardModel.popoverGap), y: NativeCardModel.pillReservedTop + model.selectedPopoverTop)
        .transition(.asymmetric(
          insertion: .offset(x: 5).combined(with: .scale(scale: 0.992, anchor: .trailing)).combined(with: .opacity),
          removal: .offset(x: 4).combined(with: .scale(scale: 0.996, anchor: .trailing)).combined(with: .opacity)
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
    .animation(.timingCurve(0.20, 0.86, 0.26, 1, duration: 0.22), value: model.activeHistoryID)
  }

  private var cardBackground: some View {
    ZStack {
      RoundedRectangle(cornerRadius: 30, style: .continuous)
        .fill(Color(red: 0.078, green: 0.098, blue: 0.149).opacity(0.95))
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
      .clipShape(RoundedRectangle(cornerRadius: 30, style: .continuous))
      .allowsHitTesting(false)
  }

  private var edgeColor: Color {
    if model.stateVariant == .error { return Color(red: 1, green: 0.42, blue: 0.42).opacity(0.85) }
    if model.stateVariant == .warn { return Color(red: 1, green: 0.72, blue: 0.30).opacity(0.85) }
    if model.isListening || model.isDropTarget || model.attachmentEdgeFlash { return Color(red: 0.37, green: 0.78, blue: 1).opacity(0.82) }
    return Color.white.opacity(0.06)
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
        if dragBlocked { return }
        let current = NSEvent.mouseLocation
        if lastDragScreenLocation == nil {
          guard canStartPanelDrag(at: value.startLocation) else {
            dragBlocked = true
            return
          }
          lastDragScreenLocation = current
          model.beginDrag()
          return
        }
        guard let previous = lastDragScreenLocation else { return }
        lastDragScreenLocation = current
        model.movePanel(dx: current.x - previous.x, dy: current.y - previous.y)
      }
      .onEnded { _ in
        dragBlocked = false
        lastDragScreenLocation = nil
        model.endDrag()
      }
  }

  private func canStartPanelDrag(at location: CGPoint) -> Bool {
    NativeCardDragPolicy.shouldStartDrag(
      at: location,
      state: NativeCardDragPolicy.State(
        historyViewportHeight: historyViewportHeight,
        isSubmitted: model.isSubmitted,
        isFollowupInput: model.isFollowupInput,
        isListening: model.isListening,
        hasStatePill: !model.stateLabel.isEmpty
      )
    )
  }
}

enum NativeCardDragPolicy {
  struct State: Equatable {
    var historyViewportHeight: CGFloat
    var isSubmitted: Bool
    var isFollowupInput: Bool
    var isListening: Bool
    var hasStatePill: Bool
  }

  static func shouldStartDrag(at location: CGPoint, state: State) -> Bool {
    guard location.x >= 0,
          location.x <= NativeCardModel.cardWidth,
          location.y >= 0 else { return false }

    let historyTotalHeight = state.historyViewportHeight > 0 ? state.historyViewportHeight + 14 : 0
    if location.y < historyTotalHeight {
      let chipViewport = CGRect(
        x: 22,
        y: 8,
        width: NativeCardModel.cardWidth - 44,
        height: state.historyViewportHeight
      )
      return !chipViewport.contains(location)
    }

    let rowY = location.y - historyTotalHeight
    if rowY < inputRowHeight(for: state) {
      let interactiveStart = inputInteractiveStart(for: state)
      let trailingDragSliver: CGFloat = state.hasStatePill ? 0 : 12
      return location.x < interactiveStart || location.x > NativeCardModel.cardWidth - trailingDragSliver
    }

    return true
  }

  private static func inputRowHeight(for state: State) -> CGFloat {
    if state.isSubmitted { return 38 }
    if state.isFollowupInput { return 57 }
    return 64
  }

  private static func inputInteractiveStart(for state: State) -> CGFloat {
    if state.isSubmitted { return 38 }
    if state.isFollowupInput { return 40 }
    if state.isListening { return 100 }
    return 42
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
    case .heading5(let text), .heading6(let text):
      inlineTextView(text, range: range, color: Color.white.opacity(0.55))
        .font(.system(size: 13, weight: .medium))
        .padding(.top, 6)
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
      let renderedCode = NativeCodeText.renderedSource(code, language: language)
      let lineCount = renderedCode.isEmpty ? 0 : renderedCode.components(separatedBy: .newlines).count
      let codeBlockHeight = lineCount == 0 ? 24 : CGFloat(lineCount * 2 - 1) * 19.575 + 24
      ScrollView(.horizontal, showsIndicators: false) {
        NativeCodeText(code: renderedCode, language: language)
          .font(.system(size: 12.5, weight: .regular, design: .monospaced))
          .lineSpacing(20.5)
          .padding(.vertical, 12)
          .padding(.horizontal, 14)
          .frame(maxWidth: .infinity, alignment: .leading)
      }
      .frame(minHeight: codeBlockHeight, alignment: .topLeading)
      .background(
        RoundedRectangle(cornerRadius: 12, style: .continuous)
          .fill(Color.black.opacity(0.32))
      )
      .padding(.top, 5.5)
      .padding(.bottom, 6)
    case .paragraph(let text):
      inlineTextView(text, range: range, color: Color.white.opacity(followsHeading1 ? 0.55 : 0.92))
        .font(.system(size: followsHeading1 ? 13 : 14.5, weight: followsHeading1 ? .light : .regular))
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

  private func inlineText(_ value: String, color: Color) -> Text {
    Text(NativeInlineStyler.attributed(markdown: value, baseColor: color))
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
        inlineText(cells[idx], color: isHeader ? Color.white.opacity(0.55) : Color.white.opacity(0.88))
          .font(.system(size: 13, weight: isHeader ? .medium : .regular))
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

  var body: some View {
    TimelineView(.animation) { timeline in
      Text(NativeInlineStyler.attributed(
        markdown: markdown,
        baseColor: baseColor,
        range: range,
        characterBirthTimes: characterBirthTimes,
        now: timeline.date.timeIntervalSinceReferenceDate
      ))
    }
  }
}

enum NativeInlineStyler {
  static let linkColor = Color(red: 0.37, green: 0.78, blue: 1).opacity(0.92)
  static let strongColor = Color.white.opacity(0.99)
  static let emphasisColor = Color.white.opacity(0.55)
  static let deletedColor = Color.white.opacity(0.35)
  static let inlineCodeColor = Color(red: 1.0, green: 0.86, blue: 0.70).opacity(0.92)
  static let inlineCodeBackgroundColor = Color.black.opacity(0.32)

  private static let fadeDuration: TimeInterval = 0.250
  private static let linkDetector = try? NSDataDetector(types: NSTextCheckingResult.CheckingType.link.rawValue)

  static func attributed(
    markdown: String,
    baseColor: Color,
    range: Range<Int>? = nil,
    characterBirthTimes: [TimeInterval] = [],
    now: TimeInterval = 0
  ) -> AttributedString {
    var attributed = (try? AttributedString(
      markdown: markdown,
      options: AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
    )) ?? AttributedString(markdown)
    linkifyBareURLs(in: &attributed)

    let styledRanges = attributed.runs.map { run in
      StyledRun(range: run.range, style: inlineStyle(for: run, baseColor: baseColor))
    }

    guard let range, !characterBirthTimes.isEmpty else {
      for styled in styledRanges {
        apply(styled.style, foregroundOpacity: 1, to: styled.range, in: &attributed)
      }
      return attributed
    }

    var visibleOffset = range.lowerBound
    for styled in styledRanges {
      var index = styled.range.lowerBound
      while index < styled.range.upperBound, visibleOffset < range.upperBound {
        let next = attributed.index(afterCharacter: index)
        let birth = visibleOffset < characterBirthTimes.count ? characterBirthTimes[visibleOffset] : now - fadeDuration
        let age = max(0, now - birth)
        let opacity = min(1, max(0, age / fadeDuration))
        apply(styled.style, foregroundOpacity: opacity, to: index..<next, in: &attributed)
        index = next
        visibleOffset += 1
      }
    }
    return attributed
  }

  private static func inlineStyle(for run: AttributedString.Runs.Run, baseColor: Color) -> InlineStyle {
    let intent = run.inlinePresentationIntent
    var style = InlineStyle(foreground: baseColor)

    if intent?.contains(.stronglyEmphasized) == true {
      style.foreground = strongColor
    }
    if intent?.contains(.emphasized) == true {
      style.foreground = emphasisColor
    }
    if intent?.contains(.strikethrough) == true {
      style.foreground = deletedColor
    }
    if run.link != nil {
      style.foreground = linkColor
    }
    if intent?.contains(.code) == true {
      style.foreground = inlineCodeColor
      style.background = inlineCodeBackgroundColor
    }
    return style
  }

  private static func apply(
    _ style: InlineStyle,
    foregroundOpacity: Double,
    to range: Range<AttributedString.Index>,
    in attributed: inout AttributedString
  ) {
    attributed[range].foregroundColor = foregroundOpacity >= 1 ? style.foreground : style.foreground.opacity(foregroundOpacity)
    if let background = style.background {
      attributed[range].backgroundColor = background
      attributed[range].font = .system(.body, design: .monospaced)
    }
  }

  private static func linkifyBareURLs(in attributed: inout AttributedString) {
    guard let linkDetector else { return }
    let text = String(attributed.characters)
    let fullRange = NSRange(text.startIndex..<text.endIndex, in: text)
    for match in linkDetector.matches(in: text, range: fullRange) {
      guard let url = match.url,
            let stringRange = Range(match.range, in: text),
            let lowerBound = AttributedString.Index(stringRange.lowerBound, within: attributed),
            let upperBound = AttributedString.Index(stringRange.upperBound, within: attributed) else { continue }
      let attributedRange = lowerBound..<upperBound
      guard !attributed[attributedRange].runs.contains(where: { $0.link != nil }) else { continue }
      attributed[attributedRange].link = url
    }
  }

  private struct StyledRun {
    let range: Range<AttributedString.Index>
    let style: InlineStyle
  }

  private struct InlineStyle {
    var foreground: Color
    var background: Color?
  }
}

struct NativeCodeText: View {
  let code: String
  let language: String?

  var body: some View {
    Text(Self.highlighted(code, language: language))
  }

  static func highlighted(_ code: String, language: String?) -> AttributedString {
    let nsCode = code as NSString
    let fullRange = NSRange(location: 0, length: nsCode.length)
    let attributed = NSMutableAttributedString(string: code)
    attributed.addAttributes([
      .foregroundColor: themeColor(0xE6EDF3),
    ], range: fullRange)
    guard !code.isEmpty else { return AttributedString(attributed) }

    let keywordColor = themeColor(0xFF7B72)
    let functionColor = themeColor(0xD2A8FF)
    let literalColor = themeColor(0x79C0FF)
    let stringColor = themeColor(0xA5D6FF)
    let commentColor = themeColor(0x8B949E)

    apply(pattern: keywordPattern(for: language), color: keywordColor, to: attributed, in: fullRange)
    apply(pattern: functionDeclarationPattern(for: language), color: functionColor, to: attributed, in: fullRange, captureGroup: 1)
    apply(pattern: builtinCallPattern(for: language), color: literalColor, to: attributed, in: fullRange)
    apply(pattern: #"\b\d+(?:\.\d+)?\b"#, color: literalColor, to: attributed, in: fullRange)
    apply(pattern: #""(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'"#, color: stringColor, to: attributed, in: fullRange)
    apply(pattern: #"(?m)(#|//).*$"#, color: commentColor, to: attributed, in: fullRange)

    return AttributedString(attributed)
  }

  static func renderedSource(_ code: String, language: String?) -> String {
    guard language != nil else { return code }
    return code.replacingOccurrences(of: #"\s+$"#, with: "", options: .regularExpression)
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

  private static func functionDeclarationPattern(for language: String?) -> String {
    let normalized = language?.lowercased() ?? ""
    if normalized.hasPrefix("py") {
      return #"\bdef\s+([A-Za-z_][A-Za-z0-9_]*)\b"#
    }
    if normalized == "swift" || ["js", "jsx", "javascript", "ts", "tsx", "typescript"].contains(normalized) {
      return #"\bfunc(?:tion)?\s+([A-Za-z_$][A-Za-z0-9_$]*)\b"#
    }
    return #"\b(?:def|func|function)\s+([A-Za-z_$][A-Za-z0-9_$]*)\b"#
  }

  private static func builtinCallPattern(for language: String?) -> String {
    let normalized = language?.lowercased() ?? ""
    if normalized.hasPrefix("py") {
      return #"\b(print|len|range|str|int|float|dict|list|set|tuple|open|enumerate|zip|sum|min|max|json\.loads|Path)\b(?=\s*\()"#
    }
    return #"\b(print|console\.log|len|range|map|filter|reduce|min|max)\b(?=\s*\()"#
  }

  private static func themeColor(_ hex: Int, alpha: CGFloat = 1) -> NSColor {
    NSColor(
      srgbRed: CGFloat((hex >> 16) & 0xff) / 255,
      green: CGFloat((hex >> 8) & 0xff) / 255,
      blue: CGFloat(hex & 0xff) / 255,
      alpha: alpha
    )
  }

  private static func apply(
    pattern: String,
    color: NSColor,
    to attributed: NSMutableAttributedString,
    in range: NSRange,
    captureGroup: Int = 0
  ) {
    guard let regex = try? NSRegularExpression(pattern: pattern) else { return }
    regex.enumerateMatches(in: attributed.string, range: range) { match, _, _ in
      guard let match else { return }
      let targetRange = match.range(at: min(captureGroup, match.numberOfRanges - 1))
      guard targetRange.location != NSNotFound, targetRange.length > 0 else { return }
      attributed.addAttribute(.foregroundColor, value: color, range: targetRange)
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
  case heading5(String)
  case heading6(String)
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
         .heading5(let text),
         .heading6(let text),
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
    var codeFenceMarker = ""
    var inCode = false
    let lines = markdown.components(separatedBy: .newlines)
    var i = 0

    while i < lines.count {
      let raw = lines[i]
      let line = raw.trimmingCharacters(in: .whitespaces)
      if let fenceMarker = fenceMarker(in: line) {
        if inCode, fenceCloses(fenceMarker, opening: codeFenceMarker) {
          result.append(.code(codeLines.joined(separator: "\n"), language: codeLanguage))
          codeLines.removeAll()
          codeLanguage = nil
          codeFenceMarker = ""
          inCode = false
        } else if inCode {
          codeLines.append(raw)
        } else {
          inCode = true
          codeFenceMarker = fenceMarker
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
      } else if line.hasPrefix("# ") {
        result.append(.heading1(String(line.dropFirst(2))))
      } else if line.hasPrefix("## ") {
        result.append(.heading2(String(line.dropFirst(3))))
      } else if line.hasPrefix("### ") {
        result.append(.heading3(String(line.dropFirst(4))))
      } else if line.hasPrefix("#### ") {
        result.append(.heading4(String(line.dropFirst(5))))
      } else if line.hasPrefix("##### ") {
        result.append(.heading5(String(line.dropFirst(6))))
      } else if line.hasPrefix("###### ") {
        result.append(.heading6(String(line.dropFirst(7))))
      } else if let bullet = unorderedListItem(line) {
        result.append(.bullet(bullet))
      } else if let ordered = orderedListItem(line) {
        result.append(.numbered(ordered.marker, ordered.text))
      } else if let quote = blockquoteText(line) {
        var quoteLines = [quote]
        i += 1
        while i < lines.count {
          let next = lines[i].trimmingCharacters(in: .whitespaces)
          guard let nextQuote = blockquoteText(next) else { break }
          quoteLines.append(nextQuote)
          i += 1
        }
        result.append(.blockquote(quoteLines.joined(separator: "\n")))
        continue
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
        let paragraph = paragraphLines.joined(separator: "\n")
        if i < lines.count,
           let headingLevel = setextHeadingLevel(lines[i].trimmingCharacters(in: .whitespaces)) {
          result.append(headingLevel == 1 ? .heading1(paragraph) : .heading2(paragraph))
          i += 1
        } else {
          result.append(.paragraph(paragraph))
        }
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
    let markerLength = fenceMarker(in: line)?.count ?? 3
    let raw = String(line.dropFirst(markerLength)).trimmingCharacters(in: .whitespacesAndNewlines)
    let language = raw.split(whereSeparator: { $0.isWhitespace }).first.map(String.init) ?? ""
    return language.isEmpty ? nil : language.lowercased()
  }

  private static func isFenceLine(_ line: String) -> Bool {
    fenceMarker(in: line) != nil
  }

  private static func fenceMarker(in line: String) -> String? {
    guard let first = line.first, first == "`" || first == "~" else { return nil }
    let marker = line.prefix { $0 == first }
    return marker.count >= 3 ? String(marker) : nil
  }

  private static func fenceCloses(_ candidate: String, opening: String) -> Bool {
    candidate.first == opening.first && candidate.count >= opening.count
  }

  private static func isHeadingLine(_ line: String) -> Bool {
    (1...6).contains { level in
      line.hasPrefix(String(repeating: "#", count: level) + " ")
    }
  }

  private static func setextHeadingLevel(_ line: String) -> Int? {
    guard !line.isEmpty else { return nil }
    if line.allSatisfy({ $0 == "=" }) { return 1 }
    if line.allSatisfy({ $0 == "-" }) { return 2 }
    return nil
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

  private static func unorderedListItem(_ line: String) -> String? {
    guard let marker = line.first, marker == "-" || marker == "*" || marker == "+" else { return nil }
    let rest = line.dropFirst()
    guard rest.first?.isWhitespace == true else { return nil }
    let text = rest.drop { $0.isWhitespace }
    return text.isEmpty ? nil : String(text)
  }

  private static func blockquoteText(_ line: String) -> String? {
    guard line.first == ">" else { return nil }
    let text = line.dropFirst().drop { $0.isWhitespace }
    return String(text)
  }

  private static func isParagraphContinuation(_ line: String) -> Bool {
    if line.isEmpty { return false }
    if isHeadingLine(line) { return false }
    if setextHeadingLevel(line) != nil { return false }
    if unorderedListItem(line) != nil || blockquoteText(line) != nil { return false }
    if isFenceLine(line) { return false }
    if line == "---" || line == "***" || line == "___" { return false }
    if orderedListItem(line) != nil { return false }
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
  let onDropFileURLs: ([URL]) -> Bool

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
    field.target = context.coordinator
    field.action = #selector(Coordinator.commitTextField(_:))
    field.onEnterDown = onEnterDown
    field.onEnterUp = onEnterUp
    field.onEscape = onEscape
    field.onPasteImage = onPasteImage
    field.onDropFileURLs = onDropFileURLs
    field.registerForDraggedTypes([
      .fileURL,
      NSPasteboard.PasteboardType("NSFilenamesPboardType"),
    ])
    context.coordinator.onEnterDown = onEnterDown
    context.coordinator.onEnterUp = onEnterUp
    context.coordinator.onEscape = onEscape
    return field
  }

  func updateNSView(_ nsView: NativeTextField, context: Context) {
    if nsView.stringValue != text {
      nsView.stringValue = text
    }
    nsView.placeholderString = placeholder
    nsView.isEnabled = !isDisabled
    nsView.target = context.coordinator
    nsView.action = #selector(Coordinator.commitTextField(_:))
    if isDisabled {
      nsView.resignEditingIfNeeded()
    }
    nsView.onEnterDown = onEnterDown
    nsView.onEnterUp = onEnterUp
    nsView.onEscape = onEscape
    nsView.onPasteImage = onPasteImage
    nsView.onDropFileURLs = onDropFileURLs
    context.coordinator.onEnterDown = onEnterDown
    context.coordinator.onEnterUp = onEnterUp
    context.coordinator.onEscape = onEscape
  }

  func makeCoordinator() -> Coordinator {
    Coordinator(text: $text)
  }

  final class Coordinator: NSObject, NSTextFieldDelegate {
    @Binding var text: String
    var onEnterDown: (() -> Void)?
    var onEnterUp: (() -> Void)?
    var onEscape: (() -> Void)?

    init(text: Binding<String>) {
      _text = text
    }

    func controlTextDidChange(_ obj: Notification) {
      guard let field = obj.object as? NSTextField else { return }
      text = field.stringValue
    }

    @objc func commitTextField(_ sender: NSTextField) {
      text = sender.stringValue
      onEnterDown?()
      onEnterUp?()
    }

    func control(_ control: NSControl, textView: NSTextView, doCommandBy commandSelector: Selector) -> Bool {
      switch commandSelector {
      case #selector(NSResponder.insertNewline(_:)),
           #selector(NSResponder.insertNewlineIgnoringFieldEditor(_:)):
        text = control.stringValue
        onEnterDown?()
        onEnterUp?()
        return true
      case #selector(NSResponder.cancelOperation(_:)):
        onEscape?()
        return true
      default:
        return false
      }
    }
  }
}

final class NativeTextField: NSTextField {
  var onEnterDown: (() -> Void)?
  var onEnterUp: (() -> Void)?
  var onEscape: (() -> Void)?
  var onPasteImage: (() -> Bool)?
  var onDropFileURLs: (([URL]) -> Bool)?
  private var enterWasDown = false

  func resignEditingIfNeeded() {
    enterWasDown = false
    guard let window else { return }

    if let editor = currentEditor(), window.firstResponder === editor {
      abortEditing()
      window.makeFirstResponder(nil)
    } else if window.firstResponder === self {
      window.makeFirstResponder(nil)
    }
  }

  override func keyDown(with event: NSEvent) {
    if hasMarkedInputText {
      super.keyDown(with: event)
      return
    }

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

  private var hasMarkedInputText: Bool {
    guard let editor = currentEditor() as? NSTextInputClient else { return false }
    return editor.hasMarkedText()
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

  override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
    Self.fileURLs(from: sender.draggingPasteboard).isEmpty ? [] : .copy
  }

  override func draggingUpdated(_ sender: NSDraggingInfo) -> NSDragOperation {
    Self.fileURLs(from: sender.draggingPasteboard).isEmpty ? [] : .copy
  }

  override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
    let urls = Self.fileURLs(from: sender.draggingPasteboard)
    guard !urls.isEmpty else { return super.performDragOperation(sender) }
    _ = onDropFileURLs?(urls)
    return true
  }

  private static func fileURLs(from pasteboard: NSPasteboard) -> [URL] {
    let options: [NSPasteboard.ReadingOptionKey: Any] = [.urlReadingFileURLsOnly: true]
    if let urls = pasteboard.readObjects(forClasses: [NSURL.self], options: options) as? [NSURL] {
      return urls.map { $0 as URL }
    }
    if let paths = pasteboard.propertyList(forType: NSPasteboard.PasteboardType("NSFilenamesPboardType")) as? [String] {
      return paths.map { URL(fileURLWithPath: $0) }
    }
    if let raw = pasteboard.string(forType: .fileURL),
       let url = URL(string: raw),
       url.isFileURL {
      return [url]
    }
    return []
  }
}
