import SwiftUI

// What he was doing in a stretch of the day, said in his own words (ADR 0302, `POST /inherent/day/answer`). She fills almost all of the day herself and asks him only where she is unsure, so this
// entry is the rare one: it is the last thing a stretch's row opens to (and the last of the menu of a long press), and nothing on the bar or in the closed list invites him to it. It opens a
// sheet with the stretch's hours and one field; the brain writes a span of the doing lane, the days are asked for again, and what he wrote is shown by the words the list already has for
// what he was doing. The phone keeps no span of its own.

/// The stretch the sheet is about: the day it is in, and the hours it starts with in whole minutes of that day by the brain's wall clock (`TLClock`).
struct TLSayWhat: Identifiable {
    let id: String                  // the row's
    let day: String                 // `YYYY-MM-DD`
    let clock: TLClock
    let from: Int, to: Int          // the row's own hours, the start no earlier than the day began and the end no later than the latest
    let limit: Int                  // the latest the end can be: this minute on today, else the day's last

    /// the most characters the brain takes
    static let cap = 200
    /// the day's last minute: 24:00 is the next day's
    static let lastMinute = 1439
    /// what lasts and happened to him: the night, a place, the way, work, a call, a run, the sessions of Claude Code, a chat with Claude. Not her rows (a talk, a reminder), not a moment (a picture,
    /// a payment, a bus waiting), not an event, not a stop on the way, and not what has not begun.
    private static let kinds: Set<TLEntry.Kind> = [.sleep, .stay, .trip, .work, .call, .workout, .code, .claude]

    /// The stretch a row stands for as whole minutes of its day, the end no later than now (on today) or the day's last minute; none where the row is not a stretch or is under a minute long.
    /// A place with no end goes on to the day's last minute (only on a day gone by: on today it goes on to now).
    static func range(of e: TLEntry, now: Double?) -> (from: Int, to: Int)? {
        guard kinds.contains(e.kind), !e.later else { return nil }
        let end = e.t1 ?? (e.kind == .stay ? DAY : e.t0)
        let from = Int(max(e.t0, 0).rounded(.down)), to = Int(min(end, now ?? DAY, Double(lastMinute)).rounded(.down))
        return to > from ? (from, to) : nil
    }

    init?(_ e: TLEntry, in r: DayReply, now: Double?) {
        guard let range = Self.range(of: e, now: now) else { return nil }
        id = e.id; day = r.date; clock = TLClock(r); from = range.from; to = range.to
        limit = Int(min(now ?? DAY, Double(Self.lastMinute)).rounded(.down))
    }

    /// what he wrote as it is sent: trimmed, and none when it is blank or longer than the brain takes
    static func words(_ raw: String) -> String? {
        let t = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        return t.isEmpty || t.unicodeScalars.count > cap ? nil : t
    }
    /// what the field keeps of what he typed: the first `cap` characters
    static func capped(_ raw: String) -> String {
        var t = raw
        while t.unicodeScalars.count > cap { t.removeLast() }
        return t
    }

    /// the moment of a minute of this day, by the brain's wall clock
    func date(_ minute: Int) -> Date {
        clock.cal.date(bySettingHour: minute / 60, minute: minute % 60, second: 0, of: clock.origin) ?? clock.origin.addingTimeInterval(Double(minute) * 60)
    }
    func minute(of d: Date) -> Int { Int(clock.minute(d).rounded(.down)) }
    func ms(_ minute: Int) -> Int64 { Int64((date(minute).timeIntervalSince1970 * 1000).rounded()) }
    /// the hours are ones the brain takes: inside the day, the end after the start and not after the latest
    func fits(_ a: Int, _ b: Int) -> Bool { a >= 0 && b > a && b <= limit }
    /// what the pickers allow: the start from the day's beginning to a minute before the end, the end from a minute after the start to the latest
    func starts(before b: Int) -> ClosedRange<Int> { 0...max(0, b - 1) }
    func ends(after a: Int) -> ClosedRange<Int> { (a + 1)...max(a + 1, limit) }
}

extension TimelineState {
    static let sayNote = "没存上，稍后再试"

    /// 这段在干嘛: the sheet opens on the row's hours
    func beginSayWhat(_ e: TLEntry) {
        guard let r = reply, let s = TLSayWhat(e, in: r, now: nowMinute(in: r)) else { return }
        sayNote = nil
        sayWhat = s
    }

    /// 记下. Nothing goes that the brain would refuse (blank words, too many, hours outside the day), and one word at a time. When the brain takes it the sheet closes and the days are asked for
    /// again: its reply is how the stretch stands, and the phone puts nothing on the day itself. When it will not, the sheet stays, with his words in it, and says so.
    func submitSayWhat(_ s: TLSayWhat, from: Int, to: Int, text raw: String) {
        guard !sayBusy, let text = TLSayWhat.words(raw), s.fits(from, to) else { return }
        sayBusy = true; sayNote = nil
        Task { @MainActor [self] in
            defer { sayBusy = false }
            switch await brain.answer(s.day, s.ms(from), s.ms(to), text) {
            case .done:
                sayWhat = nil
                Haptic.notify(.success)
                brain.refetch()
            case .failed:
                sayNote = Self.sayNote
                Haptic.notify(.warning)
            }
        }
    }
}

// MARK: - the sheet

private let ink = SetColor.ink
private let muted = SetColor.muted
private let faint = SetColor.faint

/// the sheet: the stretch's start and end as two pickers of hours and minutes, his words in one field with the keyboard up, and 记下
struct TLSayWhatSheet: View {
    let tl: TimelineState
    let s: TLSayWhat
    @State private var from: Date
    @State private var to: Date
    @State private var text = ""
    @FocusState private var typing: Bool

    init(tl: TimelineState, s: TLSayWhat) {
        self.tl = tl; self.s = s
        _from = State(initialValue: s.date(s.from)); _to = State(initialValue: s.date(s.to))
    }

    private var a: Int { s.minute(of: from) }
    private var b: Int { s.minute(of: to) }
    private var ready: Bool { TLSayWhat.words(text) != nil && s.fits(a, b) && !tl.sayBusy }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("这段在干嘛").font(.system(size: 17, weight: .semibold)).foregroundStyle(ink)
            HStack(spacing: 10) {
                Text("从").font(.system(size: 14)).foregroundStyle(muted)
                picker($from, s.starts(before: b))
                Text("到").font(.system(size: 14)).foregroundStyle(muted)
                picker($to, s.ends(after: a))
                Spacer(minLength: 0)
            }
            TextField("", text: $text, prompt: Text("比如：躺着休息").foregroundStyle(faint))
                .focused($typing).submitLabel(.done).onSubmit(save)
                .font(.system(size: 15)).foregroundStyle(ink).tint(ink)
                .padding(.horizontal, 14).frame(height: 44)
                .background(Color.white.opacity(0.08), in: Capsule())
                .overlay(Capsule().stroke(ink.opacity(typing ? 0.45 : 0.12), lineWidth: 1))
                .onChange(of: text) { _, now in let kept = TLSayWhat.capped(now); if kept != now { text = kept } }
            if let n = tl.sayNote { Text(n).font(.system(size: 12.5)).foregroundStyle(SetColor.red.opacity(0.92)).transition(.opacity) }
            Button(action: save) {
                ZStack {
                    if tl.sayBusy { ProgressView().tint(AT.navy) } else { Text("记下").font(.system(size: 15, weight: .semibold)).foregroundStyle(AT.navy) }
                }
                .frame(maxWidth: .infinity).frame(height: 46)
                .background(AT.pale.opacity(ready || tl.sayBusy ? 1 : 0.35), in: Capsule())
                .contentShape(Capsule())
            }
            .buttonStyle(PressScale()).disabled(!ready)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 20).padding(.top, 30)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .animation(.easeOut(duration: 0.2), value: tl.sayNote)
        .background(SetColor.bg.ignoresSafeArea())
        .environment(\.calendar, s.clock.cal).environment(\.timeZone, s.clock.cal.timeZone).environment(\.locale, Locale(identifier: "zh_CN"))
        .presentationDetents([.medium])
        .presentationBackground(SetColor.bg)
        .presentationCornerRadius(14)
        .presentationDragIndicator(.visible)
        .preferredColorScheme(.dark)
        .onAppear { DispatchQueue.main.async { typing = true } }
    }

    private func picker(_ date: Binding<Date>, _ minutes: ClosedRange<Int>) -> some View {
        DatePicker("", selection: date, in: s.date(minutes.lowerBound)...s.date(minutes.upperBound), displayedComponents: .hourAndMinute).labelsHidden().datePickerStyle(.compact).frame(minHeight: 44)
    }

    private func save() { tl.submitSayWhat(s, from: a, to: b, text: text) }
}
