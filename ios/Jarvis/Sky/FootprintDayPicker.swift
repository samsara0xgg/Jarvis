import SwiftUI

/// The 日历 button at the left of the strip on 今天 and 地图 and the calendar it opens in a sheet: the system's, with the days up to today to pick from. The day picked becomes the page's day,
/// as it does when the list is swiped, and the sheet goes.
struct FootprintDayPicker: View {
    let ui: StarUI
    @State private var open = false

    var body: some View {
        TopGlassButton(symbol: "calendar", label: Words.FootprintDay.pickDay) { Haptic.selection(); open = true }
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
