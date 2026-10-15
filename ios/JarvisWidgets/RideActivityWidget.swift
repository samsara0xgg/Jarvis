import ActivityKit
import SwiftUI
import WidgetKit

// The bus he is riding, on the lock screen and in the Dynamic Island, in the app's night sky. The content comes from the brain's pushes
// (RideAttributes); one stop before his, the whole thing turns amber and says to get off.

private typealias Ride = RideAttributes.ContentState

private let arriveBg = Color(red: 58 / 255, green: 36 / 255, blue: 14 / 255)

private func routeName(_ state: Ride, _ attributes: RideAttributes) -> String { "\(state.route ?? attributes.route) 路" }

private func headline(_ state: Ride) -> String {
    if state.isEnded { return "行程结束" }
    if state.isArriving { return "下一站下车" }
    return state.nextStop.map { "下一站 \($0)" } ?? "在路上"
}

/// his stop and how many stops are left to it
private func detail(_ state: Ride) -> String {
    guard let stop = state.alightStop else { return state.isEnded ? "" : "还没说在哪站下车" }
    if state.isEnded { return "在 \(stop) 下车" }
    if state.isArriving { return stop }
    return state.stopsLeft.map { "到 \(stop) 还有 \($0) 站" } ?? "到 \(stop)"
}

/// how far to trust the minutes: plain when the bus has no live position, quiet when it is only a prediction
private func trust(_ state: Ride) -> some View {
    Text(state.trustNote).font(.system(size: 12)).foregroundStyle(state.hasLivePosition ? Night.muted : Night.amber.opacity(0.85)).lineLimit(1)
}

/// the route number as a small plate
private struct RoutePlate: View {
    let text: String
    let arriving: Bool
    var body: some View {
        Text(text).font(mono(15)).foregroundStyle(arriving ? arriveBg : Night.ink).lineLimit(1).fixedSize()
            .padding(.horizontal, 10).padding(.vertical, 5)
            .background(arriving ? AnyShapeStyle(Night.amber) : AnyShapeStyle(Color.white.opacity(0.12)), in: Capsule())
    }
}

private struct RideLockScreen: View {
    let state: Ride
    let attributes: RideAttributes

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .center, spacing: 14) {
                RoutePlate(text: routeName(state, attributes), arriving: state.isArriving)
                VStack(alignment: .leading, spacing: 3) {
                    Text(headline(state)).font(serif(26)).foregroundStyle(state.isArriving ? Night.amber : Night.ink).lineLimit(1).minimumScaleFactor(0.7)
                    if !detail(state).isEmpty { Text(detail(state)).font(.system(size: 14)).foregroundStyle(Night.ink.opacity(0.84)).lineLimit(1) }
                }
                Spacer(minLength: 8)
                if !state.isEnded, let minutes = state.minutesText {
                    Text(minutes).font(serif(24)).foregroundStyle(state.isArriving ? Night.amber : Night.ink).lineLimit(1).minimumScaleFactor(0.6)
                }
            }
            if !state.isEnded && !state.trustNote.isEmpty { trust(state) }
        }
        .padding(.horizontal, 18).padding(.vertical, 14)
    }
}

/// what the island's small end says: minutes to his stop, or 「下车」 one stop before
private func shortMinutes(_ state: Ride) -> String {
    if state.isEnded { return "到了" }
    if state.isArriving { return "下车" }
    guard let m = state.minutesToAlight else { return "—" }
    return m <= 0 ? "到了" : (state.hasLiveArrival ? "" : "约") + "\(m)′"
}

struct RideLiveActivityWidget: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: RideAttributes.self) { context in
            RideLockScreen(state: context.state, attributes: context.attributes)
                .activityBackgroundTint(context.state.isArriving ? arriveBg : Night.bg)
                .activitySystemActionForegroundColor(Night.ink)
        } dynamicIsland: { context in
            let s = context.state, tint = s.isArriving ? Night.amber : Night.ink
            return DynamicIsland {
                DynamicIslandExpandedRegion(.leading) { RoutePlate(text: routeName(s, context.attributes), arriving: s.isArriving).padding(.leading, 4) }
                DynamicIslandExpandedRegion(.trailing) {
                    if !s.isEnded, let minutes = s.minutesText { Text(minutes).font(serif(22)).foregroundStyle(tint).lineLimit(1).minimumScaleFactor(0.6) }
                }
                DynamicIslandExpandedRegion(.center) {
                    Text(headline(s)).font(serif(22)).foregroundStyle(tint).lineLimit(1).minimumScaleFactor(0.7)
                }
                DynamicIslandExpandedRegion(.bottom) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(detail(s)).font(.system(size: 14)).foregroundStyle(Night.ink.opacity(0.84)).lineLimit(1)
                        if !s.isEnded && !s.trustNote.isEmpty { trust(s) }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            } compactLeading: {
                Text(s.route ?? context.attributes.route).font(mono(13)).foregroundStyle(tint)
            } compactTrailing: {
                Text(shortMinutes(s)).font(mono(12)).foregroundStyle(tint)
            } minimal: {
                Image(systemName: "bus.fill").font(.system(size: 12)).foregroundStyle(tint)
            }
            .keylineTint(Night.amber)
        }
    }
}
