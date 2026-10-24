// words: legacy
import SwiftUI

// What a row of the day opens to: on the phone under the row itself, on the row's own columns (Sky/DayTimelineRowViews.swift unfolds it), on the iPad on the stage beside the list, and in
// the menu of a long press as the row's preview. A stretch says one thing a line in time order: each run of his attention with a dot of its colour, what was on his phone alongside, what
// happened in it with its icon, and what took him off it; a stretch she is not sure of asks him first, in one line, and 依据 says how she knows. Anything else says what its line does not: a
// place's map and words, the picture, how a stretch of Mac work was spent, a night's stages, the sessions of Claude Code, with what can be done with it as chips under it (only what the
// row offers: `TLEntry.actions`; 问她 is the long press's, on every row with something to put to her). The row's own title and line are not said again.

private let warm = RGB(255, 240, 220)
private let prose = Color(red: 226 / 255, green: 230 / 255, blue: 250 / 255).opacity(0.86)
private let iconColor = Color(red: 214 / 255, green: 220 / 255, blue: 246 / 255).opacity(0.82)
private let ground = Look.sky            // what the list sits on: the ring that cuts a rail round a line's dot
private let beadFill = Color(red: 17 / 255, green: 16 / 255, blue: 22 / 255)

extension TLEntry {
    /// About how tall what it opens to where it stands is, before it is drawn: how far up the list can carry it, near the list's end.
    var unfoldGuess: Double {
        switch kind {
        case .segment: return (ask != nil ? 40 : 0) + 28 * Double(lines.count) + (knows != nil ? 30 : 0) + 12 + (unsure && ask != nil ? 110 : 0)
        case .sleep: return 260
        case .trip: return 560
        case .segment where commute: return 560
        case .stay: return (place?.onMap == true ? 132 : 0) + 20 * Double(more.count) + 60
        default: return 60 + 20 * Double(more.count)
        }
    }
}

struct TLOpenView: View {
    let e: TLEntry
    let tl: TimelineState
    let engine: StarEngine
    var interactive = true              // false in the preview of a long press, which has no buttons: the menu has them
    var inPlace = false                 // under the row, on its columns: what it says comes in a line after another
    var mapWidth = 296.0                // how wide the map of a place is taken: the room it is given
    @State private var session: String? // the session of Claude Code opened to its recap
    @State private var draft = ""
    @FocusState private var typing: Bool
    @State private var born = Date()    // when this was made: its field is not given the keyboard while it is still coming in
    @State private var why = false      // 依据 is open
    @State private var writing = false  // 写一句… has become a field
    @State private var words = ""       // and what he has written in it
    @FocusState private var writingFocus: Bool
    @Environment(\.accessibilityReduceMotion) private var reduce
    @Environment(\.cardCompact) private var compact          // in the iPad's 等你 under the stage, with the brain's cards: one line, its buttons at the right

    var body: some View {
        Group {
            if compact { waitingLine } else if e.kind == .segment && !e.commute { segment } else { other }          // a stretch on the way opens to its trip
        }
        .padding(.bottom, inPlace ? 12 : 0)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// on the row's columns, the words begin where the row's do and keep clear of its arrow
    private func column<V: View>(_ v: V) -> some View {
        v.padding(.leading, inPlace ? TLColumns.text : 0).padding(.trailing, inPlace ? TLColumns.arrow : 0)
    }

    // MARK: a stretch of what he was doing

    private enum Part: Identifiable {
        case ask, said(String), line(TLLine), links, why(String), note(String), guess, heading(String)
        var id: String {
            switch self {
            case .ask: "ask"
            case .said: "said"
            case .line(let l): "line-" + l.id
            case .links: "links"
            case .why: "why"
            case .note: "note"
            case .guess: "guess"
            case .heading(let h): "h-" + h
            }
        }
    }

    /// what a stretch says, in order: what it asks (or his answer, once he has given it), its lines, 依据 and what it opens to, and why what he did did not go
    private var parts: [Part] {
        var out: [Part] = []
        // a stretch she is not sure of (跨端UI标准 §11): her guess and why first, what she saw, then 对不对？ with the ways to say it
        if e.unsure, e.ask != nil, tl.chosen[e.id] == nil, interactive {
            out.append(.guess)
            if !e.lines.isEmpty { out.append(.heading(Words.PhoneRow.sawWhat)) }
            out += e.lines.map { Part.line($0) }
            out += [.heading(Words.PhoneRow.rightOrNot), .ask]
            if let n = tl.notes[e.id] { out.append(.note(n)) }
            return out
        }
        if let said = tl.chosen[e.id] { out.append(.said(said)) } else if e.ask != nil, interactive { out.append(.ask) }
        out += e.lines.map { Part.line($0) }
        if let k = e.knows, interactive { out.append(.links); if why { out.append(.why(k)) } }
        if interactive, let n = tl.notes[e.id] { out.append(.note(n)) }
        return out
    }

    private var segment: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(parts.enumerated()), id: \.element.id) { k, p in
                part(p).modifier(TLRiseIn(rank: k, on: inPlace && interactive, reduce: reduce))
            }
        }
    }

    @ViewBuilder private func part(_ p: Part) -> some View {
        switch p {
        case .ask: column(askLine)
        case .said(let s):
            column(HStack(spacing: 6) {
                Image(systemName: "checkmark").font(.system(size: 11, weight: .semibold)).foregroundStyle(RGB(76, 195, 138).color)
                Text(Words.Timeline.noted(s)).font(.system(size: 13)).foregroundStyle(Look.ink).lineLimit(1)
            }
            .frame(height: 30))
        case .line(let l): lineRow(l)
        case .links:
            column(Button { withAnimation(TLMotion.unfold(open: !why, reduce: reduce)) { why.toggle() } } label: {
                Text(Words.Timeline.why).font(.system(size: 12, weight: .medium)).foregroundStyle(why ? Look.ink : Look.ink2)
                    .frame(minWidth: 44, minHeight: 30, alignment: .leading).contentShape(Rectangle())
            }
            .buttonStyle(.plain))
        case .why(let k):
            column(Text(k).font(.system(size: 12)).lineSpacing(3).foregroundStyle(Look.ink2).fixedSize(horizontal: false, vertical: true).padding(.bottom, 6)).transition(.opacity)
        case .guess: column(guessCard)
        case .heading(let h): column(StageHeading(text: h))
        case .note(let n):
            column(Text(n).font(.system(size: 12.5)).foregroundStyle(SetColor.red.opacity(0.92)).fixedSize(horizontal: false, vertical: true).padding(.vertical, 4)).transition(.opacity)
        }
    }

    /// What she asks of it, in one line that wraps only when it must: the brain's question and its answers as chips, else her words for it and 是, or for a stretch nothing was recorded for 补一句;
    /// writing a line of his own last, which becomes a field. While his answer is on its way the chips wait.
    @ViewBuilder private var askLine: some View {
        if let a = e.ask {
            let busy = tl.choosing == e.id, guess = e.unsure
            TLFlow(spacing: 6) {
                if guess {          // 对的 / 不对，跟她说, then her other ways of saying it, a tap each
                    chip(Words.PhoneRow.right) { tl.choose(e, choice: nil, text: a.yes ?? e.title) }
                    if writing { field } else { chip(Words.PhoneRow.notRight, quiet: true) { writing = true; DispatchQueue.main.async { writingFocus = true } } }
                    ForEach(a.options, id: \.self) { o in chip(o) { tl.choose(e, choice: o, text: nil) } }
                } else {
                    Text(a.text ?? (a.hole ? Words.Timeline.fillIn : Words.Timeline.isIt(a.yes ?? e.title))).font(.system(size: 13.5)).foregroundStyle(Look.ink)
                        .fixedSize(horizontal: false, vertical: true).frame(minHeight: 26).padding(.trailing, 2)
                    ForEach(a.options, id: \.self) { o in chip(o) { tl.choose(e, choice: o, text: nil) } }
                    if let y = a.yes { chip(Words.Timeline.yes) { tl.choose(e, choice: nil, text: y) } }
                    if writing { field } else { chip(Words.Timeline.writeLine, quiet: true) { writing = true; DispatchQueue.main.async { writingFocus = true } } }
                }
            }
            .opacity(busy ? 0.5 : 1).allowsHitTesting(!busy)
            .animation(.easeOut(duration: 0.15), value: busy)
            .frame(minHeight: 36, alignment: .leading).padding(.bottom, 4)
        }
    }

    /// her guess, on a small card: what she thinks it was and why (依据), else the guess alone
    private var guessCard: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(Words.PhoneRow.unsure).font(.system(size: Look.small, weight: .medium)).foregroundStyle(Look.waits)
            Text(e.knows ?? e.note ?? Words.PhoneRow.guess(e.title)).font(.system(size: 13.5)).foregroundStyle(Look.ink).lineSpacing(3).fixedSize(horizontal: false, vertical: true)
        }
        .padding(12).frame(maxWidth: .infinity, alignment: .leading)
        .background(Look.card, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(Look.waits.opacity(0.25), lineWidth: 0.6))
        .padding(.bottom, 4)
    }

    private func chip(_ text: String, quiet: Bool = false, _ action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(text).font(.system(size: 12.5, weight: .medium)).foregroundStyle(quiet ? Look.ink2 : Look.ink).lineLimit(1).fixedSize()
                .padding(.horizontal, 11).frame(height: 26)
                .background(Color.white.opacity(0.07), in: Capsule())
                .overlay(Capsule().strokeBorder(Look.ink.opacity(0.14), lineWidth: 1))
                .contentShape(Capsule().inset(by: -8))
        }
        .buttonStyle(PressScale())
    }

    /// his own line for the stretch, in the place of 写一句…: done sends it
    private var field: some View {
        TextField("", text: $words, prompt: Text(Words.Timeline.writeLine).foregroundStyle(Look.ink3))
            .focused($writingFocus).submitLabel(.done)
            .onSubmit { if let t = TLSayWhat.words(words) { tl.choose(e, choice: nil, text: t) } }
            .onChange(of: words) { _, now in let kept = TLSayWhat.capped(now); if kept != now { words = kept } }
            .font(.system(size: 13.5)).foregroundStyle(Look.ink).tint(Look.ink)
            .padding(.horizontal, 11).frame(width: 190, height: 30)
            .background(warm.color(0.14), in: Capsule())
            .overlay(Capsule().strokeBorder(warm.color(0.35), lineWidth: 1))
    }

    /// One thing inside a stretch, on the row's columns: its time, a dot of its colour for a run of his attention (an empty ring for what took him off it) or its icon in a small circle for
    /// anything else, its words with what it was made of after them, and at the right how long or what it came to. A tap frames its time on the bar for a moment, and the line lights while
    /// it is; a picture's opens the picture.
    private func lineRow(_ l: TLLine) -> some View {
        let lit = tl.litLine == l.id, quiet = l.mark == .drift
        let title = Text(said(l)).font(.system(size: quiet ? 12.5 : 13.5)).foregroundStyle(quiet ? Look.ink2 : Look.ink)
        let detail: String? = if case .session(let cloud, let prompts) = l.said { lit ? Words.Timeline.sessionDetail(cloud: cloud, prompts: prompts) : nil } else { l.detail }          // a session says where it ran and what he typed when tapped
        let rest = detail.map { Text(" · " + $0).font(.system(size: 12.5)).foregroundStyle(Look.ink2) } ?? Text("")
        return HStack(spacing: 0) {
            Text(l.time ?? "").font(Look.figures(11)).foregroundStyle(Look.ink2).lineLimit(1).frame(width: 46, alignment: .leading)
            mark(l.mark).frame(width: 28, height: 28)
            (title + rest).lineLimit(1).padding(.leading, 10).frame(maxWidth: .infinity, alignment: .leading)
            if !l.right.isEmpty {
                Text(l.right).font(tlFigures(11.5)).foregroundStyle(l.strong ? Look.ink : Look.ink2).lineLimit(1).fixedSize().padding(.leading, 10).padding(.trailing, inPlace ? TLColumns.arrow : 0)
            }
        }
        .frame(height: 28)
        .background(RoundedRectangle(cornerRadius: 8).fill(warm.color(lit ? 0.09 : 0)).padding(.horizontal, -8))
        .contentShape(Rectangle())
        .onTapGesture {
            guard interactive else { return }
            if let a = l.attachment {
                PictureViewer.present([ViewerPicture(id: a, gone: false, small: PictureStore.shared.peek(id: a, maxPixel: 0))], index: 0)
            } else if l.span != nil {
                Haptic.selection()
                tl.lightLine(l)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(interactive && (l.attachment != nil || l.span != nil) ? .isButton : [])
    }

    /// the words of a line: its title, or the timeline's own words for what it says
    private func said(_ l: TLLine) -> String {
        switch l.said {
        case .drift(let m, let s): Words.Timeline.drift(minutes: m, switches: s)
        case .onDevice(let device, let name): Words.Timeline.onDevice(device, name)
        case .more(let n): Words.Timeline.moreRuns(n)
        case .background(let n): Words.Timeline.backgroundSessions(n)
        case .session(let cloud, _): l.title.isEmpty ? (cloud ? Words.Timeline.cloudThread : Words.Timeline.untitledSession) : l.title
        case .received(let app, let people, let count): Words.Timeline.received(app, people: people, count: count)
        case nil: l.title
        }
    }

    @ViewBuilder private func mark(_ m: TLLine.Mark) -> some View {
        switch m {
        case .dot(let c):
            Circle().fill((c ?? RGB(196, 204, 238)).color(c == nil ? 0.45 : 1)).frame(width: 8, height: 8)
                .background(Circle().fill(ground).frame(width: 14, height: 14))
        case .icon(let symbol):
            Image(systemName: symbol).font(.system(size: 10.5)).foregroundStyle(iconColor)
                .frame(width: 20, height: 20).background(Circle().fill(beadFill))
                .background(Circle().fill(ground).frame(width: 26, height: 26))
        case .drift:
            Circle().strokeBorder(RGB(196, 204, 238).color(0.55), lineWidth: 1.5).background(Circle().fill(ground)).frame(width: 8, height: 8)
                .background(Circle().fill(ground).frame(width: 14, height: 14))
        case .claude(let cloud):
            ClaudeMark().stroke(iconColor, style: StrokeStyle(lineWidth: 1.2, lineCap: .round)).frame(width: 10.5, height: 10.5)
                .frame(width: 20, height: 20).background(Circle().fill(beadFill))
                .overlay(alignment: .bottomTrailing) {
                    if cloud { Image(systemName: "cloud.fill").font(.system(size: 6.5)).foregroundStyle(iconColor).padding(1.5).background(Circle().fill(ground)).offset(x: 3, y: 2) }
                }
                .background(Circle().fill(ground).frame(width: 26, height: 26))
        }
    }

    // MARK: anything else

    /// a trip and a night take the row's whole width (their map, curves and bars); the rest begins where the row's words do
    private var wide: Bool { e.kind == .trip || e.kind == .sleep || e.commute }
    /// something not yet happened that came from somewhere: an event or a reminder still to come
    private var upcoming: Bool { e.later && (e.kind == .event || e.kind == .reminder) }

    private var other: some View {
        VStack(alignment: .leading, spacing: 12) {
            if interactive, tl.renaming == e.id { renameField }         // first, so that it is up under the place's name whatever else the place has to show, clear of the keyboard
            content
            if interactive, tl.renaming != e.id { buttons }
            if interactive, let note = tl.notes[e.id] { Text(note).font(.system(size: 12.5)).foregroundStyle(SetColor.red.opacity(0.92)).fixedSize(horizontal: false, vertical: true).transition(.opacity) }
        }
        .padding(.horizontal, inPlace && wide ? 10 : 0).padding(.vertical, inPlace && wide ? 12 : 0)          // 10 + the curves' 380 + 10: the row's 400
        .background { if inPlace && wide { RoundedRectangle(cornerRadius: 16, style: .continuous).fill(Look.card) } }          // over the list's rail, which would run through its map and curves
        .padding(.top, inPlace ? 2 : 0)
        .padding(.leading, inPlace && !wide ? TLColumns.text : 0).padding(.trailing, inPlace && !wide ? TLColumns.arrow : 0)
        .modifier(TLRiseIn(rank: 0, on: inPlace && interactive, reduce: reduce))
    }

    @ViewBuilder private var content: some View {
        if upcoming { upcomingParts } else {
        switch e.kind {
        case .stay: stay
        case .picture: picture
        case .work: work
        case .trip: TripStage(e: e, tl: tl, engine: engine)
        case .sleep: NightStage(e: e, health: tl.reply.map { tl.model($0).body } ?? [], tl: tl, engine: engine)
        case .code: code
        case .segment where e.commute: TripStage(e: e, tl: tl, engine: engine)
        case .now, .pause, .segment: EmptyView()
        default: facts(e.more)
        }
        }
    }

    // MARK: something not yet happened

    /// 从哪来的: what the row came from, as the brain says it (the mail, the calendar, the reminder); 怎么去: the time to leave for it, where the day has one in the two hours before it
    private var upcomingParts: some View {
        let leave = tl.reply.flatMap { r in tl.model(r).entries.last { $0.kind == .departure && $0.t0 <= e.t0 && $0.t0 >= e.t0 - 120 } }
        return VStack(alignment: .leading, spacing: 10) {
            if e.detail != nil || !e.more.isEmpty {
                StageHeading(text: Words.PhoneRow.fromWhere)
                VStack(alignment: .leading, spacing: 4) {
                    if let d = e.detail { Text(d).font(.system(size: Look.small)).foregroundStyle(Look.ink3) }
                    facts(e.more)
                }
                .padding(12).frame(maxWidth: .infinity, alignment: .leading)
                .background(Look.card, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
            }
            if let l = leave {
                StageHeading(text: Words.PhoneRow.howToGo)
                HStack(spacing: 10) {
                    Text(l.time).font(Look.figures(11.5)).foregroundStyle(Look.ink2).frame(width: 40, alignment: .leading)
                    Image(systemName: l.symbol.isEmpty ? "figure.walk" : l.symbol).font(.system(size: 12)).foregroundStyle(Look.ink2).frame(width: 16)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(l.title).font(.system(size: 13.5)).foregroundStyle(Look.ink)
                        if let d = l.detail { Text(d).font(.system(size: 11.5)).foregroundStyle(Look.ink2) }
                    }
                }
            }
        }
    }

    /// what it says in full, a line each
    private func facts(_ lines: [String]) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(Array(lines.enumerated()), id: \.offset) { _, l in
                Text(l).font(.system(size: 13.5)).foregroundStyle(prose).fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    // MARK: a place

    /// where he was: the map (120 high, radius 10; a tap opens the whole map) where the phone has the coordinates, the words of the place and the name he keeps it under
    private var stay: some View {
        VStack(alignment: .leading, spacing: 10) {
            if let p = e.place, let lat = p.lat, let lng = p.lng {
                PlaceMapCard(places: [AnswerPlace(name: e.title, lat: lat, lng: lng, address: p.text, placeId: nil)], height: 120, radius: 10, named: false, width: mapWidth)
            }
            facts(e.more)
        }
    }

    // MARK: a picture

    private var picture: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let id = e.attachment { TLPicture(attachment: id) }
            if !e.more.isEmpty { facts(e.more) }
        }
    }

    // MARK: Mac work

    private var work: some View {
        VStack(alignment: .leading, spacing: 12) {
            if let w = e.work {
                if !w.projects.isEmpty { bars("项目", w.projects) }
                if !w.apps.isEmpty { bars("应用", w.apps) }
            }
            facts(e.more)
        }
    }

    /// up to five of what the time went to, the longest the whole width
    private func bars(_ title: String, _ list: [TLTop]) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            Text(title).font(.system(size: 11)).foregroundStyle(Look.ink3)
            ForEach(Array(list.enumerated()), id: \.offset) { _, t in
                VStack(alignment: .leading, spacing: 4) {
                    HStack(spacing: 8) {
                        Text(t.name).font(.system(size: 13)).foregroundStyle(Look.ink.opacity(0.88)).lineLimit(1)
                        Spacer(minLength: 6)
                        Text(tlDur(t.minutes)).font(tlFigures(11.5)).foregroundStyle(Look.ink2).lineLimit(1).fixedSize()
                    }
                    GeometryReader { g in
                        ZStack(alignment: .leading) {
                            Capsule().fill(Color.white.opacity(0.08))
                            Capsule().fill(TLLens.work.color.color(0.85)).frame(width: max(3, g.size.width * t.share))
                        }
                    }
                    .frame(height: 3)
                }
            }
        }
    }

    // MARK: a night

    private var sleep: some View {
        VStack(alignment: .leading, spacing: 10) {
            facts(e.more)
            if let s = e.sleep {
                TLHypnogram(stages: s.stages, t0: e.t0, t1: e.t1 ?? e.t0)
                if let t = s.summary { Text(t).font(.system(size: 12.5)).foregroundStyle(Look.ink2).fixedSize(horizontal: false, vertical: true) }
            }
        }
    }

    // MARK: Claude Code

    /// one row for the sessions of a place: each as a line (time, title and account, how long) that opens to its recap; a session alone opens to its recap
    @ViewBuilder private var code: some View {
        if e.holdsSessions {
            VStack(alignment: .leading, spacing: 0) {
                ForEach(e.sessions) { s in sessionLine(s) }
            }
        } else {
            facts(e.more)
        }
    }

    private func sessionLine(_ s: TLSession) -> some View {
        let shown = session == s.id && s.recap != nil
        return VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .center, spacing: 10) {
                Text(s.time).font(Look.figures(11.5)).foregroundStyle(Look.ink2).frame(width: 40, alignment: .leading)
                VStack(alignment: .leading, spacing: 1) {
                    Text(s.name).font(.system(size: 14)).foregroundStyle(Look.ink).lineLimit(1)
                    if let meta = s.account { Text(meta).font(.system(size: 11.5)).foregroundStyle(Look.ink2).lineLimit(1) }
                }
                Spacer(minLength: 6)
                Text(s.right).font(tlFigures(11.5)).foregroundStyle(Look.ink2).lineLimit(1).fixedSize()
                if s.recap != nil {
                    Image(systemName: "chevron.right").font(.system(size: 10, weight: .semibold)).foregroundStyle(Look.ink3).rotationEffect(.degrees(shown ? 90 : 0))
                }
            }
            .frame(minHeight: 44)
            .contentShape(Rectangle())
            .onTapGesture {
                guard s.recap != nil, interactive else { return }
                Haptic.impact(.light)
                withAnimation(.smooth(duration: 0.26)) { session = shown ? nil : s.id }
            }
            if shown, let r = s.recap {
                Text(r).font(.system(size: 13)).lineSpacing(3).foregroundStyle(prose).fixedSize(horizontal: false, vertical: true).padding(.bottom, 10)
                    .transition(.opacity)
            }
        }
    }

    // MARK: what waits, in one line

    /// its icon, its title and a line under it (when, and what it came from), and what can be done with it at the right; what the brain says to a press under it
    private var waitingLine: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 14) {
                if !e.symbol.isEmpty {
                    Image(systemName: e.symbol).font(.system(size: 14, weight: .medium)).foregroundStyle(e.color.map { $0.color } ?? iconColor)
                        .frame(width: 32, height: 32).background(Circle().fill(Color.white.opacity(0.06)))
                }
                VStack(alignment: .leading, spacing: 2) {
                    Text(e.title).font(.system(size: 16, weight: .medium)).foregroundStyle(Look.ink).lineLimit(1)
                    Text([e.time, e.detail ?? ""].filter { !$0.isEmpty }.joined(separator: " · ")).font(.system(size: 13)).foregroundStyle(Look.ink3).lineLimit(1)
                }
                Spacer(minLength: 10)
                if interactive { buttons.fixedSize() }
            }
            .frame(minHeight: 56)
            if interactive, let note = tl.notes[e.id] { Text(note).font(.system(size: 12.5)).foregroundStyle(SetColor.red.opacity(0.92)).fixedSize(horizontal: false, vertical: true).padding(.leading, 46).transition(.opacity) }
        }
    }

    // MARK: what can be done with it

    /// What can be done with it, as chips: what the row opened is for first, in the light fill. Asking her is the long press's, and 这段在干嘛 is the last line of that menu.
    @ViewBuilder private var buttons: some View {
        let doing = e.actions.filter { ($0 != .ask || upcoming && !compact) && $0 != .sayWhat }.sorted { rank($0) < rank($1) }          // 问她 is the long press's, and a chip of its own on what is still to come (not in one line)
        if !doing.isEmpty {
            TLFlow(spacing: 6) {
                ForEach(doing, id: \.self) { a in
                    Button { tl.perform(a, on: e, engine: engine) } label: {
                        Text(upcoming && a == .confirm ? Words.PhoneRow.going : upcoming && a == .deny ? Words.PhoneRow.notGoing : a.title).font(.system(size: 12.5, weight: .medium)).lineLimit(1).fixedSize()
                            .foregroundStyle(strong(a) ? AT.navy : Look.ink)
                            .padding(.horizontal, 11).frame(height: 26)
                            .background(strong(a) ? AT.pale : Color.white.opacity(0.07), in: Capsule())
                            .overlay(Capsule().strokeBorder(Look.ink.opacity(strong(a) ? 0 : 0.14), lineWidth: 1))
                            .contentShape(Capsule().inset(by: -8))
                    }
                    .buttonStyle(PressScale())
                }
            }
            .animation(.easeOut(duration: reduce ? TLMotion.reducedFade : 0.2), value: e.actions)          // 是我的 and 不是 go when one of them is said
        }
    }
    /// what the row opened is for comes first, and in the light fill
    private func strong(_ a: TLAction) -> Bool { a == .acknowledge || a == .confirm }
    private func rank(_ a: TLAction) -> Int { strong(a) ? 0 : a == .rename || a == .name || a == .deny ? 1 : 2 }

    /// the name of a place he keeps, typed in the row (or the first name of one he keeps none at, with the kind of place to choose under it): done sends it, the keyboard's return too; what
    /// the brain refuses says why under it
    private var renameField: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                TextField("这个地方叫什么", text: $draft)
                    .focused($typing).submitLabel(.done).onSubmit { tl.rename(e, to: draft) }
                    .font(.system(size: 15)).foregroundStyle(Look.ink).tint(Look.ink)
                    .padding(.horizontal, 14).frame(height: 42)
                    .background(Color.white.opacity(0.08), in: Capsule())
                    .overlay(Capsule().stroke(Look.ink.opacity(typing ? 0.45 : 0.12), lineWidth: 1))
                Button { tl.rename(e, to: draft) } label: {
                    Text("完成").font(.system(size: 14, weight: .semibold)).foregroundStyle(AT.navy).padding(.horizontal, 16).frame(height: 42)
                        .background(AT.pale.opacity(tl.renameBusy ? 0.5 : 1), in: Capsule()).contentShape(Capsule())
                }
                .buttonStyle(PressScale()).disabled(tl.renameBusy)
            }
            if e.place?.canName == true { roleChips }
            HStack(spacing: 12) {
                Button { tl.cancelRename() } label: { Text("取消").font(.system(size: 13)).foregroundStyle(Look.ink2).frame(minWidth: 44, minHeight: 32, alignment: .leading).contentShape(Rectangle()) }
                    .buttonStyle(.plain)
                if let n = tl.renameNote { Text(n).font(.system(size: 12.5)).foregroundStyle(SetColor.red.opacity(0.92)).fixedSize(horizontal: false, vertical: true) }
            }
        }
        .onAppear { begin() }
    }

    /// 家, 学校 or 其他: the one chosen is in the light fill
    private var roleChips: some View {
        HStack(spacing: 8) {
            ForEach(TLRole.allCases, id: \.self) { r in
                let on = tl.nameRole == r
                Button { tl.nameRole = r; Haptic.selection() } label: {
                    Text(r.word).font(.system(size: 13.5, weight: .semibold)).lineLimit(1).fixedSize()
                        .foregroundStyle(on ? AT.navy : Look.ink)
                        .padding(.horizontal, 15).frame(height: 32)
                        .background(on ? AT.pale : Color.white.opacity(0.1), in: Capsule())
                        .contentShape(Capsule().inset(by: -6))
                }
                .buttonStyle(PressScale())
                .accessibilityAddTraits(on ? .isSelected : [])
            }
        }
        .animation(.easeOut(duration: 0.15), value: tl.nameRole)
    }

    /// a place he keeps starts as the name it has; one he keeps none at starts as what the phone called it, if it did
    private func begin() {
        draft = e.place?.canName == true ? e.barName ?? "" : e.place?.keptName ?? e.title
        DispatchQueue.main.asyncAfter(deadline: .now() + max(0, 0.35 - Date().timeIntervalSince(born))) { typing = true }
    }
}

/// What an open row says comes in a line after another: from 6 pt below, each a little after the one before (`TLMotion.line`); under Reduce Motion a fade. Elsewhere (the stage, the preview of a
/// long press) it is simply there.
private struct TLRiseIn: ViewModifier {
    let rank: Int
    let on: Bool
    let reduce: Bool
    @State private var shown = false

    func body(content: Content) -> some View {
        content
            .opacity(!on || shown ? 1 : 0)
            .offset(y: !on || shown || reduce ? 0 : TLMotion.lineRise)
            .onAppear { if on && !shown { withAnimation(TLMotion.line(rank: rank, reduce: reduce)) { shown = true } } }
    }
}

/// Views in a row that goes on to the next line when it is full, each given the whole width at most (a question longer than the row wraps in its own lines).
struct TLFlow: Layout {
    var spacing = 6.0

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let w = proposal.width ?? .infinity
        var x = 0.0, y = 0.0, rowH = 0.0, widest = 0.0
        for s in subviews {
            let d = s.sizeThatFits(ProposedViewSize(width: w, height: nil))
            if x > 0 && x + d.width > w { x = 0; y += rowH + spacing; rowH = 0 }
            x += d.width + spacing; rowH = max(rowH, d.height); widest = max(widest, x - spacing)
        }
        return CGSize(width: min(widest, w), height: y + rowH)
    }

    func placeSubviews(in b: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = b.minX, y = b.minY, rowH = 0.0
        var row: [(LayoutSubview, CGSize, Double)] = []
        func flush() {
            for (s, d, at) in row { s.place(at: CGPoint(x: at, y: y + (rowH - d.height) / 2), proposal: ProposedViewSize(d)) }          // a row's views on its middle
            row = []
        }
        for s in subviews {
            let d = s.sizeThatFits(ProposedViewSize(width: b.width, height: nil))
            if x > b.minX && x + d.width > b.maxX { flush(); x = b.minX; y += rowH + spacing; rowH = 0 }
            row.append((s, d, x)); x += d.width + spacing; rowH = max(rowH, d.height)
        }
        flush()
    }
}

// MARK: - the picture

/// A picture he sent: the picture as the talk shows it (up to 240 high; a tap opens it full screen). One the brain has swept is a plain line.
struct TLPicture: View {
    let attachment: String
    var tall = 240.0                // the picture opened is up to 240 high, one among a stretch's lines less
    @State private var swept = false

    var body: some View {
        Group {
            if swept {
                Text("图片已过期").font(.system(size: 13)).foregroundStyle(Look.ink2)
            } else {
                PictureCell(item: SentItem(id: attachment, attachment: attachment, isImage: true, title: ""), size: nil, tall: tall) {
                    PictureViewer.present([ViewerPicture(id: attachment, gone: false, small: PictureStore.shared.peek(id: attachment, maxPixel: 0))], index: 0)
                }
            }
        }
        .task(id: attachment) {
            if case .gone = await PictureStore.shared.source(id: attachment, gone: false) { swept = true }
        }
    }
}

// MARK: - the night

/// The night's stages as a hypnogram: four lanes (清醒, REM, 核心, 深睡) 12 high, a block for each stage on the row's own time scale, and the hours it began and ended under it.
struct TLHypnogram: View {
    let stages: [TLStage]
    let t0: Double
    let t1: Double

    private static let lanes = ["清醒", "REM", "核心", "深睡"], laneH = 12.0, labelW = 30.0

    static func lane(_ k: TLStage.Kind) -> Int {
        switch k {
        case .awake, .inBed: 0
        case .rem: 1
        case .core, .unspecified: 2
        case .deep: 3
        }
    }
    /// a block's colour on the hypnogram, where its lane says which stage it is: the bar's looks, a little stronger so that a light stage is seen
    static func paint(_ k: TLStage.Kind) -> (rgb: RGB, alpha: Double) {
        switch k {
        case .deep: (TLPaint.sleep, 1)
        case .core: (TLPaint.sleep, 0.8)
        case .rem: (TLPaint.rem, 0.9)
        case .awake: (TLPaint.ink, 0.6)
        case .inBed: (TLPaint.ink, 0.3)
        case .unspecified: (TLPaint.sleep, 0.6)
        }
    }

    var body: some View {
        let blocks = TLStage.blocks(stages, in: t0, t1)
        VStack(alignment: .leading, spacing: 4) {
            ZStack(alignment: .topLeading) {
                ForEach(Array(Self.lanes.enumerated()), id: \.offset) { i, name in
                    Text(name).font(.system(size: 9.5)).foregroundStyle(Look.ink3).frame(width: Self.labelW, height: Self.laneH, alignment: .leading).offset(y: Double(i) * Self.laneH)
                }
                Canvas { ctx, size in
                    let w = size.width - Self.labelW, span = max(t1 - t0, 1)
                    func x(_ t: Double) -> Double { Self.labelW + (t - t0) / span * w }
                    var last: (lane: Int, x: Double)?
                    for b in blocks {
                        let lane = Self.lane(b.kind), x0 = x(b.t0), x1 = x(b.t1), y = Double(lane) * Self.laneH
                        let p = Self.paint(b.kind)
                        ctx.fill(Path(roundedRect: CGRect(x: x0, y: y + 1.5, width: max(x1 - x0 - 0.5, 1.5), height: Self.laneH - 3), cornerRadius: 2), with: .color(p.rgb.color(p.alpha)))
                        if let l = last, l.lane != lane {
                            var join = Path()
                            join.move(to: CGPoint(x: x0, y: Double(l.lane) * Self.laneH + Self.laneH / 2)); join.addLine(to: CGPoint(x: x0, y: y + Self.laneH / 2))
                            ctx.stroke(join, with: .color(Color.white.opacity(0.16)), lineWidth: 1)
                        }
                        last = (lane, x1)
                    }
                }
            }
            .frame(height: Self.laneH * 4)
            HStack {
                Text(clock(t0)).font(Look.figures(10)).foregroundStyle(Look.ink3)
                Spacer()
                Text(tlEnd(t1)).font(Look.figures(10)).foregroundStyle(Look.ink3)
            }
            .padding(.leading, Self.labelW)
        }
    }
}

// MARK: - what he did

/// what the brain said to a rename he can take back: a quiet capsule for a few seconds over the foot of the list
struct TLUndoToast: View {
    let undo: PlaceUndo
    let take: () -> Void

    var body: some View {
        HStack(spacing: 0) {
            if let note = undo.note {
                Text(note).font(.system(size: 13.5)).foregroundStyle(Look.ink.opacity(0.9)).lineLimit(1).padding(.horizontal, 16)
            } else {
                Text("改好了").font(.system(size: 13.5)).foregroundStyle(Look.ink.opacity(0.9)).padding(.leading, 16)
                Text(" · ").font(.system(size: 13.5)).foregroundStyle(Look.ink2)
                Button(action: take) {
                    Text("撤销").font(.system(size: 13.5, weight: .semibold)).foregroundStyle(Look.ink).padding(.trailing, 16).frame(minWidth: 44, minHeight: 44).contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
        .frame(height: 40)
        .background(Color(red: 28 / 255, green: 31 / 255, blue: 58 / 255).opacity(0.96), in: Capsule())
        .overlay(Capsule().stroke(Color.white.opacity(0.14), lineWidth: 0.5))
        .shadow(color: .black.opacity(0.4), radius: 12, y: 4)
    }
}

/// Claude's mark as the timeline draws it: a thin starburst of eight rays round an open centre, stroked like the other line icons
struct ClaudeMark: Shape {
    func path(in r: CGRect) -> Path {
        var p = Path()
        let c = CGPoint(x: r.midX, y: r.midY), outer = min(r.width, r.height) / 2, inner = outer * 0.3
        for k in 0..<8 {
            let a = Double(k) * .pi / 4 + .pi / 8
            p.move(to: CGPoint(x: c.x + inner * cos(a), y: c.y + inner * sin(a)))
            p.addLine(to: CGPoint(x: c.x + outer * cos(a), y: c.y + outer * sin(a)))
        }
        return p
    }
}

/// A row's icon by its symbol's name: an SF Symbol, or `TLSymbol.claude` for the Claude mark, drawn at the symbol's size and weight
struct TLSymbol: View {
    static let claude = "claude.mark"
    let name: String
    var size: Double

    var body: some View {
        if name == Self.claude {
            ClaudeMark().stroke(style: StrokeStyle(lineWidth: size / 9, lineCap: .round)).frame(width: size * 0.9, height: size * 0.9)
        } else {
            Image(systemName: name).font(.system(size: size))
        }
    }
}
