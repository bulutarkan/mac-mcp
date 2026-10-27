import AppKit
import SwiftUI

// MARK: The conversation, one turn at a time: the owner's message, then the agent's answer. How it got
// there (tools, thinking, notes in between) folds under "Worked for 1m 12s"; what the CLI reported
// (time, model, tokens) sits under the answer with Copy and more.

struct ChatTurn: Identifiable, Equatable {
    let id: String
    var user: ChatItem?
    var steps: [ChatItem] = []
    var asks: [ChatItem] = []      // questions still waiting for the owner: never folded away
    var answer: ChatItem?
    var summary: ChatItem?
    var notices: [ChatItem] = []

    static func group(_ items: [ChatItem]) -> [ChatTurn] {
        var turns: [ChatTurn] = []
        for item in items {
            if item.type == "user" || turns.isEmpty {
                turns.append(ChatTurn(id: item.id, user: item.type == "user" ? item : nil))
                if item.type == "user" { continue }
            }
            var turn = turns.removeLast()
            switch item.type {
            case "turn": turn.summary = item
            case "notice": turn.notices.append(item)
            case "permission" where item.status == "pending": turn.asks.append(item)
            default: turn.steps.append(item)
            }
            turns.append(turn)
        }
        // The answer is the agent's last words when nothing came after them; earlier words are steps.
        return turns.map { turn in
            var turn = turn
            if let last = turn.steps.last, last.type == "assistant" {
                turn.answer = turn.steps.removeLast()
            }
            return turn
        }
    }
}

struct ChatConversation: View {
    @ObservedObject var model: ChatModel
    @MacMCPState private var atBottom = true
    /// Stay at the newest words until the owner scrolls up; scrolling back to the end follows again.
    @MacMCPState private var following = true

    init(model: ChatModel) {
        self.model = model
    }

    var body: some View {
        VStack(spacing: 0) {
            ScrollViewReader { proxy in
                ScrollView {
                    // A plain stack: a lazy one guessed the heights of rows it had not drawn, so the content changed
                    // height while the owner scrolled and the scroll bar jumped. Turns are Equatable, so this stays cheap.
                    VStack(alignment: .leading, spacing: 26) {
                        ForEach(model.turns) { turn in
                            TurnView(turn: turn, agent: model.chat?.agent ?? "", agentLabel: agentLabel,
                                     live: running && turn.id == lastTurnID, startedAt: model.chat?.turnStartedAt ?? 0,
                                     answer: { item, option in model.answer(item, option: option) },
                                     retry: { model.retry($0) }, edit: { model.edit($0) })
                                .equatable()
                                .id(turn.id)
                        }
                        Color.clear.frame(height: 1).id("end")
                    }
                    .frame(maxWidth: ChatStyle.readingWidth)
                    .padding(.horizontal, VisualTokens.Space.l)
                    .padding(.top, 22).padding(.bottom, 12)
                    .frame(maxWidth: .infinity)
                    // Where a diff's relative path lives; it only changes with the chat.
                    .environment(\.chatProject, model.chat?.project ?? "")
                    // Where the owner is comes from the scroll view itself, never from layout: a state change
                    // driven by what the lazy stack happened to load made SwiftUI lay out again forever.
                    .background(ScrollPositionReader(
                        bottomChanged: { bottom in
                            if bottom != atBottom { atBottom = bottom }
                            if bottom && !following { following = true }
                        },
                        scrolledUp: { if following { following = false } }
                    ))
                }
                .onChange(of: model.items.last?.rev) { _ in
                    if following { proxy.scrollTo("end", anchor: .bottom) }
                }
                .task(id: running) {
                    // The answer also grows between updates (it is let out smoothly), so keep to the end while it runs.
                    while running && !Task.isCancelled {
                        if following { proxy.scrollTo("end", anchor: .bottom) }
                        try? await Task.sleep(nanoseconds: 180_000_000)
                    }
                }
                .onChange(of: model.selectedID) { _ in
                    following = true
                    DispatchQueue.main.async { proxy.scrollTo("end", anchor: .bottom) }
                }
                .onAppear { proxy.scrollTo("end", anchor: .bottom) }
                .overlay(alignment: .bottom) {
                    Group {
                    // Away from the end: always when the agent is idle, and while it works only once the owner
                    // scrolled up (otherwise the transcript is following the answer).
                    if showJump {
                        Button {
                            following = true
                            withAnimation(.easeOut(duration: 0.25)) { proxy.scrollTo("end", anchor: .bottom) }
                        } label: {
                            Image(systemName: "arrow.down").font(.system(size: 12, weight: .semibold))
                                .frame(width: 30, height: 30)
                                .background(Circle().fill(ChatStyle.raised))
                                .overlay(Circle().strokeBorder(VisualTokens.hairline))
                                .shadow(color: .black.opacity(0.12), radius: 6, y: 2)
                        }
                        .buttonStyle(.plain)
                        .help("Go to the latest message")
                        .padding(.bottom, 10)
                        .transition(.scale(scale: 0.8).combined(with: .opacity))
                    }
                    }
                    // Only the button animates: animating the scroll view would lay the transcript out again.
                    .animation(.easeOut(duration: 0.18), value: showJump)
                }
            }
            ChatComposer(model: model)
        }
    }

    private var running: Bool { model.chat?.running == true }
    private var lastTurnID: String? { model.turns.last?.id }
    private var showJump: Bool { !atBottom && (!following || !running) }

    private var agentLabel: String {
        model.agents.first { $0.id == model.chat?.agent }?.label ?? "The agent"
    }
}

// MARK: One turn

/// Equal when what it shows is equal, so a long chat redraws only the turn that changed.
private struct TurnView: View, Equatable {
    let turn: ChatTurn
    let agent: String
    let agentLabel: String
    let live: Bool
    let startedAt: Double
    let answer: (ChatItem, ChatPermissionOption) -> Void
    let retry: (String) -> Void
    let edit: (ChatItem) -> Void
    @MacMCPState private var openOverride: Bool?

    static func == (lhs: TurnView, rhs: TurnView) -> Bool {
        lhs.turn == rhs.turn && lhs.live == rhs.live && lhs.agent == rhs.agent && lhs.agentLabel == rhs.agentLabel
            && lhs.startedAt == rhs.startedAt
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            if let user = turn.user {
                UserMessage(item: user, edit: live ? nil : { edit(user) })
            }
            if hasAgentPart {
                VStack(alignment: .leading, spacing: 12) {
                    workedHeader
                    if open && !turn.steps.isEmpty {
                        StepList(steps: turn.steps)
                            .transition(.opacity.combined(with: .move(edge: .top)))
                    }
                    ForEach(turn.asks) { ask in
                        PermissionCard(item: ask, agent: agentLabel, shortcuts: live && ask.id == turn.asks.first?.id,
                                       answer: { answer(ask, $0) })
                    }
                    if let reply = turn.answer {
                        if live {
                            StreamingAnswer(text: reply.text)
                        } else {
                            MarkdownView(text: reply.text)
                        }
                    }
                    ForEach(turn.notices) { NoticeLine(item: $0) }
                    if !live, turn.answer != nil || turn.summary != nil {
                        AnswerFooter(turn: turn, retry: retry)
                    }
                }
                .padding(.leading, 34)
                .overlay(alignment: .topLeading) { AgentAvatar(agent: agent, size: 24) }
            }
        }
    }

    private var hasAgentPart: Bool {
        live || !turn.steps.isEmpty || turn.answer != nil || turn.summary != nil || !turn.asks.isEmpty
            || !turn.notices.isEmpty
    }

    /// Open while the agent works, folded once it answered; the owner's own click wins.
    private var open: Bool { openOverride ?? live }

    private var workedHeader: some View {
        Button {
            withAnimation(.spring(response: 0.32, dampingFraction: 0.9)) { openOverride = !open }
        } label: {
            HStack(spacing: 6) {
                Text(agentLabel).font(.system(size: 13, weight: .semibold)).foregroundStyle(.primary)
                if live {
                    TimelineView(.periodic(from: .now, by: 1)) { context in
                        Text("Working · \(ChatTime.duration(context.date.timeIntervalSince1970 - startedAt))")
                            .monospacedDigit()
                    }
                    PulseDot()
                } else if let summary = turn.summary {
                    Text(summary.stop == "cancelled" ? "Stopped after \(ChatTime.duration(summary.durationSeconds))"
                         : "Worked for \(ChatTime.duration(summary.durationSeconds))")
                }
                if !turn.steps.isEmpty {
                    Text(live || turn.summary != nil ? "· \(stepCount)" : stepCount).foregroundStyle(.tertiary)
                    Image(systemName: "chevron.right").font(.system(size: 9, weight: .bold))
                        .rotationEffect(.degrees(open ? 90 : 0))
                }
            }
            .font(.system(size: 12.5))
            .foregroundStyle(.secondary)
            .frame(height: 24)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .allowsHitTesting(!turn.steps.isEmpty)
    }

    private var stepCount: String {
        let tools = turn.steps.filter { $0.type == "tool" }.count
        return tools == 1 ? "1 step" : tools > 0 ? "\(tools) steps" : "\(turn.steps.count) notes"
    }
}

private struct UserMessage: View {
    let item: ChatItem
    let edit: (() -> Void)?
    @MacMCPState private var hovering = false
    /// Edit and Copy show while the pointer is over the message or either has the keyboard (Tab with keyboard
    /// navigation on), so they are never focused while invisible.
    @FocusState private var focusedAction: MessageAction?

    private enum MessageAction: Hashable {
        case edit
        case copy
    }

    var body: some View {
        VStack(alignment: .trailing, spacing: 4) {
            if !item.attachments.isEmpty {
                SentAttachments(files: item.attachments)
            }
            if !item.text.isEmpty {
            Text(item.text)
                .font(.system(size: 14))
                .lineSpacing(3)
                .textSelection(.enabled)
                .padding(.horizontal, 14).padding(.vertical, 10)
                .background(RoundedRectangle(cornerRadius: 18, style: .continuous).fill(ChatStyle.userBubble))
                .frame(maxWidth: 560, alignment: .trailing)
            }
            HStack(spacing: 6) {
                Text(ChatTime.clock(item.at)).font(.system(size: 11)).foregroundStyle(.tertiary)
                if let edit {
                    Button(action: edit) {
                        Image(systemName: "pencil").font(.system(size: 11, weight: .medium)).foregroundStyle(.secondary)
                            .frame(width: 22, height: 20).contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .help("Edit and send again")
                    .focused($focusedAction, equals: .edit)
                }
                CopyButton(text: item.text, label: "Copy message")
                    .focused($focusedAction, equals: .copy)
            }
            .opacity(hovering || focusedAction != nil ? 1 : 0)
        }
        .frame(maxWidth: .infinity, alignment: .trailing)
        .onHover { hovering = $0 }
        .animation(.easeOut(duration: 0.12), value: hovering || focusedAction != nil)
    }
}

/// A streaming answer: new words are let out smoothly rather than in the poll's bursts, and the
/// last few are drawn faint and settle in. Text already there when it appears is shown at once.
private struct StreamingAnswer: View {
    let text: String
    @MacMCPState private var shown = -1
    @MacMCPState private var settled = 0

    var body: some View {
        let visible = shown < 0 ? text.count : min(shown, text.count)
        MarkdownView(text: String(text.prefix(visible)), fade: min(48, max(0, visible - settled)))
            .onAppear {
                shown = text.count
                settled = text.count
            }
            .task(id: text.count) {
                while !Task.isCancelled {
                    let target = text.count
                    if shown >= target && settled >= shown { break }
                    let backlog = max(0, target - shown)
                    shown = min(target, shown + max(2, backlog / 6))
                    settled = min(shown, settled + max(1, (shown - settled) / 5))
                    try? await Task.sleep(nanoseconds: 33_000_000)
                }
            }
    }
}

// MARK: Steps

private struct StepList: View {
    let steps: [ChatItem]

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            ForEach(steps) { step in
                switch step.type {
                case "tool": StepRow(item: step)
                case "thought": ThoughtStep(text: step.text)
                case "plan": PlanCard(item: step)
                case "permission": PermissionCard(item: step, agent: "", answer: { _ in })
                case "assistant":
                    MarkdownView(text: step.text, size: 13).foregroundStyle(.secondary).padding(.vertical, 4)
                default: EmptyView()
                }
            }
        }
        .padding(.leading, 12)
        .overlay(alignment: .leading) { Rectangle().fill(VisualTokens.hairline).frame(width: 1.5) }
    }
}

/// A step on one line: what it was for, with the app it used (Finder, Terminal, Safari…) where it was Mac MCP.
private struct StepRow: View {
    let item: ChatItem
    @MacMCPState private var open = false

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Button { withAnimation(.easeOut(duration: 0.18)) { open.toggle() } } label: {
                HStack(spacing: 8) {
                    StepIcon(item: item).frame(width: 18, height: 18)
                    Text(item.title).font(.system(size: 13)).lineLimit(1).truncationMode(.middle)
                    if !source.isEmpty {
                        Text(source).font(.system(size: 11)).foregroundStyle(.tertiary).lineLimit(1)
                    }
                    if !item.diffs.isEmpty {
                        DiffCounts(added: item.diffs.reduce(0) { $0 + $1.added }, removed: item.diffs.reduce(0) { $0 + $1.removed })
                    }
                    if let code = item.terminal?.exitCode, code != 0 {
                        Text("exit \(code)").font(.system(size: 10.5, weight: .semibold, design: .monospaced))
                            .foregroundStyle(VisualTokens.error)
                    }
                    statusMark
                    if hasDetail {
                        Image(systemName: "chevron.right").font(.system(size: 8, weight: .bold)).foregroundStyle(.tertiary)
                            .rotationEffect(.degrees(open ? 90 : 0))
                    }
                    Spacer(minLength: 0)
                }
                .padding(.vertical, 4)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .allowsHitTesting(hasDetail)
            if open && hasDetail {
                StepDetail(item: item).padding(.leading, 26).padding(.bottom, 4)
            } else if item.status == "running", let terminal = item.terminal, !terminal.output.isEmpty {
                // A command at work shows its newest lines as they come, without opening the step.
                TerminalBlock(terminal: terminal, live: true).padding(.leading, 26).padding(.bottom, 4)
            }
        }
    }

    private var hasDetail: Bool {
        !item.output.isEmpty || !item.input.isEmpty || !item.diffs.isEmpty || !(item.terminal?.output.isEmpty ?? true)
    }

    private var source: String {
        if item.server == "mac-mcp" { return "Mac MCP" }
        if !item.server.isEmpty {
            return item.server.replacingOccurrences(of: "claude_ai_", with: "").replacingOccurrences(of: "_", with: " ")
        }
        return ""
    }

    @ViewBuilder private var statusMark: some View {
        switch item.status {
        case "running": ProgressView().controlSize(.mini)
        case "done": Image(systemName: "checkmark").font(.system(size: 10, weight: .bold)).foregroundStyle(VisualTokens.success)
        case "failed": Image(systemName: "xmark").font(.system(size: 10, weight: .bold)).foregroundStyle(VisualTokens.error)
        default: Image(systemName: "minus").font(.system(size: 10, weight: .bold)).foregroundStyle(.tertiary)
        }
    }
}

/// Mac MCP tools show the Mac app they worked in; the agent's own steps a symbol for what they did.
private struct StepIcon: View {
    let item: ChatItem

    var body: some View {
        if item.server == "mac-mcp", case .app(let key) = IslandIcon.forTool(item.tool, target: nil),
           let image = IslandIcon.image(for: key) {
            Image(nsImage: image).resizable().interpolation(.high).scaledToFit()
        } else {
            Image(systemName: symbol)
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(item.server == "mac-mcp" ? ChatStyle.accent : Color.secondary)
                .frame(width: 18, height: 18)
                .background(RoundedRectangle(cornerRadius: 5, style: .continuous).fill(ChatStyle.code))
        }
    }

    private var symbol: String {
        if item.server == "mac-mcp", case .symbol(let name) = IslandIcon.forTool(item.tool, target: nil) { return name }
        if !item.server.isEmpty { return "puzzlepiece.extension" }
        switch item.kind {
        case "read": return "doc.text"
        case "edit": return "pencil"
        case "delete": return "trash"
        case "move": return "arrow.right.doc.on.clipboard"
        case "search": return "magnifyingglass"
        case "execute": return "terminal"
        case "fetch": return "globe"
        case "think": return "brain"
        default: return "wrench.and.screwdriver"
        }
    }
}

private struct StepDetail: View {
    let item: ChatItem

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            // An edit shows what changed; its raw old and new text would only repeat that.
            ForEach(item.diffs) { DiffBlock(diff: $0) }
            ForEach(item.diffs.isEmpty ? item.input.sorted(by: { $0.key < $1.key }) : [], id: \.key) { key, value in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(key).font(.system(size: 11)).foregroundStyle(.secondary).frame(width: 84, alignment: .leading)
                    Text(value).font(.system(size: 11.5, design: .monospaced)).textSelection(.enabled).lineLimit(4)
                }
            }
            if let terminal = item.terminal, !terminal.output.isEmpty {
                TerminalBlock(terminal: terminal, live: item.status == "running")
            } else if !item.output.isEmpty {
                ScrollView {
                    Text(item.output).font(.system(size: 11.5, design: .monospaced)).textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(maxHeight: 200)
                .padding(10)
                .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(ChatStyle.code))
            }
        }
    }
}

/// A command's output the way a terminal shows it. Live: its newest lines as they stream in; afterwards all of it,
/// with how the command ended.
private struct TerminalBlock: View {
    let terminal: ChatTerminal
    let live: Bool

    private static let liveLines = 12

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 6) {
                Image(systemName: "terminal").font(.system(size: 10, weight: .semibold))
                Text(live ? "Running" : "Output").font(.system(size: 10.5, weight: .semibold))
                Spacer(minLength: 6)
                if let code = terminal.exitCode {
                    Text(code == 0 ? "exit 0" : "exit \(code)")
                        .font(.system(size: 10.5, weight: .semibold, design: .monospaced))
                        .foregroundStyle(code == 0 ? VisualTokens.success : VisualTokens.error)
                } else if live {
                    ProgressView().controlSize(.mini)
                }
                if !live {
                    Button {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(terminal.output, forType: .string)
                    } label: {
                        Image(systemName: "doc.on.doc").font(.system(size: 10))
                    }
                    .buttonStyle(.plain).help("Copy the output")
                }
            }
            .foregroundStyle(Self.dim)
            .padding(.horizontal, 10).padding(.vertical, 6)
            Divider().overlay(Color.white.opacity(0.08))
            if live {
                Text(tail).font(.system(size: 11.5, design: .monospaced)).foregroundStyle(Self.ink)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(10)
            } else {
                ScrollView {
                    Text(terminal.output.trimmingCharacters(in: .newlines))
                        .font(.system(size: 11.5, design: .monospaced)).foregroundStyle(Self.ink).textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(10)
                }
                .frame(maxHeight: 260)
            }
        }
        .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(Self.ground))
        .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous).strokeBorder(Color.white.opacity(0.06)))
        .accessibilityElement(children: .combine)
        .accessibilityLabel(live ? "Command output, running" : "Command output")
    }

    /// The last lines only: the block keeps one height while output pours in.
    private var tail: String {
        let lines = terminal.output.trimmingCharacters(in: .newlines).split(separator: "\n", omittingEmptySubsequences: false)
        return lines.suffix(Self.liveLines).joined(separator: "\n")
    }

    // A terminal is dark in both appearances.
    private static let ground = Color(red: 0.11, green: 0.11, blue: 0.12)
    private static let ink = Color(red: 0.88, green: 0.89, blue: 0.86)
    private static let dim = Color(red: 0.62, green: 0.63, blue: 0.62)
}

private struct ThoughtStep: View {
    let text: String
    @MacMCPState private var open = false

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Button { withAnimation(.easeOut(duration: 0.18)) { open.toggle() } } label: {
                HStack(spacing: 8) {
                    Image(systemName: "brain").font(.system(size: 11)).foregroundStyle(.secondary)
                        .frame(width: 18, height: 18)
                    Text(open ? "Thinking" : firstLine).font(.system(size: 13)).foregroundStyle(.secondary).lineLimit(1)
                    Spacer(minLength: 0)
                }
                .padding(.vertical, 4)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            if open {
                Text(text).font(.system(size: 12.5)).foregroundStyle(.secondary).italic().textSelection(.enabled)
                    .padding(.leading, 26).padding(.bottom, 4)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var firstLine: String {
        text.split(separator: "\n").first.map(String.init)?.trimmingCharacters(in: .whitespaces) ?? "Thinking"
    }
}

// MARK: Under the answer

private struct AnswerFooter: View {
    let turn: ChatTurn
    let retry: (String) -> Void

    var body: some View {
        HStack(spacing: 4) {
            CopyButton(text: turn.answer?.text ?? "", label: "Copy answer")
                .disabled(turn.answer == nil)
            Menu {
                Button("Copy Answer") { copy(turn.answer?.text ?? "") }.disabled(turn.answer == nil)
                Button("Copy Your Message") { copy(turn.user?.text ?? "") }.disabled(turn.user == nil)
                Divider()
                Button("Ask Again") { if let text = turn.user?.text { retry(text) } }.disabled(turn.user == nil)
            } label: {
                Image(systemName: "ellipsis").font(.system(size: 11, weight: .semibold)).foregroundStyle(.secondary)
                    .frame(width: 22, height: 20)
            }
            .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
            .help("More")
            Text(details).font(.system(size: 11)).foregroundStyle(.tertiary).lineLimit(1).padding(.leading, 6)
                .monospacedDigit()
        }
    }

    /// "14:32 · Opus 5.5 · 12.4K tokens": what the CLI reported for this answer.
    private var details: String {
        var parts: [String] = []
        let time = turn.summary?.at ?? turn.answer?.at ?? 0
        if time > 0 { parts.append(ChatTime.clock(time)) }
        if let summary = turn.summary {
            if !summary.model.isEmpty { parts.append(summary.model) }
            if summary.tokens > 0 { parts.append(ChatTime.tokens(summary.tokens)) }
        }
        return parts.joined(separator: " · ")
    }

    private func copy(_ text: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }
}

private struct NoticeLine: View {
    let item: ChatItem

    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: item.tone == "error" ? "exclamationmark.triangle.fill"
                  : item.tone == "warning" ? "exclamationmark.circle" : "stop.circle")
            Text(item.text).textSelection(.enabled)
        }
        .font(.system(size: 12))
        .foregroundStyle(item.tone == "error" ? VisualTokens.error : item.tone == "warning" ? VisualTokens.warning : .secondary)
    }
}

/// Reads the transcript's own NSScrollView: whether its end is in view, and when the owner scrolls up over it
/// (wheel or trackpad). Both come from scrolling, so nothing here can be set off by a layout pass.
private struct ScrollPositionReader: NSViewRepresentable {
    let bottomChanged: (Bool) -> Void
    let scrolledUp: () -> Void

    func makeNSView(context: Context) -> ScrollPositionView {
        let view = ScrollPositionView()
        view.bottomChanged = bottomChanged
        view.scrolledUp = scrolledUp
        return view
    }

    func updateNSView(_ view: ScrollPositionView, context: Context) {
        view.bottomChanged = bottomChanged
        view.scrolledUp = scrolledUp
    }
}

final class ScrollPositionView: NSView {
    var bottomChanged: ((Bool) -> Void)?
    var scrolledUp: (() -> Void)?
    private weak var watched: NSScrollView?
    private var lastBottom: Bool?
    private var wheel: Any?

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        NotificationCenter.default.removeObserver(self)
        if let wheel { NSEvent.removeMonitor(wheel) }
        wheel = nil
        guard window != nil, let scrollView = enclosingScrollView else { return }
        watched = scrollView
        scrollView.contentView.postsBoundsChangedNotifications = true
        NotificationCenter.default.addObserver(self, selector: #selector(scrolled), name: NSView.boundsDidChangeNotification,
                                               object: scrollView.contentView)
        wheel = NSEvent.addLocalMonitorForEvents(matching: .scrollWheel) { [weak self] event in
            if event.scrollingDeltaY > 0.5, let self, let scrollView = self.watched, event.window === scrollView.window,
               scrollView.bounds.contains(scrollView.convert(event.locationInWindow, from: nil)) {
                self.scrolledUp?()
            }
            return event
        }
        scrolled()
    }

    @objc private func scrolled() {
        guard let scrollView = watched, let document = scrollView.documentView else { return }
        let visible = scrollView.documentVisibleRect
        let height = document.bounds.height
        let distance = document.isFlipped ? height - visible.maxY : visible.minY
        let bottom = distance <= 48
        guard bottom != lastBottom else { return }
        lastBottom = bottom
        DispatchQueue.main.async { [weak self] in self?.bottomChanged?(bottom) }
    }

    deinit {
        NotificationCenter.default.removeObserver(self)
        if let wheel { NSEvent.removeMonitor(wheel) }
    }
}

/// "+12 −3": what an edit added and took away.
private struct DiffCounts: View {
    let added: Int
    let removed: Int

    var body: some View {
        HStack(spacing: 4) {
            if added > 0 { Text("+\(added)").foregroundStyle(VisualTokens.success) }
            if removed > 0 { Text("−\(removed)").foregroundStyle(VisualTokens.error) }
        }
        .font(.system(size: 11, weight: .semibold, design: .monospaced))
        .monospacedDigit()
    }
}

private struct ChatProjectKey: EnvironmentKey {
    static let defaultValue = ""
}

extension EnvironmentValues {
    /// The open chat's project folder: a diff's relative path is inside it.
    var chatProject: String {
        get { self[ChatProjectKey.self] }
        set { self[ChatProjectKey.self] = newValue }
    }
}

/// One file's edit: added lines on green, removed on red, the rest as context. The file's name opens the file;
/// the expand button shows the whole diff in a sheet.
private struct DiffBlock: View {
    let diff: ChatDiff
    @Environment(\.chatProject) private var project
    @MacMCPState private var missing = false
    @MacMCPState private var showingAll = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 8) {
                Image(systemName: diff.created ? "doc.badge.plus" : "doc.text").font(.system(size: 11)).foregroundStyle(.secondary)
                Button(action: openFile) {
                    Text((diff.path as NSString).lastPathComponent).font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(missing ? Color.secondary : ChatStyle.accent)
                        .strikethrough(missing, color: .secondary)
                }
                .buttonStyle(.plain)
                .help(missing ? "\(path) is no longer there" : "Open \(path)")
                .contextMenu {
                    Button("Open", action: openFile)
                    Button("Show in Finder") { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)]) }
                    Button("Copy Path") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(path, forType: .string)
                    }
                }
                if diff.created { Text("new file").font(.system(size: 11)).foregroundStyle(.tertiary) }
                DiffCounts(added: diff.added, removed: diff.removed)
                Spacer()
                Button { showingAll = true } label: {
                    Image(systemName: "arrow.up.left.and.arrow.down.right").font(.system(size: 10, weight: .medium))
                        .foregroundStyle(.secondary).frame(width: 22, height: 20).contentShape(Rectangle())
                }
                .buttonStyle(.plain).help("Show the whole diff")
                CopyButton(text: diff.lines.joined(separator: "\n"), label: "Copy diff")
            }
            .padding(.horizontal, 10).padding(.vertical, 6)
            .background(ChatStyle.code)
            ScrollView {
                DiffLines(diff: diff)
            }
            .frame(maxHeight: 320)
        }
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous).strokeBorder(VisualTokens.hairline))
        .sheet(isPresented: $showingAll) { DiffSheet(diff: diff, path: path, missing: missing, open: openFile) }
    }

    private var path: String { ChatLibrary.filePath(diff.path, project: project) }

    /// In the app the owner uses for that kind of file; a file that is gone is marked so instead.
    private func openFile() {
        guard FileManager.default.fileExists(atPath: path) else {
            missing = true
            NSSound.beep()
            return
        }
        missing = false
        NSWorkspace.shared.open(URL(fileURLWithPath: path))
    }
}

/// A diff's lines, tinted: shared by the step and the whole-diff sheet.
private struct DiffLines: View {
    let diff: ChatDiff

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(diff.lines.enumerated()), id: \.offset) { _, line in
                Text(line.isEmpty ? " " : line)
                    .font(.system(size: 11.5, design: .monospaced))
                    .foregroundStyle(line.hasPrefix("@@") ? Color.secondary : Color.primary.opacity(0.9))
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 10).padding(.vertical, 1)
                    .background(tint(line))
            }
            if diff.truncated {
                Text("… more changes not shown: open the file to see all of it").font(.system(size: 11))
                    .foregroundStyle(.tertiary).padding(10)
            }
        }
        .textSelection(.enabled)
    }

    private func tint(_ line: String) -> Color {
        if line.hasPrefix("+") { return VisualTokens.success.opacity(0.14) }
        if line.hasPrefix("-") { return VisualTokens.error.opacity(0.12) }
        if line.hasPrefix("@@") { return ChatStyle.code.opacity(0.6) }
        return .clear
    }
}

/// The whole diff of one file, large, with the file a click away.
private struct DiffSheet: View {
    let diff: ChatDiff
    let path: String
    let missing: Bool
    let open: () -> Void
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Image(systemName: diff.created ? "doc.badge.plus" : "doc.text").foregroundStyle(.secondary)
                VStack(alignment: .leading, spacing: 1) {
                    Text((path as NSString).lastPathComponent).font(.headline)
                    Text(path).font(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.head)
                        .textSelection(.enabled)
                }
                DiffCounts(added: diff.added, removed: diff.removed)
                Spacer()
                Button("Open File", action: open).disabled(missing)
                Button("Done") { dismiss() }.keyboardShortcut(.cancelAction)
            }
            .padding(14)
            Divider()
            ScrollView { DiffLines(diff: diff).padding(.vertical, 6) }
                .background(ChatStyle.canvas)
        }
        .frame(minWidth: 640, idealWidth: 820, minHeight: 420, idealHeight: 640)
    }
}
