import SwiftUI

/// The glass button at the right of the footprint page's head and the calendar it opens in a sheet: the system's, with the days up to today to pick from. The day picked becomes the page's day,
/// as it does when the list is swiped, and the sheet goes.
struct FootprintDayPicker: View {
    let ui: StarUI
    @State private var open = false

    var body: some View {
        Button { Haptic.selection(); open = true } label: {
            Image(systemName: "calendar").font(.system(size: 15, weight: .regular)).foregroundStyle(TLPaint.icon.color(0.8))
                .frame(width: 36, height: 36).glass(Circle())
        }
        .buttonStyle(PressScale())
        .accessibilityLabel(Words.FootprintDay.pickDay)
        .sheet(isPresented: $open) { FootprintDaySheet(ui: ui) { open = false } }
    }

    /// the day number (0 is the calendar's first day, as the timeline counts) of the day a date is in
    static func day(of date: Date) -> Int {
        let c = Cal.cur
        return c.cal.dateComponents([.day], from: c.origin, to: c.cal.startOfDay(for: date)).day ?? 0
    }
}

private struct FootprintDaySheet: View {
    let ui: StarUI
    let done: () -> Void
    @State private var picked: Date

    init(ui: StarUI, done: @escaping () -> Void) {
        self.ui = ui; self.done = done
        _picked = State(initialValue: Cal.cur.date(ui.tl.day))
    }

    var body: some View {
        DatePicker("", selection: $picked, in: ...Cal.cur.date(ui.tl.today), displayedComponents: .date)
            .datePickerStyle(.graphical).labelsHidden()
            .padding(.horizontal, 16)
            .presentationDetents([.medium]).presentationDragIndicator(.visible)
            .onChange(of: picked) { _, d in ui.tl.setDay(FootprintDayPicker.day(of: d)); done() }
    }
}
