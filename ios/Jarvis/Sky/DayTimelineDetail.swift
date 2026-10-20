import SwiftUI

// The page of one row of the day, over the list (Sky/DayTimeline.swift slides it in): a way back with the row's title, under it its hours, how long and where, and then what the row opens to
// (Sky/DayTimelineOpen.swift) with the buttons of what can be done with it. The header, the bar and the day's numbers stay above it; the bar marks the row's span.

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
                    .lineLimit(3).fixedSize(horizontal: false, vertical: true).padding(.top, 10)
                Spacer(minLength: 0)
            }
            .padding(.leading, 16).padding(.trailing, 30)
            Text(e.pageLine).font(.system(size: 12.5)).foregroundStyle(muted).fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 30).padding(.top, 2)
            ScrollView(showsIndicators: false) {
                TLOpenView(e: e, tl: tl, engine: engine, mapWidth: TLLayout.barW)
                    .padding(.horizontal, 30).padding(.top, 18).padding(.bottom, 80)
            }
            .scrollBounceBehavior(.basedOnSize)
        }
        .padding(.top, TLLayout.fadeTop)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .accessibilityElement(children: .contain)
        .accessibilityAction(.escape, back)
    }
}
