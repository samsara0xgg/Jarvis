import SwiftUI

// What the day's header says besides her sentence and the bar: after the date, `今天` (or on another day the way back to it) and the day's weather; on the row of the grabber how many
// of the day's rows wait on him; after the day's numbers what Health reported.

private let ink = SetColor.ink
private let muted = SetColor.muted
private let faint = SetColor.faint
private let amber = Color(red: 1, green: 201 / 255, blue: 143 / 255)

/// the way back: the warm dot of now and its words, in a capsule
struct TLBackLabel: View {
    let text: String

    var body: some View {
        HStack(spacing: 6) {
            Circle().fill(Color(red: 1, green: 243 / 255, blue: 214 / 255)).frame(width: 6, height: 6)
                .shadow(color: Color(red: 1, green: 228 / 255, blue: 170 / 255).opacity(0.7), radius: 4)
            Text(text).font(.system(size: 12.5, weight: .semibold)).foregroundStyle(ink)
        }
        .padding(.leading, 10).padding(.trailing, 12).frame(height: 26).background(Color.white.opacity(0.1), in: Capsule())
    }
}

/// after the date in the top row: `今天` on today, on another day the way back to it, and the day's weather. A tap on the weather says for a moment when the sun rose and set, in the
/// place of the tag and the weather (there is no room for it beside them). `room` is what the row leaves after the date: the weather gives up its word, and then itself, before it
/// crowds the settings button.
struct TLDayHead: View {
    let tl: TimelineState
    let room: Double

    /// how much of the weather `room` holds, after the tag or the way back and its 10 pt: all of it (2), the temperatures (1), none (0)
    static func weatherFit(_ w: TLWeather, today: Bool, room: Double) -> Int {
        let tag = today ? tlWidth("今天", size: 12, weight: .semibold) + 18 : tlWidth("回到今天", size: 12.5, weight: .semibold) + 34
        let icon = w.symbol == nil ? 0 : 16.0, temps = w.temps.isEmpty ? 0 : tlWidth(w.temps, size: 11.5, weight: .medium, mono: true)
        let word = w.word.map { tlWidth($0, size: 12.5) + 5 } ?? 0
        let left = room - tag - 10 - 6                          // and a little to spare
        return icon + word + temps <= left ? 2 : icon + temps <= left ? 1 : 0
    }

    var body: some View {
        let today = tl.day == tl.today
        let reply = tl.reply
        let weather = reply.flatMap { DayTimelineModel.weatherOf($0.weather) }
        let sun = reply.flatMap { DayTimelineModel.sun(of: $0, day: tl.day) }?.text
        let saying = tl.sunSaid && sun != nil
        let fit = weather.map { Self.weatherFit($0, today: today, room: room) } ?? 0
        HStack(spacing: 10) {
            if !saying {
                ZStack(alignment: .leading) {
                    if today {
                        Text("今天").font(.system(size: 12, weight: .semibold)).foregroundStyle(ink.opacity(0.9)).padding(.horizontal, 9).frame(height: 22)
                            .background(Color.white.opacity(0.1), in: Capsule()).transition(.opacity)
                    } else {
                        Button { tl.goToday() } label: { TLBackLabel(text: "回到今天").contentShape(Rectangle().inset(by: -9)) }
                            .buttonStyle(PressScale()).transition(.opacity)
                    }
                }
                .fixedSize().transition(.opacity)
            }
            if saying, let sun {
                Text(sun).font(AT.mono(11.5)).foregroundStyle(muted).lineLimit(1).minimumScaleFactor(0.8).transition(.opacity)
            } else if let weather, fit > 0 {
                TLWeatherLine(w: weather, word: fit == 2).transition(.opacity)
                    .contentShape(Rectangle().inset(by: -10))
                    .onTapGesture { if sun != nil { tl.saySun() } }
                    .accessibilityAddTraits(sun != nil ? .isButton : [])
                    .accessibilityHint(sun != nil ? "显示日出和日落" : "")
            }
        }
        .animation(.easeOut(duration: 0.2), value: today)
        .animation(.easeOut(duration: 0.25), value: weather)
    }
}

/// `多云 14° / 8°`, quiet (`word` false: without the word)
struct TLWeatherLine: View {
    let w: TLWeather
    let word: Bool

    var body: some View {
        HStack(spacing: 5) {
            if let s = w.symbol { Image(systemName: s).font(.system(size: 12)).foregroundStyle(muted) }
            if word, let t = w.word { Text(t).font(.system(size: 12.5)).foregroundStyle(muted) }
            if !w.temps.isEmpty { Text(w.temps).font(AT.mono(11.5)).foregroundStyle(muted) }
        }
        .lineLimit(1).fixedSize()
        .accessibilityElement(children: .ignore).accessibilityLabel("天气 " + w.text)
    }
}

/// 等你, on the grabber's row: how many of the day's rows wait on him. A tap goes to the first and the number rolls down as they are answered; with none it is gone.
struct TLWaitPill: View {
    let count: Int
    let go: () -> Void
    @State private var last = 0                 // what it said before the last was answered, which it keeps while it fades out

    var body: some View {
        let on = count > 0
        Button(action: go) {
            HStack(spacing: 4) {
                MarkShape(kd: .wait).frame(width: 16, height: 16)
                Text("\(on ? count : last) 件等你").font(.system(size: 12.5, weight: .semibold)).foregroundStyle(amber).monospacedDigit()
                    .contentTransition(.numericText(countsDown: true))
            }
            .padding(.leading, 8).padding(.trailing, 12).frame(height: 26).background(amber.opacity(0.16), in: Capsule())
            .contentShape(Rectangle().inset(by: -9))
        }
        .buttonStyle(PressScale())
        .opacity(on ? 1 : 0).scaleEffect(on ? 1 : 0.92).allowsHitTesting(on)
        .animation(.easeOut(duration: 0.25), value: count)
        .onChange(of: count, initial: true) { _, c in if c > 0 { last = c } }
        .accessibilityLabel("\(on ? count : last) 件事等你")
    }
}

/// what Health reported for the day, after the chips: `8,240 步`, `静息 58`, `HRV 42`. Quiet, no background, nothing to press.
struct TLBodyValues: View {
    let values: [TLBody]

    var body: some View {
        HStack(spacing: 14) {
            ForEach(values) { v in
                HStack(spacing: 0) {
                    Text(v.prefix).font(.system(size: 11.5)).foregroundStyle(faint)
                    Text(v.value).font(AT.mono(11.5)).foregroundStyle(muted)
                    Text(v.suffix).font(.system(size: 11.5)).foregroundStyle(faint)
                }
                .accessibilityElement(children: .combine)
            }
        }
        .lineLimit(1).fixedSize()
    }
}
