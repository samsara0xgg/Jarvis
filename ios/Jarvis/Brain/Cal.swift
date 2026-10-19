import Foundation

/// The calendar the dial lives in. Day 0 is the date of `origin`; an engine time is minutes since that date's local midnight, so day d starts at d * 1440.
/// Unpaired it is the prototype's Thursday 10.8 at 21:14, paired it is the phone's own today and clock.
/// The day origin is fixed when the world is entered: a session that runs past midnight keeps counting up from it (tomorrow is day 1) and the month grid stays on the month it started in.
final class Cal {
    /// What every calendar helper reads. `StarEngine` swaps it when the world changes between the sample and the real day.
    static var cur = Cal(real: BrainLink.shared.hasToken)

    let real: Bool
    let cal: Calendar
    let origin: Date
    let wd0: Int                       // Monday-first weekday of day 0 (Thursday = 3)
    let year: Int, mFirst: Int, mLast: Int, firstCol: Int
    private var dates: [Int: (Int, Int)] = [:]
    private var anchor: (at: Double, t: Double)?

    #if DEBUG
    /// `-fakeNow 2026-11-20T14:30` moves the phone's clock for screenshots of other months.
    private static let skew: TimeInterval = {
        let a = ProcessInfo.processInfo.arguments
        guard let i = a.firstIndex(of: "-fakeNow"), i + 1 < a.count else { return 0 }
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX"); f.dateFormat = "yyyy-MM-dd'T'HH:mm"
        return f.date(from: a[i + 1])?.timeIntervalSinceNow ?? 0
    }()
    #else
    private static let skew: TimeInterval = 0
    #endif
    static func now() -> Date { Date().addingTimeInterval(skew) }

    init(real: Bool) {
        self.real = real
        var c = Calendar(identifier: .gregorian)
        c.timeZone = .current
        cal = c
        origin = real ? c.startOfDay(for: Cal.now()) : c.date(from: DateComponents(year: 2026, month: 10, day: 8))!
        wd0 = (c.component(.weekday, from: origin) + 5) % 7
        let day = c.component(.day, from: origin)
        year = c.component(.year, from: origin)
        mFirst = 1 - day
        mLast = mFirst + (c.range(of: .day, in: .month, for: origin)?.count ?? 30) - 1
        firstCol = ((wd0 + mFirst) % 7 + 7) % 7
    }

    var rows: Int { (firstCol + (mLast - mFirst + 1) + 6) / 7 }
    func date(_ d: Int) -> Date { cal.date(byAdding: .day, value: d, to: origin)! }
    func dateOf(_ d: Int) -> (Int, Int) {
        if let o = dates[d] { return o }
        let dt = date(d), o = (cal.component(.month, from: dt), cal.component(.day, from: dt))
        dates[d] = o
        return o
    }
    func weekday(_ d: Int) -> Int { ((wd0 + d) % 7 + 7) % 7 }
    /// the `date=` the brain is asked for
    func iso(_ d: Int) -> String {
        let c = cal.dateComponents([.year, .month, .day], from: date(d))
        return String(format: "%04d-%02d-%02d", c.year!, c.month!, c.day!)
    }
    /// The engine time of a real instant: the day by the calendar and the minute by the clock on the wall, so 09:00 is 540 on a day with a clock change as on any other (that day has the
    /// minutes it shows, 1440 of them, and not the hours it lasted).
    func t(_ at: Date) -> Double { Cal.wall(at, in: cal, from: origin) }
    /// minutes by the wall clock of `cal`: the whole days from the day `origin` is in, and the minute of the day that the clock on the wall shows (the repeated hour of a clock going back
    /// counts as the hour it shows)
    static func wall(_ at: Date, in cal: Calendar, from origin: Date) -> Double {
        let start = cal.startOfDay(for: at), d = cal.dateComponents([.day], from: origin, to: start).day ?? 0
        let c = cal.dateComponents([.hour, .minute, .second, .nanosecond], from: at)
        return Double(d) * DAY + Double(c.hour ?? 0) * 60 + Double(c.minute ?? 0) + (Double(c.second ?? 0) + Double(c.nanosecond ?? 0) / 1e9) / 60
    }
    func t(ms: Int64) -> Double { t(Date(timeIntervalSince1970: Double(ms) / 1000)) }
    /// now, in engine minutes; the calendar is asked once every few seconds and the clock carries it between
    func nowT() -> Double {
        let n = Date().timeIntervalSince1970
        if let a = anchor, n - a.at < 5 { return a.t + (n - a.at) / 60 }
        let v = t(Cal.now())
        anchor = (n, v)
        return v
    }
}
