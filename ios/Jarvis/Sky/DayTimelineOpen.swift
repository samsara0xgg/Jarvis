import SwiftUI

// What a row of the day opens to, in place under it: a place's map and words, what a trip, a ride, an event or a payment says that its line does not, the first lines of a talk, the picture,
// how a stretch of Mac work was spent, a night's stages, the sessions of Claude Code, and the buttons of what can be done with it (only those the row offers: `TLEntry.actions`) with the
// star at their right and, for a stretch, 这段在干嘛 under them. The row's own title and line are not said again: they wrap, in the row, while it is open.

private let ink = SetColor.ink
private let muted = SetColor.muted
private let faint = SetColor.faint
private let prose = Color(red: 226 / 255, green: 230 / 255, blue: 250 / 255).opacity(0.86)
private let iconColor = Color(red: 214 / 255, green: 220 / 255, blue: 246 / 255).opacity(0.82)

struct TLOpenView: View {
    let e: TLEntry
    let tl: TimelineState
    let engine: StarEngine
    var interactive = true              // false in the preview of a long press, which has no buttons: the menu has them
    @State private var session: String? // the session of Claude Code opened to its recap
    @State private var draft = ""
    @FocusState private var typing: Bool
    @Environment(\.accessibilityReduceMotion) private var reduce

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if interactive, tl.renaming == e.id { renameField }         // first, so that it is up under the place's name whatever else the place has to show, clear of the keyboard
            content
            if interactive, tl.renaming != e.id { buttons }
            if interactive, tl.renaming != e.id, e.actions.contains(.sayWhat) { sayWhatLink }
            if interactive, let note = tl.notes[e.id] { Text(note).font(.system(size: 12.5)).foregroundStyle(SetColor.red.opacity(0.92)).fixedSize(horizontal: false, vertical: true).transition(.opacity) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder private var content: some View {
        switch e.kind {
        case .stay: stay
        case .talk: talk
        case .picture: picture
        case .work: work
        case .sleep: sleep
        case .code: code
        case .segment: segment
        case .now, .pause: EmptyView()
        default: facts(e.more)
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
                PlaceMapCard(places: [AnswerPlace(name: e.title, lat: lat, lng: lng, address: p.text, placeId: nil)], height: 120, radius: 10, named: false, width: 296)
            }
            facts(e.more)
        }
    }

    // MARK: a stretch of what he was doing

    /// the model's line about it, what was inside it a line each in time order, and how she knows what he was doing and what she went by (two lines at most)
    private var segment: some View {
        VStack(alignment: .leading, spacing: 12) {
            if let n = e.note { Text(n).font(.system(size: 13.5)).foregroundStyle(prose).fixedSize(horizontal: false, vertical: true) }
            if !e.lines.isEmpty {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(e.lines) { insideLine($0) }
                }
            }
            if let k = e.knows { Text(k).font(.system(size: 12)).foregroundStyle(muted).lineLimit(2).fixedSize(horizontal: false, vertical: true) }
        }
    }

    /// one thing inside a stretch: its icon, the words its own row has, and at the right how long of it was inside, what it came to, or the time; a picture has its thumbnail under it
    private func insideLine(_ l: TLLine) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 10) {
                Image(systemName: l.symbol).font(.system(size: 12)).foregroundStyle(iconColor).frame(width: 20)
                VStack(alignment: .leading, spacing: 1) {
                    Text(l.title).font(.system(size: 13.5)).foregroundStyle(ink).lineLimit(1)
                    if let d = l.detail { Text(d).font(.system(size: 11.5)).foregroundStyle(muted).lineLimit(1) }
                }
                Spacer(minLength: 6)
                Text(l.right).font(AT.mono(11.5)).foregroundStyle(l.strong ? ink : muted).lineLimit(1).fixedSize()
            }
            .frame(minHeight: 36)
            if let a = l.attachment { TLPicture(attachment: a, tall: 150).padding(.bottom, 4) }
        }
    }

    // MARK: a talk

    /// his first words (unless they are what the row is called) and her first reply, when the phone holds them, and how many lines it had; else it says that it has not read them
    private var talk: some View {
        let x = TalkTopics.shared.exchange(e.id)
        let his = x.flatMap { $0.his == e.title ? nil : $0.his }, hers = x.flatMap { $0.hers.isEmpty ? nil : $0.hers }
        return VStack(alignment: .leading, spacing: 8) {
            if his != nil || hers != nil {
                HStack(alignment: .top, spacing: 10) {
                    Rectangle().fill(ink.opacity(0.18)).frame(width: 2)
                    VStack(alignment: .leading, spacing: 6) {
                        if let his { Text(his).font(.system(size: 14)).foregroundStyle(ink.opacity(0.9)).lineLimit(2) }
                        if let hers { Text(hers).font(.system(size: 14)).foregroundStyle(muted).lineLimit(2) }
                    }
                }
                .fixedSize(horizontal: false, vertical: true)
            } else if x == nil {
                Text("这次对话的字还没读到，打开对话可以看全部").font(.system(size: 13)).foregroundStyle(muted).fixedSize(horizontal: false, vertical: true)
            }
            if let n = e.turns { Text("共 \(n) 句").font(AT.mono(11.5)).foregroundStyle(muted) }
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
            Text(title).font(.system(size: 11)).foregroundStyle(faint)
            ForEach(Array(list.enumerated()), id: \.offset) { _, t in
                VStack(alignment: .leading, spacing: 4) {
                    HStack(spacing: 8) {
                        Text(t.name).font(.system(size: 13)).foregroundStyle(ink.opacity(0.88)).lineLimit(1)
                        Spacer(minLength: 6)
                        Text(tlDur(t.minutes)).font(AT.mono(11.5)).foregroundStyle(muted).lineLimit(1).fixedSize()
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
                if let t = s.summary { Text(t).font(.system(size: 12.5)).foregroundStyle(muted).fixedSize(horizontal: false, vertical: true) }
            }
        }
    }

    // MARK: Claude Code

    /// one row for the sessions of a place: each as a line (time, title, project and account, how long) that opens to its recap; a session alone opens to its recap
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
                Text(s.time).font(AT.mono(11.5)).foregroundStyle(muted).frame(width: 40, alignment: .leading)
                VStack(alignment: .leading, spacing: 1) {
                    Text(s.name).font(.system(size: 14)).foregroundStyle(ink).lineLimit(1)
                    let meta = [s.project, s.account].compactMap { $0 }.joined(separator: " · ")
                    if !meta.isEmpty { Text(meta).font(.system(size: 11.5)).foregroundStyle(muted).lineLimit(1) }
                }
                Spacer(minLength: 6)
                Text(s.right).font(AT.mono(11.5)).foregroundStyle(muted).lineLimit(1).fixedSize()
                if s.recap != nil {
                    Image(systemName: "chevron.right").font(.system(size: 10, weight: .semibold)).foregroundStyle(faint).rotationEffect(.degrees(shown ? 90 : 0))
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

    // MARK: what can be done with it

    @ViewBuilder private var buttons: some View {
        if e.actions.contains(where: { $0 != .sayWhat }) {
            HStack(spacing: 8) {
                ForEach(e.actions.filter { $0 != .mark && $0 != .unmark && $0 != .sayWhat }.sorted { rank($0) < rank($1) }, id: \.self) { a in
                    Button { tl.perform(a, on: e, engine: engine) } label: {
                        Text(a.title).font(.system(size: 14, weight: .semibold)).lineLimit(1).fixedSize()
                            .foregroundStyle(strong(a) ? AT.navy : ink)
                            .padding(.horizontal, 16).frame(height: 38)
                            .background(strong(a) ? AT.pale : Color.white.opacity(0.1), in: Capsule())
                            .contentShape(Capsule().inset(by: -3))
                    }
                    .buttonStyle(PressScale())
                }
                Spacer(minLength: 0)
                if e.starrable { TLStarButton(on: e.lit && !e.hers, hers: e.hers, pop: tl.pop[e.id] ?? 0, reduce: reduce) { tl.toggleStar(e) } }
            }
            .animation(.easeOut(duration: reduce ? TLMotion.reducedFade : 0.2), value: e.actions)          // 是我的 and 不是 go when one of them is said
        }
    }
    /// 这段在干嘛, the last and the quietest: a line of its own under the buttons (they have no room left for it beside them) and not a capsule, for a stretch where he goes looking for it
    private var sayWhatLink: some View {
        Button { tl.perform(.sayWhat, on: e, engine: engine) } label: {
            HStack(spacing: 6) {
                Image(systemName: TLAction.sayWhat.symbol).font(.system(size: 12))
                Text(TLAction.sayWhat.title).font(.system(size: 13))
            }
            .foregroundStyle(muted).frame(minHeight: 38, alignment: .leading).contentShape(Rectangle().inset(by: -4))
        }
        .buttonStyle(.plain)
    }
    /// what the row opened is for comes first, and in the light fill; asking her is always there and quieter
    private func strong(_ a: TLAction) -> Bool { a == .acknowledge || a == .openTalk || a == .summarize || a == .confirm }
    private func rank(_ a: TLAction) -> Int { strong(a) ? 0 : a == .rename || a == .name || a == .deny ? 1 : 2 }

    /// the name of a place he keeps, typed in the row (or the first name of one he keeps none at, with the kind of place to choose under it): done sends it, the keyboard's return too; what
    /// the brain refuses says why under it
    private var renameField: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                TextField("这个地方叫什么", text: $draft)
                    .focused($typing).submitLabel(.done).onSubmit { tl.rename(e, to: draft) }
                    .font(.system(size: 15)).foregroundStyle(ink).tint(ink)
                    .padding(.horizontal, 14).frame(height: 42)
                    .background(Color.white.opacity(0.08), in: Capsule())
                    .overlay(Capsule().stroke(ink.opacity(typing ? 0.45 : 0.12), lineWidth: 1))
                Button { tl.rename(e, to: draft) } label: {
                    Text("完成").font(.system(size: 14, weight: .semibold)).foregroundStyle(AT.navy).padding(.horizontal, 16).frame(height: 42)
                        .background(AT.pale.opacity(tl.renameBusy ? 0.5 : 1), in: Capsule()).contentShape(Capsule())
                }
                .buttonStyle(PressScale()).disabled(tl.renameBusy)
            }
            if e.place?.canName == true { roleChips }
            HStack(spacing: 12) {
                Button { tl.cancelRename() } label: { Text("取消").font(.system(size: 13)).foregroundStyle(muted).frame(minWidth: 44, minHeight: 32, alignment: .leading).contentShape(Rectangle()) }
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
                        .foregroundStyle(on ? AT.navy : ink)
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
        DispatchQueue.main.async { typing = true }
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
                Text("图片已过期").font(.system(size: 13)).foregroundStyle(muted)
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
                    Text(name).font(.system(size: 9.5)).foregroundStyle(faint).frame(width: Self.labelW, height: Self.laneH, alignment: .leading).offset(y: Double(i) * Self.laneH)
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
                Text(clock(t0)).font(AT.mono(10)).foregroundStyle(faint)
                Spacer()
                Text(tlEnd(t1)).font(AT.mono(10)).foregroundStyle(faint)
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
                Text(note).font(.system(size: 13.5)).foregroundStyle(ink.opacity(0.9)).lineLimit(1).padding(.horizontal, 16)
            } else {
                Text("改好了").font(.system(size: 13.5)).foregroundStyle(ink.opacity(0.9)).padding(.leading, 16)
                Text(" · ").font(.system(size: 13.5)).foregroundStyle(muted)
                Button(action: take) {
                    Text("撤销").font(.system(size: 13.5, weight: .semibold)).foregroundStyle(ink).padding(.trailing, 16).frame(minWidth: 44, minHeight: 44).contentShape(Rectangle())
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
