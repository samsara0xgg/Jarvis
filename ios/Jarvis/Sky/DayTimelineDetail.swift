import SwiftUI

// The page of one row of the day, over the list (Sky/DayTimeline.swift slides it in): a way back with the row's title, under it its hours, how long and where, and then what the row opens to
// (Sky/DayTimelineOpen.swift) with the buttons of what can be done with it. The header, the bar and the day's numbers stay above it; the bar marks the row's span. A step to the row before or
// after changes only what is under the head (the head's words change in place), so the way back and the steps stay under the finger.

private let ink = SetColor.ink
private let muted = SetColor.muted

extension TLEntry {
    /// the line under the page's title: the hours it covered (or when it began, while it goes on), how long it was or what it came to, and where
    var pageLine: String {
        let hours: String
        if going { hours = time + " 起" } else if let t1, t1 > t0 { hours = time + "–" + tlEnd(t1) } else { hours = time }
        return [hours, right, detail ?? ""].filter { !$0.isEmpty }.joined(separator: " · ")
    }
}

struct TLDetailPage: View {
    let e: TLEntry
    let tl: TimelineState
    let engine: StarEngine
    let back: () -> Void
    var step: (Int) -> Void = { _ in }          // to the row before (-1) or after (+1) it in the list
    var steps: (up: Bool, down: Bool) = (false, false)          // whether there is one
    var turn: Edge?                                             // the edge the body of a step comes in from: below for the row after, above for the one before (none: a turn from the bar, a cross-fade)
    @Environment(\.accessibilityReduceMotion) private var reduce

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .top, spacing: 0) {
                Button(action: back) {
                    Image(systemName: "chevron.left").font(.system(size: 17, weight: .semibold)).foregroundStyle(ink)
                        .frame(width: 44, height: 44).contentShape(Rectangle())
                }
                .buttonStyle(PressScale())
                .accessibilityLabel("返回")
                Text(e.title).font(.system(size: 17, weight: .semibold)).foregroundStyle(e.unknown ? muted : ink)
                    .lineLimit(3).fixedSize(horizontal: false, vertical: true).padding(.top, 10).contentTransition(.opacity)
                Spacer(minLength: 8)
                // the row before and the row after, as Mail goes from message to message
                HStack(spacing: 0) {
                    stepButton("chevron.up", "上一段", on: steps.up) { step(-1) }
                    stepButton("chevron.down", "下一段", on: steps.down) { step(1) }
                }
            }
            .padding(.leading, 16).padding(.trailing, 18)
            Text(e.pageLine).font(.system(size: 12.5)).foregroundStyle(muted).fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 30).padding(.top, 2).contentTransition(.opacity)
            ZStack(alignment: .top) {
                ScrollView(showsIndicators: false) {
                    TLOpenView(e: e, tl: tl, engine: engine, mapWidth: TLLayout.barW)
                        .padding(.horizontal, 30).padding(.top, 18).padding(.bottom, 80)
                }
                .scrollBounceBehavior(.basedOnSize)
                .id(e.id).transition(bodyTurn)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
            .clipped()
        }
        .padding(.top, TLLayout.fadeTop)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .animation(TLMotion.step(reduce: reduce), value: e.id)
        .accessibilityElement(children: .contain)
        .accessibilityAction(.escape, back)
    }

    /// the body of a step: the one that leaves goes a hand's breadth the way the page is pushed and fades, the one that comes arrives from the other side; a turn from the bar is a cross-fade
    private var bodyTurn: AnyTransition {
        guard let turn, !reduce else { return .opacity }
        let d = turn == .bottom ? 1.0 : -1.0
        return .asymmetric(insertion: .opacity.combined(with: .offset(y: d * TLMotion.stepShift)), removal: .opacity.combined(with: .offset(y: -d * TLMotion.stepShift)))
    }

    private func stepButton(_ symbol: String, _ label: String, on: Bool, _ action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol).font(.system(size: 15, weight: .semibold)).foregroundStyle(on ? ink : muted.opacity(0.4))
                .frame(width: 40, height: 44).contentShape(Rectangle())
        }
        .buttonStyle(PressScale())
        .disabled(!on)
        .accessibilityLabel(label)
    }
}
