import AppKit
import SwiftUI

// MARK: The conversation, one turn at a time: the owner's message, then the agent's answer. How it got
// there (tools, thinking, notes in between) folds under "Worked for 1m 12s"; what the CLI reported
// (time, model, tokens) sits under the answer with Copy and more.

struct ChatTurn: Identifiable {
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

    init(model: ChatModel) {
        self.model = model
    }

    var body: some View {
        VStack(spacing: 0) {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 26) {
                        ForEach(turns) { turn in
                            TurnView(turn: turn, agent: model.chat?.agent ?? "", agentLabel: agentLabel,
                                     live: isLive(turn), startedAt: model.chat?.turnStartedAt ?? 0,
                                     answer: { item, option in model.answer(item, option: option) },
                                     retry: { model.retry($0) })
                                .id(turn.id)
                        }
                        // The lazy stack shows this only when it scrolls into view: then the owner is at the bottom.
                        Color.clear.frame(height: 1).id("end")
                            .onAppear { atBottom = true }
                            .onDisappear { atBottom = false }
                    }
                    .frame(maxWidth: ChatStyle.readingWidth)
                    .padding(.horizontal, VisualTokens.Space.l)
                    .padding(.top, 22).padding(.bottom, 12)
                    .frame(maxWidth: .infinity)
                }
                .onChange(of: model.items.last?.rev) { _ in
                    // Follow a streaming answer only while the owner is at the bottom.
                    if atBottom { withAnimation(.easeOut(duration: 0.2)) { proxy.scrollTo("end", anchor: .bottom) } }
                }
                .onChange(of: model.selectedID) { _ in
                    DispatchQueue.main.async { proxy.scrollTo("end", anchor: .bottom) }
                }
                .onAppear { proxy.scrollTo("end", anchor: .bottom) }
                .overlay(alignment: .bottom) {
                    if !atBottom {
                        Button {
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
                .animation(.easeOut(duration: 0.18), value: atBottom)
            }
            ChatComposer(model: model)
        }
    }

    private var turns: [ChatTurn] { ChatTurn.group(model.items) }

    private func isLive(_ turn: ChatTurn) -> Bool {
        model.chat?.running == true && turn.id == turns.last?.id
    }

    private var agentLabel: String {
        model.agents.first { $0.id == model.chat?.agent }?.label ?? "The agent"
    }
}

// MARK: One turn

private struct TurnView: View {
    let turn: ChatTurn
    let agent: String
    let agentLabel: String
    let live: Bool
    let startedAt: Double
    let answer: (ChatItem, ChatPermissionOption) -> Void
    let retry: (String) -> Void
    @MacMCPState private var openOverride: Bool?

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            if let user = turn.user {
                UserMessage(item: user)
            }
            if hasAgentPart {
                VStack(alignment: .leading, spacing: 12) {
                    workedHeader
                    if open && !turn.steps.isEmpty {
                        StepList(steps: turn.steps)
                            .transition(.opacity.combined(with: .move(edge: .top)))
                    }
                    ForEach(turn.asks) { ask in
                        PermissionCard(item: ask, agent: agentLabel, answer: { answer(ask, $0) })
                    }
                    if let reply = turn.answer {
                        MarkdownView(text: reply.text)
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
                } else if !turn.steps.isEmpty {
                    Text("\(turn.steps.count) steps")
                }
                if !turn.steps.isEmpty {
                    Text("· \(stepCount)").foregroundStyle(.tertiary)
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
    @MacMCPState private var hovering = false

    var body: some View {
        VStack(alignment: .trailing, spacing: 4) {
            Text(item.text)
                .font(.system(size: 14))
                .lineSpacing(3)
                .textSelection(.enabled)
                .padding(.horizontal, 14).padding(.vertical, 10)
                .background(RoundedRectangle(cornerRadius: 18, style: .continuous).fill(ChatStyle.userBubble))
                .frame(maxWidth: 560, alignment: .trailing)
            HStack(spacing: 6) {
                Text(ChatTime.clock(item.at)).font(.system(size: 11)).foregroundStyle(.tertiary)
                CopyButton(text: item.text, label: "Copy message")
            }
            .opacity(hovering ? 1 : 0)
        }
        .frame(maxWidth: .infinity, alignment: .trailing)
        .onHover { hovering = $0 }
        .animation(.easeOut(duration: 0.12), value: hovering)
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
            }
        }
    }

    private var hasDetail: Bool { !item.output.isEmpty || !item.input.isEmpty }

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
            ForEach(item.input.sorted(by: { $0.key < $1.key }), id: \.key) { key, value in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(key).font(.system(size: 11)).foregroundStyle(.secondary).frame(width: 84, alignment: .leading)
                    Text(value).font(.system(size: 11.5, design: .monospaced)).textSelection(.enabled).lineLimit(4)
                }
            }
            if !item.output.isEmpty {
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
