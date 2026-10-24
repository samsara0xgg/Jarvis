import SwiftUI

// What the day's header says besides the bar: after the date the day's weather (the iPad's); after the day's numbers what Health reported.

private let ink = SetColor.ink
private let muted = SetColor.muted
private let faint = SetColor.faint

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

/// After the date in the top row: the day's weather. A tap on it says for a moment when the sun rose and set, in its place. `room` is what the row leaves between the date and her: the
/// weather gives up its word, and then itself, before it reaches her.
struct TLDayHead: View {
    let tl: TimelineState
    let room: Double
    @Environment(\.accessibilityReduceMotion) private var reduce

    /// how much of the weather `room` holds: all of it (2), the temperatures (1), none (0)
    static func weatherFit(_ w: TLWeather, room: Double) -> Int {
        let icon = w.symbol == nil ? 0 : 16.0, temps = w.temps.isEmpty ? 0 : tlWidth(w.temps, size: 11.5, weight: .medium, mono: true)
        let word = w.word.map { tlWidth($0, size: 12.5) + 5 } ?? 0
        let left = room - 6                          // and a little to spare
        return icon + word + temps <= left ? 2 : icon + temps <= left ? 1 : 0
    }

    var body: some View {
        let reply = tl.reply
        let weather = reply.flatMap { DayTimelineModel.weatherOf($0.weather) }
        let sun = reply.flatMap { DayTimelineModel.sun(of: $0) }
        let saying = tl.sunSaid && sun != nil
        let fit = weather.map { Self.weatherFit($0, room: room) } ?? 0
        let swap = TLMotion.swapWords(reduce: reduce)
        ZStack(alignment: .leading) {
            if saying, let sun {
                TLSunLine(sun: sun).frame(maxWidth: room, alignment: .leading).transition(.opacity)
                    .contentShape(Rectangle().inset(by: -10))
                    .onTapGesture { tl.saySun() }
            } else if let weather, fit > 0 {
                TLWeatherLine(w: weather, word: fit == 2).id(weather.text).transition(swap)
                    .contentShape(Rectangle().inset(by: -10))
                    .onTapGesture { if sun != nil { tl.saySun() } }
                    .accessibilityAddTraits(sun != nil ? .isButton : [])
                    .accessibilityHint(sun != nil ? Words.Home.sunHint : "")
            }
        }
        .animation(.easeOut(duration: 0.2), value: saying)
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
        .accessibilityElement(children: .ignore).accessibilityLabel(Words.Home.weather(w.text))
    }
}

/// When the sun rose and set, by its two symbols and their times (`sunrise 07:34  sunset 18:11`), quiet, in what the date leaves of the row: smaller before it is cut
struct TLSunLine: View {
    let sun: TLSun

    private var line: Text {
        let rise = sun.rise.map { Text("\(Image(systemName: "sunrise")) \(clock($0))") }, set = sun.set.map { Text("\(Image(systemName: "sunset")) \(clock($0))") }
        if let rise, let set { return Text("\(rise)   \(set)") }
        return rise ?? set ?? Text(verbatim: "")
    }

    var body: some View {
        line.font(tlFigures(11.5)).foregroundStyle(muted).lineLimit(1).minimumScaleFactor(0.75)
            .accessibilityLabel(Words.Home.sun(rise: sun.rise.map { clock($0) }, set: sun.set.map { clock($0) }))
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
