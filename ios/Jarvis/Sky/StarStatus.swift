import SwiftUI

// Her side of the talk before she has said a word, and what she did once she has: the typing line, the step she is on, the small line under her answer.
// The state is Brain/TurnStatus.swift's; this file shows it.

private let ink = SetColor.ink
private let muted = SetColor.muted
private let amber = Color(red: 1, green: 217 / 255, blue: 176 / 255)

extension StarEngine {
    /// The line that stands in her place from the moment he sends: a row of hers with no words, which her first words then fill where it is (`addTurn`).
    func showStatus() {
        guard statusTurn == nil else { return }
        turnSeq += 1
        statusTurn = turnSeq
        withAnimation(.easeOut(duration: 0.25)) { ui.liveTurns.append(LiveTurn(id: turnSeq, his: false, text: "", status: StatusLine())) }
    }

    /// The turn ended without her saying anything (stopped, failed, taken back): the line goes.
    func dropStatus() {
        guard let id = statusTurn else { return }
        statusTurn = nil
        withAnimation(.easeOut(duration: 0.25)) { ui.liveTurns.removeAll { $0.id == id } }
    }

    /// every frame: the typing line says what the board says
    func syncStatus() {
        guard let id = statusTurn else { return }
        guard let i = ui.liveTurns.lastIndex(where: { $0.id == id }) else { statusTurn = nil; return }
        if let line = MainActor.assumeIsolated({ StatusBoard.shared.line() }), ui.liveTurns[i].status != line { ui.liveTurns[i].status = line }
    }

    /// the line under an answer opens to its steps, or folds
    func toggleSteps(_ id: String) {
        wake()
        withAnimation(.easeOut(duration: 0.2)) { if !ui.stepsOpen.insert(id).inserted { ui.stepsOpen.remove(id) } }
    }
}

/// Her place while she has not said a word: the quiet dots, or the step she is on with its seconds. It is one line high, as a line of her words is, and only
/// opacity changes, so her words take its place with nothing moving.
struct StatusRow: View {
    let line: StatusLine
    var size = 15.0             // the step's text; the row is as high as a line of her words 3 pt larger (under the dial 18, in the timeline's place for the talk 16)

    var body: some View {
        Text("　").font(.system(size: size + 3)).hidden()
            .frame(maxWidth: .infinity, alignment: .leading)
            .overlay(alignment: .leading) {
                ZStack(alignment: .leading) {
                    if let label = line.label { step(label).id(label).transition(.opacity) } else { TypingDots().transition(.opacity) }
                }
            }
            .animation(.easeOut(duration: 0.2), value: line.label)
            .accessibilityElement(children: .combine)
            .accessibilityLabel(line.label ?? "她在想")
    }

    /// the step's own text and the seconds since its frame came, counted here
    private func step(_ label: String) -> some View {
        TimelineView(.periodic(from: Date(timeIntervalSinceNow: line.since - Double(uptimeNs()) / 1e9), by: 1)) { _ in
            let s = Int(max(0, Double(uptimeNs()) / 1e9 - line.since))
            Text(s >= 1 ? "\(label) · \(s) 秒" : label).font(.system(size: size)).monospacedDigit().foregroundStyle(muted).lineLimit(1)
        }
    }
}

/// three dots in her colour that breathe one after the other
struct TypingDots: View {
    @Environment(\.accessibilityReduceMotion) private var reduce

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 24, paused: reduce)) { ctx in
            let t = ctx.date.timeIntervalSinceReferenceDate
            HStack(spacing: 6) {
                ForEach(0..<3, id: \.self) { i in
                    Circle().fill(amber).frame(width: 6, height: 6)
                        .opacity(reduce ? 0.55 : 0.28 + 0.52 * (0.5 + 0.5 * sin((t / 1.6 - Double(i) * 0.2) * 2 * .pi)))
                }
            }
        }
    }
}

/// Under her answer, small and muted, what she did to get it; a tap lists each step with its time.
struct StepsLine: View {
    let info: StepsInfo
    let open: Bool
    let toggle: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Button(action: toggle) {
                HStack(spacing: 5) {
                    Text(info.line).font(.system(size: 13))
                    Image(systemName: "chevron.right").font(.system(size: 10, weight: .semibold)).rotationEffect(.degrees(open ? 90 : 0))
                }
                .foregroundStyle(muted).frame(minHeight: 28, alignment: .leading).contentShape(Rectangle())
            }
            .buttonStyle(.plain).accessibilityHint(open ? "收起" : "展开每一步")
            if open {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(Array(info.list.enumerated()), id: \.offset) { _, s in
                        HStack(alignment: .firstTextBaseline, spacing: 10) {
                            Text(s.failed ? "\(s.label) · 没成功" : s.label).font(.system(size: 13)).foregroundStyle(ink.opacity(0.7))
                            Spacer(minLength: 0)
                            Text(s.time).font(AT.mono(12)).foregroundStyle(muted)
                        }
                    }
                }
                .padding(.leading, 10).padding(.bottom, 4)
                .transition(.opacity)
            }
        }
    }
}

#if DEBUG
extension StarEngine {
    /// `-scene steps|stepsDots|stepsStep|stepsDone|stepsOpen`: a typed turn with two steps and an answer, fed to the board as the host's own frames (parsed as the phone
    /// parses them). `steps` plays it at its own pace; the others stop at the dots, a step at four seconds, the answer with its line, and that line opened.
    func runStepsScene(_ name: String) {
        setLevel(0)
        beginTalk(); lingerHold = true
        addTurn(his: true, "明天九点去 UVic，坐什么车？顺便查查维多利亚的小蛋糕")
        let board = MainActor.assumeIsolated { StatusBoard.shared }
        MainActor.assumeIsolated { board.begin(utterance: "scene") }
        showStatus()
        func frame(_ json: String, at: Double? = nil) {
            MainActor.assumeIsolated {
                switch HostFrame(Data(json.utf8)) {
                case .step(let f)?: board.feed(.step(f), at: at)
                case .row(let r)?: board.feed(.row(r), at: at)
                default: break
                }
            }
        }
        func step(_ n: Int, _ tool: String, _ label: String, _ state: String, at: Double? = nil) {
            let ended = state == "running" ? "null" : "1791650009000"
            frame(#"{"type":"step","turn_id":"T1","step":\#(n),"tool":"\#(tool)","label":"\#(label)","state":"\#(state)","started_ms":1791650003000,"ended_ms":\#(ended)}"#, at: at)
        }
        let transit = "查公交 · 去 UVic", web = "上网搜 · 维多利亚 小蛋糕"
        func answer() {
            let rows = #"{"rows":[{"seq":9,"source":"jarvis","ts":"2026-10-11T09:00:00","text":"<voice>九点二十有一班 32 路，半小时到。</voice>","steps":[{"label":"查公交 · 去 UVic","ms":5200},{"label":"上网搜 · 维多利亚 小蛋糕","ms":6100}],"steps_ms":11300,"steps_summary":"查了公交、搜了网页"}]}"#
            frame(#"{"type":"row","event_type":"surface.response_emitted","event_uid":"6f1c2d3e4a5b46788899aabbccddeeff","ts_epoch_ms":1791650015000,"payload":{"turn_id":"T1","text":"x","phase":"final"}}"#)
            guard let row = ConversationRow.parse(Data(rows.utf8))?.first else { return }
            startSay(AnswerText.visible(row.text), cps: 400, steps: row.steps)
            if name == "stepsOpen", let id = ui.liveTurns.last?.id { ui.stepsOpen.insert("l\(id)") }
        }
        let later = { (s: Double, f: @escaping () -> Void) in DispatchQueue.main.asyncAfter(deadline: .now() + s) { f() } }
        let now = MainActor.assumeIsolated { board.clock() }
        switch name {
        case "stepsDots": break
        case "stepsStep":
            MainActor.assumeIsolated { board.bind(utterance: "scene", turn: "T1") }
            step(1, "transit", transit, "running", at: now - 4.2)
        case "stepsDone", "stepsOpen":
            MainActor.assumeIsolated { board.bind(utterance: "scene", turn: "T1") }
            step(1, "transit", transit, "running", at: now - 12); step(1, "transit", transit, "done", at: now - 7)
            step(2, "web", web, "running", at: now - 7); step(2, "web", web, "done", at: now - 1)
            later(1.0) { answer() }
        default:
            later(1.0) { MainActor.assumeIsolated { board.bind(utterance: "scene", turn: "T1") } }
            later(3.0) { step(1, "transit", transit, "running") }
            later(8.0) { step(1, "transit", transit, "done"); step(2, "web", web, "running") }
            later(14.0) { step(2, "web", web, "done") }
            later(15.0) { answer() }
        }
    }
}
#endif
