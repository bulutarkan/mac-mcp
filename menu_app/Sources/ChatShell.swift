import AppKit
import SwiftUI
import UniformTypeIdentifiers

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
    @MacMCPState private var switching = false
    @MacMCPState private var sidebarShown: Bool

    init(model: ChatModel, sidebarShown: Bool = true) {
        self.model = model
        _sidebarShown = MacMCPState(initialValue: sidebarShown)
    }

    var body: some View {
        HStack(spacing: 0) {
            if sidebarShown {
                ChatSidebar(model: model, newChat: { showingNewChat = true }, switchChat: { switching = true },
                            hide: toggleSidebar)
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
            // On its own view: two sheets on one view do not both open on macOS 13.
            .sheet(isPresented: $switching) { ChatSwitcher(model: model) }
        }
        .background(ChatStyle.canvas)
        .ignoresSafeArea()
        .background(
            Group {
                Button("", action: toggleSidebar).keyboardShortcut("s", modifiers: [.control, .command])
                Button("") { switching = true }.keyboardShortcut("k", modifiers: .command)
            }
            .hidden()
        )
        .sheet(isPresented: $showingNewChat) { NewChatSheet(model: model) }
        .task {
            await model.refreshList()
            if model.selectedID == nil, let first = model.listedChats.first { model.select(first.id) }
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
    let switchChat: () -> Void
    let hide: () -> Void
    @FocusState private var searching: Bool
    @MacMCPState private var renaming: String?
    @MacMCPState private var deleting: ChatSummary?
    @MacMCPState private var showArchived = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 2) {
                Spacer()
                Button(action: switchChat) {
                    Image(systemName: "rectangle.stack").font(.system(size: 13)).foregroundStyle(.secondary)
                        .frame(width: 28, height: 24).contentShape(Rectangle())
                }
                .buttonStyle(.plain).help("Switch to a recent chat (⌘K)")
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

            HStack(spacing: 6) {
                Image(systemName: "magnifyingglass").font(.system(size: 11)).foregroundStyle(.tertiary)
                TextField("Search chats", text: $model.search).textFieldStyle(.plain).font(.system(size: 12.5))
                    .focused($searching)
                if !model.search.isEmpty {
                    Button { model.search = "" } label: {
                        Image(systemName: "xmark.circle.fill").font(.system(size: 11)).foregroundStyle(.tertiary)
                    }
                    .buttonStyle(.plain).help("Clear")
                }
            }
            .padding(.horizontal, 9).padding(.vertical, 6)
            .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(ChatStyle.selection.opacity(0.6)))
            .padding(.horizontal, 12).padding(.top, 8)
            .background(Button("", action: { searching = true }).keyboardShortcut("f", modifiers: .command).hidden())

            ScrollView {
                // A plain stack, as for the transcript: a lazy one changes height while scrolling.
                VStack(alignment: .leading, spacing: 0) {
                    let pinned = model.pinnedChats
                    if !pinned.isEmpty {
                        VStack(alignment: .leading, spacing: 2) {
                            SectionTitle(symbol: "pin.fill", title: "Pinned")
                            ForEach(pinned) { chat in
                                ChatListRow(chat: chat, model: model, renaming: $renaming, deleting: $deleting)
                                    .help(chat.projectName)
                            }
                        }
                        .padding(.top, 18)
                    }
                    ForEach(model.shownProjects) { project in
                        ProjectSection(project: project, model: model, renaming: $renaming, deleting: $deleting)
                    }
                    if pinned.isEmpty && model.shownProjects.isEmpty && model.archivedChats.isEmpty && !model.search.isEmpty {
                        Text("No chats match “\(model.search)”").font(.caption).foregroundStyle(.secondary)
                            .padding(.horizontal, 10).padding(.top, 18)
                    }
                    archivedSection
                }
                .padding(.horizontal, 10)
                .padding(.bottom, 16)
            }
            if let archived = model.lastArchived {
                // Undo for the chat just archived: it comes back where it was.
                HStack(spacing: 8) {
                    Image(systemName: "archivebox").font(.system(size: 11)).foregroundStyle(.secondary)
                    Text("Archived “\(archived.title)”").font(.system(size: 12)).lineLimit(1).truncationMode(.middle)
                    Spacer(minLength: 4)
                    Button("Undo") { model.unarchive(archived.id) }.buttonStyle(.link).font(.system(size: 12, weight: .medium))
                }
                .padding(.horizontal, 12).padding(.vertical, 9)
                .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(ChatStyle.raised))
                .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(VisualTokens.hairline))
                .padding(.horizontal, 10).padding(.bottom, 10)
                .transition(.opacity.combined(with: .move(edge: .bottom)))
            }
        }
        .animation(.easeOut(duration: 0.18), value: model.lastArchived?.id)
        .background(ChatStyle.sidebar)
        .overlay(alignment: .trailing) { Rectangle().fill(VisualTokens.hairline).frame(width: 1) }
        .alert("Delete “\(deleting?.title ?? "")”?", isPresented: Binding(get: { deleting != nil }, set: { if !$0 { deleting = nil } })) {
            Button("Delete", role: .destructive) {
                if let chat = deleting { model.delete(chat.id) }
                deleting = nil
            }
            Button("Cancel", role: .cancel) { deleting = nil }
        } message: {
            Text("The conversation and its history are removed from Mac MCP. The files the agent changed stay as they are.")
        }
    }

    /// Archived chats, folded away at the end of the list; open while searching, so a match shows.
    @ViewBuilder private var archivedSection: some View {
        let archived = model.archivedChats
        if !archived.isEmpty {
            let open = showArchived || !model.search.isEmpty
            VStack(alignment: .leading, spacing: 2) {
                Button { showArchived.toggle() } label: {
                    HStack(spacing: 8) {
                        Image(systemName: "archivebox").font(.system(size: 11)).foregroundStyle(.secondary).frame(width: 16)
                        Text("Archived").font(.system(size: 12, weight: .semibold)).foregroundStyle(.secondary)
                        Text("\(archived.count)").font(.system(size: 11)).foregroundStyle(.tertiary).monospacedDigit()
                        Image(systemName: "chevron.right").font(.system(size: 8, weight: .bold)).foregroundStyle(.tertiary)
                            .rotationEffect(.degrees(open ? 90 : 0))
                        Spacer()
                    }
                    .padding(.horizontal, 10).padding(.bottom, 6)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .help(open ? "Hide archived chats" : "Show archived chats")
                if open {
                    ForEach(archived) { chat in
                        HStack(spacing: 2) {
                            ChatRow(chat: chat, selected: chat.id == model.selectedID, dimmed: true) { model.select(chat.id) }
                            Button { model.unarchive(chat.id) } label: {
                                Image(systemName: "arrow.uturn.backward").font(.system(size: 11, weight: .medium))
                                    .foregroundStyle(.secondary).frame(width: 24, height: 24).contentShape(Rectangle())
                            }
                            .buttonStyle(.plain).help("Unarchive: back to the list")
                        }
                        .contextMenu {
                            Button("Unarchive") { model.unarchive(chat.id) }
                            Divider()
                            Button("Delete Chat…", role: .destructive) { deleting = chat }
                        }
                        .help(chat.projectName)
                    }
                }
            }
            .padding(.top, 22)
        }
    }
}

private struct SectionTitle: View {
    let symbol: String
    let title: String

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: symbol).font(.system(size: 11)).foregroundStyle(.secondary).frame(width: 16)
            Text(title).font(.system(size: 12, weight: .semibold)).foregroundStyle(.secondary).lineLimit(1)
        }
        .padding(.horizontal, 10)
        .padding(.bottom, 6)
    }
}

/// A chat in the list, or its name being edited; right-click pins, renames, archives or deletes it.
private struct ChatListRow: View {
    let chat: ChatSummary
    @ObservedObject var model: ChatModel
    @Binding var renaming: String?
    @Binding var deleting: ChatSummary?

    var body: some View {
        if renaming == chat.id {
            RenameField(title: chat.title) { title in
                if let title { model.rename(chat.id, to: title) }
                renaming = nil
            }
            .padding(.leading, 24)
        } else {
            ChatRow(chat: chat, selected: chat.id == model.selectedID) { model.select(chat.id) }
                .contextMenu {
                    Button(chat.pinned ? "Unpin" : "Pin") { model.pin(chat.id, !chat.pinned) }
                    Button("Rename…") { renaming = chat.id }
                    Button("Archive") { model.archive(chat) }
                    Divider()
                    Button("Delete Chat…", role: .destructive) { deleting = chat }
                }
        }
    }
}

/// A project and its chats. The chat titles line up with the folder's name, so the list reads as one tree.
private struct ProjectSection: View {
    let project: ChatProjectGroup
    @ObservedObject var model: ChatModel
    @Binding var renaming: String?
    @Binding var deleting: ChatSummary?

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
                ChatListRow(chat: chat, model: model, renaming: $renaming, deleting: $deleting)
            }
        }
        .padding(.top, 18)
    }
}

/// Editing a chat's name in place: Return keeps it, Escape or leaving the field puts the old one back.
struct RenameField: View {
    let title: String
    let done: (String?) -> Void
    @MacMCPState private var text: String
    @FocusState private var focused: Bool

    init(title: String, done: @escaping (String?) -> Void) {
        self.title = title
        self.done = done
        _text = MacMCPState(initialValue: title)
    }

    var body: some View {
        TextField("Chat name", text: $text)
            .textFieldStyle(.plain)
            .font(.system(size: 13, weight: .medium))
            .padding(.horizontal, 8).padding(.vertical, 5)
            .background(RoundedRectangle(cornerRadius: 7, style: .continuous).fill(ChatStyle.raised))
            .overlay(RoundedRectangle(cornerRadius: 7, style: .continuous).strokeBorder(ChatStyle.accent.opacity(0.7)))
            .focused($focused)
            .onSubmit { done(text.trimmingCharacters(in: .whitespaces).isEmpty || text == title ? nil : text) }
            .onExitCommand { done(nil) }
            .onAppear { focused = true }
            .onChange(of: focused) { now in if !now { done(nil) } }
    }
}

private struct ChatRow: View {
    let chat: ChatSummary
    let selected: Bool
    var dimmed = false
    let open: () -> Void

    var body: some View {
        Button(action: open) {
            HStack(spacing: 8) {
                status.frame(width: 16)
                Text(chat.title).font(.system(size: 13, weight: selected ? .medium : .regular))
                    .foregroundStyle(Color.primary.opacity(selected ? 1 : dimmed ? 0.6 : 0.85))
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
    @MacMCPState private var renaming = false

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
                    if renaming {
                        RenameField(title: chat.title) { title in
                            if let title { model.rename(chat.id, to: title) }
                            renaming = false
                        }
                        .frame(maxWidth: 360)
                    } else {
                        Text(chat.title).font(.system(size: 13, weight: .semibold)).lineLimit(1)
                            .onTapGesture(count: 2) { renaming = true }
                            .help("Double-click to rename")
                    }
                    HStack(spacing: 6) {
                        Text("\(chat.projectName) · \(agentLabel)")
                        if chat.isPrivate {
                            Label("Not saved", systemImage: "eye.slash")
                                .help("Private mode was on when this chat started: it is kept only until Mac MCP quits.")
                        }
                        if chat.archived {
                            Label("Archived", systemImage: "archivebox")
                            Button("Unarchive") { model.unarchive(chat.id) }.buttonStyle(.link)
                                .help("Back to the chat list")
                        }
                    }
                    .font(.system(size: 11)).foregroundStyle(.secondary)
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

// MARK: Quick switch

/// ⌘K: the recent chats across projects, newest first; type to narrow (archived ones too), ↑ ↓ and Return to open.
private struct ChatSwitcher: View {
    @ObservedObject var model: ChatModel
    @Environment(\.dismiss) private var dismiss
    @MacMCPState private var keys: Any?
    @FocusState private var focused: Bool

    var body: some View {
        let found = chats
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass").foregroundStyle(.tertiary)
                TextField("Switch to a chat", text: $model.switcherQuery).textFieldStyle(.plain).font(.system(size: 15))
                    .focused($focused)
                    .onSubmit { open(found) }
            }
            .padding(.horizontal, 14).padding(.vertical, 12)
            Divider()
            if found.isEmpty {
                Text(model.switcherQuery.isEmpty ? "No other chats yet" : "No chats match “\(model.switcherQuery)”")
                    .font(.callout).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ScrollViewReader { proxy in
                    ScrollView {
                        VStack(spacing: 2) {
                            ForEach(Array(found.enumerated()), id: \.element.id) { index, chat in
                                row(chat, highlighted: index == highlighted(found)) { open(found, at: index) }
                                    .id(chat.id)
                                    .onHover { inside in if inside { model.switcherIndex = index } }
                            }
                        }
                        .padding(6)
                    }
                    .onChange(of: model.switcherIndex) { index in
                        if found.indices.contains(index) { proxy.scrollTo(found[index].id) }
                    }
                }
            }
            HStack {
                Spacer()
                Text("↑↓ to choose · ↩ to open · esc to close").font(.system(size: 10)).foregroundStyle(.tertiary)
            }
            .padding(.horizontal, 12).padding(.vertical, 6)
            .background(ChatStyle.code.opacity(0.5))
        }
        .frame(width: 480, height: 400)
        .background(ChatStyle.canvas)
        .background(Button("") { dismiss() }.keyboardShortcut(.cancelAction).hidden())
        .onChange(of: model.switcherQuery) { _ in model.switcherIndex = 0 }
        .onAppear {
            model.switcherQuery = ""
            model.switcherIndex = 0
            focused = true
            installKeys()
        }
        .onDisappear {
            if let keys { NSEvent.removeMonitor(keys) }
            keys = nil
        }
    }

    private var chats: [ChatSummary] { model.switcherChats }

    private func highlighted(_ found: [ChatSummary]) -> Int { min(model.switcherIndex, max(found.count - 1, 0)) }

    private func open(_ found: [ChatSummary], at index: Int? = nil) {
        let index = index ?? highlighted(found)
        guard found.indices.contains(index) else { return }
        model.select(found[index].id)
        dismiss()
    }

    private func row(_ chat: ChatSummary, highlighted: Bool, open: @escaping () -> Void) -> some View {
        Button(action: open) {
            HStack(spacing: 10) {
                AgentAvatar(agent: chat.agent, size: 20)
                VStack(alignment: .leading, spacing: 1) {
                    Text(chat.title).font(.system(size: 13, weight: .medium)).lineLimit(1)
                    Text(chat.projectName).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(1)
                }
                Spacer(minLength: 6)
                if chat.pinned { Image(systemName: "pin.fill").font(.system(size: 10)).foregroundStyle(.tertiary) }
                if chat.archived { Image(systemName: "archivebox").font(.system(size: 10)).foregroundStyle(.tertiary) }
                Text(ChatTime.short(chat.updatedAt)).font(.system(size: 11)).foregroundStyle(.tertiary).monospacedDigit()
                if highlighted { Text("↩").font(.system(size: 11)).foregroundStyle(.tertiary) }
            }
            .padding(.horizontal, 10).padding(.vertical, 7)
            .background(RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(highlighted ? ChatStyle.selection : Color.clear))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(chat.title), \(chat.projectName)")
    }

    /// ↑ ↓ move through the list while the search field has the keys (a text field keeps them for itself).
    private func installKeys() {
        guard keys == nil else { return }
        let model = model
        keys = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { event in
            guard event.modifierFlags.intersection([.command, .control, .option]).isEmpty,
                  event.keyCode == 125 || event.keyCode == 126 else { return event }
            let count = model.switcherChats.count
            model.switcherIndex = ChatLibrary.move(model.switcherIndex, by: event.keyCode == 125 ? 1 : -1, count: count)
            return nil
        }
    }
}

// MARK: Composer

struct ChatComposer: View {
    @ObservedObject var model: ChatModel
    @FocusState private var focused: Bool
    @MacMCPState private var dropping = false
    @MacMCPState private var commandKeys: Any?

    init(model: ChatModel) {
        self.model = model
    }

    var body: some View {
        VStack(spacing: 6) {
            if model.link.state != .live {
                ConnectionNotice(state: model.link.state, retry: model.retryConnection)
            }
            if let error = model.error {
                Text(error).font(.caption).foregroundStyle(VisualTokens.error).frame(maxWidth: .infinity, alignment: .leading)
            }
            ForEach(model.failedSends.filter { $0.chatID == model.selectedID }) { failed in
                FailedMessage(failed: failed, tryAgain: { model.resend(failed) }, remove: { model.discard(failed) })
            }
            if !suggestions.isEmpty {
                CommandSuggestions(commands: suggestions, selected: model.commandIndex,
                                   hover: { model.commandIndex = $0 }) { complete($0) }
                    .transition(.opacity.combined(with: .move(edge: .bottom)))
            }
            if let queue = model.chat?.queue, !queue.isEmpty {
                VStack(spacing: 4) {
                    ForEach(queue) { entry in
                        QueuedMessage(entry: entry, sendNow: { model.queued(entry, "send_now") },
                                      remove: { model.queued(entry, "remove") })
                    }
                }
                .transition(.opacity.combined(with: .move(edge: .bottom)))
            }
            VStack(alignment: .leading, spacing: 10) {
                if !model.attachments.isEmpty {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 8) {
                            ForEach(model.attachments, id: \.self) { url in
                                PendingAttachment(url: url, note: imageNote(url)) { model.detach(url) }
                            }
                        }
                    }
                    .transition(.opacity)
                }
                TextField(placeholder, text: $model.draft, axis: .vertical)
                    .textFieldStyle(.plain)
                    .font(.system(size: 14))
                    .lineLimit(1...10)
                    .focused($focused)
                    .onSubmit {
                        // Return completes a command being typed; once it is complete, Return sends.
                        if !suggestions.isEmpty { model.acceptCommand() } else { model.send() }
                    }
                    .disabled(waiting)
                HStack(spacing: 6) {
                    Button(action: chooseFiles) {
                        Image(systemName: "paperclip").font(.system(size: 13, weight: .medium)).foregroundStyle(.secondary)
                            .frame(width: 26, height: 24).contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .help("Add files or images (or drop or paste them here)")
                    ChatSettingsBar(model: model)
                    Spacer(minLength: 8)
                    if focused && canSend && suggestions.isEmpty && !waiting {
                        // Only where it fits beside the agent's settings; it never pushes them.
                        ViewThatFits(in: .horizontal) {
                            Text(ChatLibrary.sendHint(running: running)).font(.system(size: 10.5))
                                .foregroundStyle(.tertiary).lineLimit(1).fixedSize()
                            Color.clear.frame(width: 0, height: 0)
                        }
                        .accessibilityLabel(running ? "Return sends when this answer ends, Option-Return starts a new line"
                                            : "Return sends, Option-Return starts a new line")
                    }
                    sendOrStop
                }
            }
            .padding(.horizontal, 14).padding(.top, 12).padding(.bottom, 9)
            .background(RoundedRectangle(cornerRadius: 18, style: .continuous).fill(ChatStyle.raised))
            .overlay(
                RoundedRectangle(cornerRadius: 18, style: .continuous)
                    .strokeBorder(dropping ? ChatStyle.accent : focused ? ChatStyle.accent.opacity(0.7) : VisualTokens.hairline,
                                  style: StrokeStyle(lineWidth: dropping ? 2 : focused ? 1.5 : 1, dash: dropping ? [6, 4] : []))
            )
            .onDrop(of: [UTType.fileURL], isTargeted: $dropping) { providers in
                for provider in providers {
                    _ = provider.loadObject(ofClass: URL.self) { url, _ in
                        if let url { DispatchQueue.main.async { model.attach([url]) } }
                    }
                }
                return !providers.isEmpty
            }
            .shadow(color: .black.opacity(0.07), radius: 14, y: 4)
            .animation(.easeOut(duration: 0.15), value: focused)
        }
        .frame(maxWidth: ChatStyle.readingWidth + 40)
        .padding(.horizontal, VisualTokens.Space.l)
        .padding(.bottom, 18).padding(.top, 4)
        .onAppear {
            focused = true
            installCommandKeys()
        }
        .onChange(of: focused) { model.composerFocused = $0 }
        .onChange(of: model.focusComposer) { _ in focused = true }
        .onDisappear {
            if let commandKeys { NSEvent.removeMonitor(commandKeys) }
            commandKeys = nil
        }
    }

    @ViewBuilder private var sendOrStop: some View {
        if running {
            if canSend {
                Button(action: model.send) {
                    Image(systemName: "arrow.up").font(.system(size: 13, weight: .bold))
                        .frame(width: 30, height: 30)
                        .background(Circle().fill(ChatStyle.accent))
                        .foregroundStyle(ChatStyle.onAccent)
                }
                .buttonStyle(.plain).help("Send when this answer ends (Return)")
                .transition(.scale.combined(with: .opacity))
            }
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

    /// The agent's commands matching what follows a leading "/" (until the first space).
    private var suggestions: [ChatCommand] { model.commandSuggestions }

    private func complete(_ command: ChatCommand) {
        model.acceptCommand(command)
        focused = true
    }

    /// ↑ ↓ move through the "/" list, Return and Tab take the highlighted command, Esc closes the list.
    /// A key monitor, since the text field keeps those keys for itself (and onKeyPress needs macOS 14).
    private func installCommandKeys() {
        guard commandKeys == nil else { return }
        let model = model
        commandKeys = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { event in
            // ⌘V with an image or files on the clipboard adds them to the message (the text field takes only text).
            if event.keyCode == 9, event.modifierFlags.intersection([.command, .control, .option, .shift]) == .command,
               event.window?.isKeyWindow == true, model.composerFocused, model.pasteFiles() {
                return nil
            }
            guard !model.commandSuggestions.isEmpty, event.window?.isKeyWindow == true,
                  event.window?.firstResponder is NSTextView,
                  event.modifierFlags.intersection([.command, .control, .option]).isEmpty else { return event }
            switch event.keyCode {
            case 125: model.moveCommandSelection(1)  // ↓
            case 126: model.moveCommandSelection(-1)  // ↑
            case 36, 76, 48: model.acceptCommand()  // Return, Enter, Tab
            case 53: model.dismissedCommandDraft = model.draft  // Esc
            default: return event
            }
            return nil
        }
    }

    private var running: Bool { model.chat?.running == true }
    private var waiting: Bool { model.chat?.status == "waiting" }
    private var canSend: Bool {
        !model.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !model.attachments.isEmpty
    }

    /// Said on an image that goes to the agent as a file link instead of a picture (its type, its size, or an
    /// agent that takes no pictures).
    private func imageNote(_ url: URL) -> String? {
        let size = (try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0
        let agent = model.agents.first { $0.id == model.chat?.agent }?.label ?? "This agent"
        return ChatImageNote.note(fileExtension: url.pathExtension, size: size, takesImages: model.chat?.takesImages,
                                  agent: agent)
    }

    private func chooseFiles() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = true
        panel.prompt = "Add"
        if let project = model.chat?.project { panel.directoryURL = URL(fileURLWithPath: project) }
        // Let SwiftUI's button/Accessibility update return before presenting AppKit UI.
        // A nested runModal loop keeps that update open for the lifetime of the picker.
        DispatchQueue.main.async {
            panel.begin { response in
                if response == .OK { model.attach(panel.urls) }
            }
        }
    }
    private var placeholder: String {
        if waiting { return "Answer the question above first" }
        return running ? "Write the next message; it goes out when this answer ends" : "Ask for anything in this project"
    }
}

/// A message waiting for the answer in progress to end: send it now (stops the answer) or take it back.
struct QueuedMessage: View {
    let entry: ChatQueued
    let sendNow: () -> Void
    let remove: () -> Void

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "clock").font(.system(size: 11, weight: .medium)).foregroundStyle(.tertiary)
            Text(entry.text.isEmpty ? entry.files.joined(separator: ", ") : entry.text)
                .font(.system(size: 12.5)).lineLimit(1).truncationMode(.tail)
            if !entry.files.isEmpty && !entry.text.isEmpty {
                Label("\(entry.files.count)", systemImage: "paperclip").font(.system(size: 11)).foregroundStyle(.secondary)
                    .labelStyle(.titleAndIcon)
            }
            Spacer(minLength: 6)
            Button(action: sendNow) {
                Text("Send Now").font(.system(size: 11.5, weight: .medium))
                    .padding(.horizontal, 8).padding(.vertical, 3)
                    .background(Capsule().fill(ChatStyle.accent.opacity(0.14)))
                    .foregroundStyle(ChatStyle.accent)
            }
            .buttonStyle(.plain).help("Stop the answer in progress and send this now")
            Button(action: remove) {
                Image(systemName: "xmark").font(.system(size: 9, weight: .bold)).foregroundStyle(.secondary)
                    .frame(width: 18, height: 18).contentShape(Rectangle())
            }
            .buttonStyle(.plain).help("Don't send")
        }
        .padding(.horizontal, 12).padding(.vertical, 7)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(ChatStyle.raised))
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(VisualTokens.hairline))
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Waiting to send: \(entry.text)")
    }
}

/// A message that did not go out and could not go back into the message box (the owner had typed since, or was
/// in another chat): send it again, copy it, or let it go.
struct FailedMessage: View {
    let failed: ChatFailedSend
    let tryAgain: () -> Void
    let remove: () -> Void

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "exclamationmark.circle").font(.system(size: 11, weight: .medium))
                .foregroundStyle(VisualTokens.error)
            Text("Not sent:").font(.system(size: 12.5, weight: .medium))
            Text(failed.text.isEmpty ? failed.files.map(\.lastPathComponent).joined(separator: ", ") : failed.text)
                .font(.system(size: 12.5)).lineLimit(1).truncationMode(.tail)
            Spacer(minLength: 6)
            Button(action: tryAgain) {
                Text("Try Again").font(.system(size: 11.5, weight: .medium))
                    .padding(.horizontal, 8).padding(.vertical, 3)
                    .background(Capsule().fill(ChatStyle.accent.opacity(0.14)))
                    .foregroundStyle(ChatStyle.accent)
            }
            .buttonStyle(.plain).help("Send this message again")
            if !failed.text.isEmpty {
                Button {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(failed.text, forType: .string)
                } label: {
                    Text("Copy").font(.system(size: 11.5, weight: .medium)).foregroundStyle(.secondary)
                }
                .buttonStyle(.plain).help("Copy the message")
            }
            Button(action: remove) {
                Image(systemName: "xmark").font(.system(size: 9, weight: .bold)).foregroundStyle(.secondary)
                    .frame(width: 18, height: 18).contentShape(Rectangle())
            }
            .buttonStyle(.plain).help("Discard")
        }
        .padding(.horizontal, 12).padding(.vertical, 7)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(ChatStyle.raised))
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(VisualTokens.error.opacity(0.45)))
        .help(failed.reason)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Not sent: \(failed.text)")
    }
}

/// The open chat stopped getting updates: "Reconnecting…" while the app tries again on its own, "Offline" with
/// Retry once several tries in a row found Mac MCP not answering. A still icon, no endless animation.
struct ConnectionNotice: View {
    let state: ChatConnection
    let retry: () -> Void

    var body: some View {
        HStack(spacing: 6) {
            if state == .offline {
                Image(systemName: "wifi.slash").foregroundStyle(VisualTokens.warning)
                Text("Offline").fontWeight(.medium)
                Text("Mac MCP isn't answering.").foregroundStyle(.secondary)
                Button("Retry", action: retry).buttonStyle(.link)
            } else {
                Image(systemName: "arrow.triangle.2.circlepath").foregroundStyle(.secondary)
                Text("Reconnecting…").foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
        }
        .font(.caption)
        .accessibilityElement(children: .contain)
    }
}

// MARK: Attachments

enum AttachmentThumbnails {
    private static let cache = NSCache<NSString, NSImage>()

    /// A small copy of an image file, made once.
    static func image(_ path: String, side: CGFloat) -> NSImage? {
        let key = "\(path)#\(Int(side))" as NSString
        if let cached = cache.object(forKey: key) { return cached }
        guard let image = NSImage(contentsOfFile: path), image.size.width > 0, image.size.height > 0 else { return nil }
        let scale = side / max(image.size.width, image.size.height)
        let size = NSSize(width: image.size.width * scale, height: image.size.height * scale)
        let thumbnail = NSImage(size: size, flipped: false) { rect in
            image.draw(in: rect)
            return true
        }
        cache.setObject(thumbnail, forKey: key)
        return thumbnail
    }

    static let imageExtensions: Set<String> = ["png", "jpg", "jpeg", "gif", "webp", "heic", "tiff"]

    /// A long file name keeps its start and its extension: "quarterly-report…final.pdf".
    static func shortName(_ name: String, limit: Int = 28) -> String {
        guard name.count > limit else { return name }
        let tail = 10
        return String(name.prefix(limit - tail - 1)) + "…" + String(name.suffix(tail))
    }
}

/// A file waiting to be sent: its picture or icon, its name, and a button to take it out. An image that will go
/// as a file link says so under its name.
private struct PendingAttachment: View {
    let url: URL
    let note: String?
    let remove: () -> Void

    var body: some View {
        HStack(spacing: 6) {
            preview.frame(width: 28, height: 28).clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
            VStack(alignment: .leading, spacing: 1) {
                Text(AttachmentThumbnails.shortName(url.lastPathComponent)).font(.system(size: 12)).lineLimit(1)
                if let note {
                    Text(note).font(.system(size: 10.5)).foregroundStyle(VisualTokens.warning).lineLimit(1)
                }
            }
            .help(note ?? url.path)
            Button(action: remove) {
                Image(systemName: "xmark").font(.system(size: 9, weight: .bold)).foregroundStyle(.secondary)
                    .frame(width: 16, height: 16).background(Circle().fill(VisualTokens.quietFill))
            }
            .buttonStyle(.plain)
            .help("Remove")
        }
        .padding(.leading, 4).padding(.trailing, 6).padding(.vertical, 4)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(ChatStyle.code))
    }

    @ViewBuilder private var preview: some View {
        if AttachmentThumbnails.imageExtensions.contains(url.pathExtension.lowercased()),
           let image = AttachmentThumbnails.image(url.path, side: 56) {
            Image(nsImage: image).resizable().scaledToFill()
        } else {
            Image(nsImage: NSWorkspace.shared.icon(forFile: url.path)).resizable().scaledToFit()
        }
    }
}

/// What a sent message carried: images as pictures (click to open), other files as named chips.
struct SentAttachments: View {
    let files: [ChatAttachment]

    var body: some View {
        HStack(spacing: 8) {
            ForEach(files) { file in
                Button { open(file) } label: {
                    if file.isImage, let image = AttachmentThumbnails.image(file.path, side: 240) {
                        Image(nsImage: image).resizable().scaledToFit()
                            .frame(maxWidth: 160, maxHeight: 120)
                            .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                            .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(VisualTokens.hairline))
                    } else {
                        HStack(spacing: 6) {
                            Image(nsImage: NSWorkspace.shared.icon(forFile: file.path)).resizable().frame(width: 20, height: 20)
                            Text(AttachmentThumbnails.shortName(file.name)).font(.system(size: 12)).lineLimit(1)
                        }
                        .padding(.horizontal, 10).padding(.vertical, 7)
                        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(ChatStyle.userBubble))
                        .fixedSize()
                    }
                }
                .buttonStyle(.plain)
                .help(file.path)
            }
        }
    }

    private func open(_ file: ChatAttachment) {
        let url = URL(fileURLWithPath: file.path)
        if file.isImage {
            NSWorkspace.shared.open(url)
        } else {
            NSWorkspace.shared.activateFileViewerSelecting([url])
        }
    }
}

/// The agent's commands for what is being typed after "/": name, what it does, and what it expects after it.
private struct CommandSuggestions: View {
    let commands: [ChatCommand]
    let selected: Int
    let hover: (Int) -> Void
    let pick: (ChatCommand) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(commands.enumerated()), id: \.element.id) { index, command in
                Button { pick(command) } label: {
                    HStack(alignment: .firstTextBaseline, spacing: 10) {
                        Text("/" + command.name).font(.system(size: 13, weight: .semibold, design: .monospaced))
                            .foregroundStyle(index == selected ? ChatStyle.accent : Color.primary)
                        Text(command.description).font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(1)
                        Spacer(minLength: 8)
                        if !command.hint.isEmpty {
                            Text(command.hint).font(.system(size: 11)).foregroundStyle(.tertiary).lineLimit(1)
                        }
                        if index == selected {
                            Text("↩").font(.system(size: 11)).foregroundStyle(.tertiary)
                        }
                    }
                    .padding(.horizontal, 12).padding(.vertical, 7)
                    .background(index == selected ? ChatStyle.selection.opacity(0.7) : Color.clear)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .onHover { inside in if inside { hover(index) } }
            }
            HStack {
                Spacer()
                Text("↑↓ to choose · ↩ or ⇥ to use · esc to close").font(.system(size: 10)).foregroundStyle(.tertiary)
            }
            .padding(.horizontal, 12).padding(.vertical, 5)
            .background(ChatStyle.code.opacity(0.5))
        }
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(ChatStyle.raised))
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(VisualTokens.hairline))
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
        .shadow(color: .black.opacity(0.08), radius: 10, y: 3)
    }
}
