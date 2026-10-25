import AppKit
import SwiftUI

// MARK: Mac MCP Chat's own look: warm paper and the brand's champagne, not a stock split view.

enum ChatStyle {
    static let canvas = VisualTokens.adaptive(light: 0xFAF8F5, dark: 0x171615)
    static let sidebar = VisualTokens.adaptive(light: 0xF2EFEA, dark: 0x1F1E1C)
    static let raised = VisualTokens.adaptive(light: 0xFFFFFF, dark: 0x252422)
    static let userBubble = VisualTokens.adaptive(light: 0xEDE7DF, dark: 0x2C2A27)
    static let selection = VisualTokens.adaptive(light: 0xE4DED5, dark: 0x302E2B)
    static let accent = VisualTokens.brand
    static let onAccent = VisualTokens.onBrand
    static let code = VisualTokens.adaptive(light: 0xF3EFE9, dark: 0x1E1D1B)
    static let readingWidth: CGFloat = 720
    static let sidebarWidth: CGFloat = 264
    static let headerHeight: CGFloat = 52
    static let slide = Animation.spring(response: 0.38, dampingFraction: 0.88)
}

/// The maker's logo for an agent (Claude, OpenAI for Codex, OpenCode, Copilot), else a quiet mark.
struct AgentAvatar: View {
    let agent: String
    var size: CGFloat = 22

    var body: some View {
        Group {
            if let image = IslandIcon.logo(logoName) {
                Image(nsImage: image).resizable().interpolation(.high).scaledToFit().padding(size * 0.16)
            } else {
                Image(systemName: "sparkles").font(.system(size: size * 0.5, weight: .semibold))
                    .foregroundStyle(ChatStyle.accent)
            }
        }
        .frame(width: size, height: size)
        .background(Circle().fill(ChatStyle.raised))
        .overlay(Circle().strokeBorder(VisualTokens.hairline))
    }

    private var logoName: String { agent == "codex" ? "openai" : agent }
}

// MARK: Window content

struct ChatView: View {
    @ObservedObject var model: ChatModel
    @MacMCPState private var showingNewChat = false
    @MacMCPState private var sidebarShown: Bool

    init(model: ChatModel, sidebarShown: Bool = true) {
        self.model = model
        _sidebarShown = MacMCPState(initialValue: sidebarShown)
    }

    var body: some View {
        HStack(spacing: 0) {
            if sidebarShown {
                ChatSidebar(model: model, newChat: { showingNewChat = true }, hide: toggleSidebar)
                    .frame(width: ChatStyle.sidebarWidth)
                    .transition(.move(edge: .leading).combined(with: .opacity))
            }
            VStack(spacing: 0) {
                ChatHeaderBar(model: model, sidebarShown: sidebarShown, toggleSidebar: toggleSidebar,
                              newChat: { showingNewChat = true })
                if model.selectedID != nil {
                    ChatConversation(model: model)
                } else {
                    ChatWelcome(model: model, newChat: { showingNewChat = true })
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(ChatStyle.canvas)
        }
        .background(ChatStyle.canvas)
        .ignoresSafeArea()
        .background(
            Button("", action: toggleSidebar).keyboardShortcut("s", modifiers: [.control, .command]).hidden()
        )
        .sheet(isPresented: $showingNewChat) { NewChatSheet(model: model) }
        .task {
            await model.refreshList()
            if model.selectedID == nil, let first = model.allChats.first { model.select(first.id) }
        }
    }

    private func toggleSidebar() {
        withAnimation(ChatStyle.slide) { sidebarShown.toggle() }
    }
}

// MARK: Sidebar

private struct ChatSidebar: View {
    @ObservedObject var model: ChatModel
    let newChat: () -> Void
    let hide: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Spacer()
                SidebarToggle(action: hide).help("Hide the sidebar (⌃⌘S)")
            }
            .frame(height: ChatStyle.headerHeight)
            .padding(.horizontal, 12)

            Button(action: newChat) {
                HStack(spacing: 10) {
                    Image(systemName: "plus")
                        .font(.system(size: 11, weight: .bold))
                        .foregroundStyle(ChatStyle.onAccent)
                        .frame(width: 20, height: 20)
                        .background(Circle().fill(ChatStyle.accent))
                    Text("New chat").font(.system(size: 13, weight: .medium))
                    Spacer()
                    Text("⌘N").font(.caption).foregroundStyle(.tertiary)
                }
                .padding(.horizontal, 10).padding(.vertical, 8)
                .contentShape(Rectangle())
            }
            .buttonStyle(SidebarRowStyle(selected: false))
            .keyboardShortcut("n", modifiers: .command)
            .padding(.horizontal, 10)

            ScrollView {
                LazyVStack(alignment: .leading, spacing: 0) {
                    ForEach(model.projects) { project in
                        ProjectSection(project: project, model: model)
                    }
                }
                .padding(.horizontal, 10)
                .padding(.bottom, 16)
            }
        }
        .background(ChatStyle.sidebar)
        .overlay(alignment: .trailing) { Rectangle().fill(VisualTokens.hairline).frame(width: 1) }
    }
}

/// A project and its chats. The chat titles line up with the folder's name, so the list reads as one tree.
private struct ProjectSection: View {
    let project: ChatProjectGroup
    @ObservedObject var model: ChatModel

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 8) {
                Image(systemName: "folder.fill").font(.system(size: 11)).foregroundStyle(.secondary)
                    .frame(width: 16)
                Text(project.name).font(.system(size: 12, weight: .semibold)).foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            .padding(.horizontal, 10)
            .padding(.bottom, 6)
            .help(project.id)
            ForEach(project.chats) { chat in
                ChatRow(chat: chat, selected: chat.id == model.selectedID) { model.select(chat.id) }
                    .contextMenu {
                        Button("Delete Chat", role: .destructive) { model.delete(chat.id) }
                    }
            }
        }
        .padding(.top, 18)
    }
}

private struct ChatRow: View {
    let chat: ChatSummary
    let selected: Bool
    let open: () -> Void

    var body: some View {
        Button(action: open) {
            HStack(spacing: 8) {
                status.frame(width: 16)
                Text(chat.title).font(.system(size: 13, weight: selected ? .medium : .regular))
                    .foregroundStyle(Color.primary.opacity(selected ? 1 : 0.85))
                    .lineLimit(1)
                Spacer(minLength: 6)
                Text(ChatTime.short(chat.updatedAt)).font(.system(size: 11)).foregroundStyle(.tertiary)
                    .monospacedDigit()
            }
            .padding(.horizontal, 10).padding(.vertical, 7)
            .contentShape(Rectangle())
        }
        .buttonStyle(SidebarRowStyle(selected: selected))
    }

    @ViewBuilder private var status: some View {
        if chat.status == "waiting" {
            Image(systemName: "hand.raised.fill").font(.system(size: 10)).foregroundStyle(VisualTokens.warning)
                .help("Waiting for your answer")
        } else if chat.running {
            PulseDot()
        } else {
            Color.clear
        }
    }
}

private struct SidebarRowStyle: ButtonStyle {
    let selected: Bool

    func makeBody(configuration: Configuration) -> some View {
        SidebarRowBody(label: configuration.label, pressed: configuration.isPressed, selected: selected)
    }
}

private struct SidebarRowBody<Label: View>: View {
    let label: Label
    let pressed: Bool
    let selected: Bool
    @MacMCPState private var hovering = false

    var body: some View {
        label
            .background(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .fill(selected ? ChatStyle.selection : hovering ? ChatStyle.selection.opacity(0.55) : .clear)
            )
            .overlay(alignment: .leading) {
                if selected {
                    Capsule().fill(ChatStyle.accent).frame(width: 3, height: 16).offset(x: -1)
                }
            }
            .opacity(pressed ? 0.75 : 1)
            .onHover { hovering = $0 }
            .animation(.easeOut(duration: 0.12), value: hovering)
    }
}

private struct SidebarToggle: View {
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: "sidebar.left").font(.system(size: 14, weight: .regular))
                .foregroundStyle(.secondary)
                .frame(width: 28, height: 24)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}

/// A gold dot that breathes while the agent works. The breathing is a Core Animation layer animation:
/// a SwiftUI repeatForever animation here made SwiftUI lay the whole transcript out on every frame,
/// which pinned the main thread in long chats.
struct PulseDot: NSViewRepresentable {
    func makeNSView(context: Context) -> PulseDotView { PulseDotView() }
    func updateNSView(_ view: PulseDotView, context: Context) { view.refreshColor() }
    func sizeThatFits(_ proposal: ProposedViewSize, nsView: PulseDotView, context: Context) -> CGSize? {
        CGSize(width: 7, height: 7)
    }
}

final class PulseDotView: NSView {
    private let dot = CALayer()

    override init(frame: NSRect) {
        super.init(frame: NSRect(x: 0, y: 0, width: 7, height: 7))
        wantsLayer = true
        dot.frame = bounds
        dot.cornerRadius = 3.5
        layer?.addSublayer(dot)
        refreshColor()
        setAccessibilityLabel("Working")
        setAccessibilityRole(.image)
    }

    required init?(coder: NSCoder) { nil }

    override var intrinsicContentSize: NSSize { NSSize(width: 7, height: 7) }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        refreshColor()
    }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        dot.removeAllAnimations()
        guard window != nil, !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion else { return }
        let fade = CABasicAnimation(keyPath: "opacity")
        fade.fromValue = 1
        fade.toValue = 0.3
        let shrink = CABasicAnimation(keyPath: "transform.scale")
        shrink.fromValue = 1
        shrink.toValue = 0.8
        let breathe = CAAnimationGroup()
        breathe.animations = [fade, shrink]
        breathe.duration = 0.9
        breathe.autoreverses = true
        breathe.repeatCount = .infinity
        breathe.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
        dot.add(breathe, forKey: "breathe")
    }

    func refreshColor() {
        effectiveAppearance.performAsCurrentDrawingAppearance {
            dot.backgroundColor = NSColor(ChatStyle.accent).cgColor
        }
    }
}

enum ChatTime {
    /// "now", "4m", "3h", "Yesterday", "12 Oct": short enough for the sidebar.
    static func short(_ timestamp: Double) -> String {
        guard timestamp > 0 else { return "" }
        let seconds = Date().timeIntervalSince1970 - timestamp
        if seconds < 60 { return "now" }
        if seconds < 3600 { return "\(Int(seconds / 60))m" }
        let date = Date(timeIntervalSince1970: timestamp)
        if Calendar.current.isDateInToday(date) { return "\(Int(seconds / 3600))h" }
        if Calendar.current.isDateInYesterday(date) { return "Yesterday" }
        return date.formatted(.dateTime.day().month(.abbreviated))
    }

    static func clock(_ timestamp: Double) -> String {
        guard timestamp > 0 else { return "" }
        return Date(timeIntervalSince1970: timestamp).formatted(date: .omitted, time: .shortened)
    }

    /// "4s", "1m 12s", "1h 3m".
    static func duration(_ seconds: Double) -> String {
        let total = Int(seconds.rounded())
        if total < 60 { return "\(max(total, 1))s" }
        if total < 3600 { return total % 60 == 0 ? "\(total / 60)m" : "\(total / 60)m \(total % 60)s" }
        return "\(total / 3600)h \((total % 3600) / 60)m"
    }

    /// "820 tokens", "12.4K tokens".
    static func tokens(_ count: Int) -> String {
        count < 1000 ? "\(count) tokens" : String(format: "%.1fK tokens", Double(count) / 1000)
    }
}

// MARK: Header

private struct ChatHeaderBar: View {
    @ObservedObject var model: ChatModel
    let sidebarShown: Bool
    let toggleSidebar: () -> Void
    let newChat: () -> Void

    var body: some View {
        HStack(spacing: 10) {
            if !sidebarShown {
                // Clear of the window's traffic lights.
                SidebarToggle(action: toggleSidebar).help("Show the sidebar (⌃⌘S)").padding(.leading, 78)
                Button(action: newChat) {
                    Image(systemName: "square.and.pencil").font(.system(size: 14)).foregroundStyle(.secondary)
                        .frame(width: 28, height: 24).contentShape(Rectangle())
                }
                .buttonStyle(.plain).help("New chat")
                .transition(.opacity)
            }
            if let chat = model.chat {
                AgentAvatar(agent: chat.agent, size: 24)
                VStack(alignment: .leading, spacing: 1) {
                    Text(chat.title).font(.system(size: 13, weight: .semibold)).lineLimit(1)
                    Text("\(chat.projectName) · \(agentLabel)").font(.system(size: 11)).foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }
            Spacer()
            if let chat = model.chat, chat.contextSize > 0 {
                ContextGauge(used: chat.contextUsed, size: chat.contextSize)
            }
        }
        .padding(.leading, sidebarShown ? 20 : 0)
        .padding(.trailing, 18)
        .frame(height: ChatStyle.headerHeight)
        .overlay(alignment: .bottom) { Rectangle().fill(VisualTokens.hairline).frame(height: 1).opacity(0.6) }
    }

    private var agentLabel: String {
        model.agents.first { $0.id == model.chat?.agent }?.label ?? (model.chat?.agent ?? "")
    }
}

private struct ContextGauge: View {
    let used: Int
    let size: Int

    var body: some View {
        let fraction = min(1, Double(used) / Double(max(size, 1)))
        HStack(spacing: 6) {
            ZStack {
                Circle().stroke(VisualTokens.hairline, lineWidth: 2.5)
                Circle().trim(from: 0, to: max(fraction, 0.02))
                    .stroke(fraction > 0.8 ? VisualTokens.warning : ChatStyle.accent,
                            style: StrokeStyle(lineWidth: 2.5, lineCap: .round))
                    .rotationEffect(.degrees(-90))
            }
            .frame(width: 14, height: 14)
            Text("\(Int((fraction * 100).rounded()))% context").font(.system(size: 11)).foregroundStyle(.secondary)
                .monospacedDigit()
        }
        .help("How much of the model's context window this chat uses: \(used.formatted()) of \(size.formatted()) tokens")
    }
}

// MARK: Welcome

private struct ChatWelcome: View {
    @ObservedObject var model: ChatModel
    let newChat: () -> Void

    var body: some View {
        VStack(spacing: VisualTokens.Space.m) {
            Image(nsImage: BrandArt.image("BrandMark")).resizable().interpolation(.high).scaledToFit()
                .frame(width: 56, height: 56)
            Text("Work on a project with your agents").font(.system(size: 22, weight: .semibold, design: .rounded))
            Text("Pick a folder and talk. Claude, Codex, OpenCode or Copilot work there with your own accounts and use your Mac through Mac MCP. They ask before they change anything.")
                .multilineTextAlignment(.center).foregroundStyle(.secondary).frame(maxWidth: 440)
            Button(action: newChat) {
                Text("Choose a Project Folder…").font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(ChatStyle.onAccent)
                    .padding(.horizontal, 18).padding(.vertical, 9)
                    .background(Capsule().fill(ChatStyle.accent))
            }
            .buttonStyle(.plain)
            if let error = model.error {
                Text(error).font(.callout).foregroundStyle(VisualTokens.error)
            }
        }
        .padding(VisualTokens.Space.xl)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

// MARK: Composer

struct ChatComposer: View {
    @ObservedObject var model: ChatModel
    @FocusState private var focused: Bool

    init(model: ChatModel) {
        self.model = model
    }

    var body: some View {
        VStack(spacing: 6) {
            if let error = model.error {
                Text(error).font(.caption).foregroundStyle(VisualTokens.error).frame(maxWidth: .infinity, alignment: .leading)
            }
            VStack(alignment: .leading, spacing: 10) {
                TextField(placeholder, text: $model.draft, axis: .vertical)
                    .textFieldStyle(.plain)
                    .font(.system(size: 14))
                    .lineLimit(1...10)
                    .focused($focused)
                    .onSubmit { model.send() }
                    .disabled(waiting)
                HStack(spacing: 6) {
                    ChatSettingsBar(model: model)
                    Spacer(minLength: 8)
                    sendOrStop
                }
            }
            .padding(.horizontal, 14).padding(.top, 12).padding(.bottom, 9)
            .background(RoundedRectangle(cornerRadius: 18, style: .continuous).fill(ChatStyle.raised))
            .overlay(
                RoundedRectangle(cornerRadius: 18, style: .continuous)
                    .strokeBorder(focused ? ChatStyle.accent.opacity(0.7) : VisualTokens.hairline, lineWidth: focused ? 1.5 : 1)
            )
            .shadow(color: .black.opacity(0.07), radius: 14, y: 4)
            .animation(.easeOut(duration: 0.15), value: focused)
        }
        .frame(maxWidth: ChatStyle.readingWidth + 40)
        .padding(.horizontal, VisualTokens.Space.l)
        .padding(.bottom, 18).padding(.top, 4)
        .onAppear { focused = true }
    }

    @ViewBuilder private var sendOrStop: some View {
        if running {
            Button(action: model.stop) {
                Image(systemName: "stop.fill").font(.system(size: 11, weight: .bold))
                    .frame(width: 30, height: 30)
                    .background(Circle().fill(Color.primary.opacity(0.85)))
                    .foregroundStyle(ChatStyle.canvas)
            }
            .buttonStyle(.plain).help("Stop (⌘.)").keyboardShortcut(".", modifiers: .command)
        } else {
            Button(action: model.send) {
                Image(systemName: "arrow.up").font(.system(size: 13, weight: .bold))
                    .frame(width: 30, height: 30)
                    .background(Circle().fill(canSend ? ChatStyle.accent : VisualTokens.hairline))
                    .foregroundStyle(canSend ? ChatStyle.onAccent : Color.secondary)
            }
            .buttonStyle(.plain).disabled(!canSend).help("Send (Return)")
            .animation(.easeOut(duration: 0.15), value: canSend)
        }
    }

    private var running: Bool { model.chat?.running == true }
    private var waiting: Bool { model.chat?.status == "waiting" }
    private var canSend: Bool { !running && !model.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
    private var placeholder: String { waiting ? "Answer the question above first" : "Ask for anything in this project" }
}
