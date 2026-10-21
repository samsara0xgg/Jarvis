import Foundation

extension Words {
    /// Every fixed string of the footprint page. Durations, distances and clock times arrive already formatted (`tlDur`, `tlDistance`, the timeline's clock);
    /// a `speed` in a sentence carries its unit, the `speed` of `held` is the number alone.
    @MainActor enum Footprint {
        private static func pick(zh: String, en: String) -> String { Words.pick(zh: zh, en: en) }

        // page
        static var name: String { pick(zh: "足迹", en: "Footprint") }
        static var button: String { pick(zh: "我的足迹", en: "My footprint") }
        static var today: String { pick(zh: "今天", en: "Today") }
        static var backToToday: String { pick(zh: "回到今天", en: "Back to today") }
        static var back: String { pick(zh: "返回", en: "Back") }

        // summary of the day; `past` is a day before today. A name stands in a Chinese sentence with no space round it, in an English one with its spaces.
        static func summaryOne(title: String, distance: String, speed: String) -> String {
            pick(zh: "\(title)，\(distance)，最快 \(speed)。", en: "\(title), \(distance), fastest \(speed).")
        }
        static func summaryMany(count: Int, distance: String, past: Bool) -> String {
            pick(zh: "\(past ? "这天" : "今天")出门 \(count) 趟，一共 \(distance)。",
                 en: "\(past ? "That day" : "Today"): \(count) outings, \(distance) in all.")
        }
        static func summaryNone(past: Bool) -> String { pick(zh: past ? "这天没出门。" : "今天没出门。", en: past ? "No outings that day." : "No outings today.") }
        static func nowAt(place: String) -> String { pick(zh: "现在在\(place)。", en: "Now at \(place).") }

        // outing titles
        static func outingTo(_ destination: String) -> String { pick(zh: "去\(destination)", en: "To \(destination)") }
        static var onTheWay: String { pick(zh: "在路上", en: "On the way") }
        static var outing: String { pick(zh: "出门", en: "Outing") }

        // capsules on the map: "{place} {duration}" is composition
        static func nowFor(_ duration: String) -> String { pick(zh: "现在 · \(duration)", en: "Now · \(duration)") }
        static func left(_ time: String) -> String { pick(zh: "\(time) 出发", en: "Left \(time)") }
        static func arrived(_ time: String) -> String { pick(zh: "\(time) 到", en: "Arrived \(time)") }

        // legend and chips
        static var slow: String { pick(zh: "慢", en: "Slow") }
        static var fast: String { pick(zh: "快", en: "Fast") }
        static var distance: String { pick(zh: "距离", en: "Distance") }
        static var fastest: String { pick(zh: "最快", en: "Fastest") }

        // an outing's row
        static func rowSub(distance: String, speed: String, stops: Int) -> String {
            let stopped = stops == 0 ? nil : pick(zh: "停 \(stops) 次", en: stops == 1 ? "1 stop" : "\(stops) stops")
            return ([distance, pick(zh: "最快 \(speed)", en: "fastest \(speed)")] + [stopped].compactMap { $0 }).joined(separator: " · ")
        }
        static func stopFor(_ duration: String) -> String { pick(zh: "停 \(duration)", en: "Stopped \(duration)") }
        static func unrecorded(_ duration: String) -> String { pick(zh: "\(duration)没记到", en: "\(duration) not recorded") }

        // an outing's detail
        static var duration: String { pick(zh: "用时", en: "Time") }
        static var heartRate: String { pick(zh: "心率", en: "Heart rate") }
        static var speedAxis: String { pick(zh: "速度 · km/h", en: "Speed · km/h") }
        static var colorsMatchMap: String { pick(zh: "颜色和地图上的路一样", en: "Colors match the route on the map") }
        static func heartAverage(avg: String, home: String?) -> String {
            pick(zh: "平均 \(avg)" + (home.map { " · 在家 \($0)" } ?? ""), en: "Average \(avg)" + (home.map { " · at home \($0)" } ?? ""))
        }
        static var departed: String { pick(zh: "出发", en: "Departed") }
        static var arrivedAt: String { pick(zh: "到达", en: "Arrived") }

        // while the bar is held
        static func held(speed: String) -> String { pick(zh: "\(speed) km/h", en: "\(speed) km/h") }
        static func held(bpm: Int) -> String { pick(zh: "心率 \(bpm)", en: "Heart rate \(bpm)") }
    }
}
