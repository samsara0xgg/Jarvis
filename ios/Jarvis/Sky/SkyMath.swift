import CoreGraphics
import Foundation
import QuartzCore
import UIKit

// Shared maths and palette for the star chart. Names follow the HTML prototype so the two can be compared line by line.

let TAU = Double.pi * 2

@inline(__always) func clamp(_ v: Double, _ a: Double, _ b: Double) -> Double { v < a ? a : v > b ? b : v }
@inline(__always) func smooth(_ a: Double, _ b: Double, _ x: Double) -> Double {
    let t = clamp((x - a) / (b - a), 0, 1)
    return t * t * (3 - 2 * t)
}
@inline(__always) func lerp(_ a: Double, _ b: Double, _ k: Double) -> Double { a + (b - a) * k }
@inline(__always) func wrapAngle(_ a: Double) -> Double { atan2(sin(a), cos(a)) }
func nowMs() -> Double { CACurrentMediaTime() * 1000 }
func two(_ n: Int) -> String { n < 10 ? "0\(n)" : "\(n)" }

struct Spring {
    var value: Double
    var velocity: Double = 0
    init(_ v: Double) { value = v }
}

/// A critically damped spring, solved exactly for this step.
func crit(_ s: inout Spring, _ target: Double, _ w: Double, _ dt: Double) {
    let a = s.value - target, b = s.velocity + w * a, e = exp(-w * dt)
    s.value = target + (a + b * dt) * e
    s.velocity = (b - w * (a + b * dt)) * e
}

/// The prototype's seeded generator (a Lehmer LCG), so every launch paints the same sky.
struct Rng {
    var seed: Int
    init(_ seed: Int) { self.seed = seed }
    mutating func next() -> Double {
        seed = (seed * 16807) % 2147483647
        return Double(seed - 1) / 2147483646
    }
}

enum Ease {
    static func out(_ k: Double) -> Double { 1 - pow(1 - k, 3) }
    static func inOut(_ k: Double) -> Double { k < 0.5 ? 4 * k * k * k : 1 - pow(-2 * k + 2, 3) / 2 }
    static func back(_ k0: Double) -> Double { let s = 1.8, k = k0 - 1; return 1 + k * k * ((s + 1) * k + s) }
}

struct RGB: Hashable {
    var r: Double, g: Double, b: Double
    init(_ r: Double, _ g: Double, _ b: Double) { self.r = r; self.g = g; self.b = b }
    func cg(_ a: Double = 1) -> CGColor { CGColor(srgbRed: r / 255, green: g / 255, blue: b / 255, alpha: a) }
    func ui(_ a: Double = 1) -> UIColor { UIColor(cgColor: cg(a)) }
    func tint(_ k: Double) -> RGB { RGB((r + (255 - r) * k).rounded(), (g + (255 - g) * k).rounded(), (b + (255 - b) * k).rounded()) }
    static let lit = RGB(255, 240, 220), wait = RGB(255, 201, 143), work = RGB(108, 156, 255), done = RGB(111, 224, 180)
    static let next = RGB(214, 222, 255), ink = RGB(226, 232, 255), dial = RGB(196, 208, 255), warm = RGB(255, 222, 176)
    static let white = RGB(255, 255, 255), trail = RGB(220, 228, 255)
}

// MARK: - the calendar (the prototype's Thursday 10.8 is today until the phone is paired)

let DAY = 1440.0
let NOW0 = 21.0 * 60 + 14
/// where the sky starts turning from: the prototype's 21:14, or the phone's own clock when paired
var NOW1: Double { Cal.cur.real ? Cal.cur.nowT() : NOW0 }
let TRACE0 = -7 * DAY
let WD = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

func clock(_ m0: Double) -> String {
    let m = ((tlInt(m0) % 1440) + 1440) % 1440          // a time that is not a number reads as midnight, never a stop
    return "\(two(m / 60)):\(two(m % 60))"
}
// the calendar is `Cal.cur`: the prototype's Thursday 10.8 unpaired, the phone's own today paired (Brain/Cal.swift)
func dateOf(_ d: Int) -> (Int, Int) { Cal.cur.dateOf(d) }
func dayName(_ d: Int) -> String { WD[Cal.cur.weekday(d)] }
func isWeekend(_ d: Int) -> Bool { Cal.cur.weekday(d) >= 5 }
var M_FIRST: Int { Cal.cur.mFirst }       // the month's first and last day, as day numbers around today
var M_LAST: Int { Cal.cur.mLast }
func monthName(_ m: Int) -> String { ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "十一", "十二"][m - 1] + "月" }
func dayWord(_ d: Int) -> String { d == -1 ? "昨天" : d == 0 ? "今天" : d == 1 ? "明天" : dayName(d) }
func partOfDay(_ m: Double) -> String {
    m < 300 ? "凌晨" : m < 540 ? "早上" : m < 690 ? "上午" : m < 780 ? "中午" : m < 1050 ? "下午" : m < 1140 ? "傍晚" : "晚上"
}
func durText(_ m: Int) -> String {
    m >= 60 ? "\(m / 60) 小时\(m % 60 != 0 ? " \(two(m % 60)) 分" : "")" : "\(m) 分钟"
}
func floorDay(_ t: Double) -> Int { Int((t / DAY).rounded(.down)) }
