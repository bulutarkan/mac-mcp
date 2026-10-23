import AppKit
import Combine
import SwiftUI

/// The live-activity island (#174), one of the two live-activity styles (the other is the tool
/// activity bubble); off unless chosen in Settings › Activity & Notifications.
///
/// It rests as two small wings beside the notch: the Mac MCP mark on the left, an empty slot on
/// the right. Work shows only in that slot (the native icon of what is being used, with a ring,
/// then a tick) so ordinary tool calls never move the shape. Events that matter (an agent or a
/// scheduled run ending, something needing the owner) slide a row down for a few seconds.
/// Hovering opens the full panel: what needs you, what runs now, recent work, scheduled tasks.

// MARK: - Geometry and shape

struct NotchGeometry: Equatable {
    let width: CGFloat
    let height: CGFloat
    let hasNotch: Bool

    static func detect(_ screen: NSScreen?) -> NotchGeometry {
        guard let screen else { return NotchGeometry(width: 190, height: 32, hasNotch: false) }
        let top = screen.safeAreaInsets.top
        if top > 0, let left = screen.auxiliaryTopLeftArea?.width, let right = screen.auxiliaryTopRightArea?.width,
           left > 0, right > 0 {
            return NotchGeometry(width: screen.frame.width - left - right, height: top, hasNotch: true)
        }
        let menuBar = screen.frame.maxY - screen.visibleFrame.maxY - 1
        return NotchGeometry(width: 120, height: menuBar > 0 ? menuBar : max(24, NSStatusBar.system.thickness), hasNotch: false)
    }

    static func targetScreen() -> NSScreen? {
        NSScreen.screens.first(where: { $0.safeAreaInsets.top > 0 }) ?? NSScreen.main
    }
}

/// Flat top flush with the screen edge, small flares into the menu bar and soft bottom corners,
/// so the island reads as the notch itself. Width, height and radius all animate together.
struct IslandSilhouette: Shape {
    var bottomRadius: CGFloat
    var flare: CGFloat = 6

    var animatableData: CGFloat {
        get { bottomRadius }
        set { bottomRadius = newValue }
    }

    func path(in rect: CGRect) -> Path {
        var path = Path()
        let r = min(bottomRadius, rect.height / 2, rect.width / 2)
        let f = flare
        path.move(to: CGPoint(x: rect.minX - f, y: rect.minY))
        path.addLine(to: CGPoint(x: rect.maxX + f, y: rect.minY))
        path.addQuadCurve(to: CGPoint(x: rect.maxX, y: rect.minY + f), control: CGPoint(x: rect.maxX, y: rect.minY))
        path.addLine(to: CGPoint(x: rect.maxX, y: rect.maxY - r))
        path.addCurve(to: CGPoint(x: rect.maxX - r, y: rect.maxY),
                      control1: CGPoint(x: rect.maxX, y: rect.maxY - r * 0.45),
                      control2: CGPoint(x: rect.maxX - r * 0.45, y: rect.maxY))
        path.addLine(to: CGPoint(x: rect.minX + r, y: rect.maxY))
        path.addCurve(to: CGPoint(x: rect.minX, y: rect.maxY - r),
                      control1: CGPoint(x: rect.minX + r * 0.45, y: rect.maxY),
                      control2: CGPoint(x: rect.minX, y: rect.maxY - r * 0.45))
        path.addLine(to: CGPoint(x: rect.minX, y: rect.minY + f))
        path.addQuadCurve(to: CGPoint(x: rect.minX - f, y: rect.minY), control: CGPoint(x: rect.minX, y: rect.minY))
        path.closeSubpath()
        return path
    }
}

/// The island's motion, from the app-wide system (AppMotion).
enum IslandMotion {
    static var reduceMotion: Bool { AppMotion.reduceMotion }
    static var open: Animation { AppMotion.islandOpen }
    static var close: Animation { AppMotion.islandClose }
    static var slot: Animation { AppMotion.spatial }
}

extension LiveTone {
    var color: Color {
        switch self {
        case .working: return VisualTokens.OnDark.accent
        case .done: return VisualTokens.OnDark.success
        case .attention: return VisualTokens.OnDark.warning
        case .problem: return VisualTokens.OnDark.error
        case .notice: return Color(red: 0.58, green: 0.68, blue: 0.82)
        }
    }
}

// MARK: - Icons for what is being used

/// The native icon for a tool's subject: Safari or Chrome for the browser, Terminal for commands,
/// Finder for files, the app itself for app tools; an SF Symbol where no app fits.
enum IslandIcon: Hashable {
    case app(String)      // bundle identifier or app name
    case symbol(String)
    case provider(String) // a maker's logo from Onboarding/logos: claude, openai, opencode, kimi, grok, …

    /// The logo of the agent's maker; an unknown provider keeps the generic agent symbol.
    static func forProvider(_ provider: String?) -> IslandIcon {
        let id = (provider ?? "").lowercased()
        if id == "claude" { return .provider("claude") }
        if id == "codex" || id.hasPrefix("chatgpt") { return .provider("openai") }
        // The other agents' logos are named after their provider id.
        if AgentProviders.names[id] != nil { return .provider(id) }
        return .symbol("sparkles")
    }

    private static var logos: [String: NSImage] = [:]

    static func logo(_ name: String) -> NSImage? {
        if let cached = logos[name] { return cached }
        guard let url = Bundle.main.url(forResource: name, withExtension: "png", subdirectory: "Onboarding/logos"),
              let image = NSImage(contentsOf: url) else { return nil }
        logos[name] = image
        return image
    }

    static func forTool(_ tool: String, target: String?) -> IslandIcon {
        let target = (target ?? "").trimmingCharacters(in: .whitespaces)
        if tool.hasPrefix("browser") || tool == "open_url" {
            return .app(target.lowercased() == "chrome" ? "com.google.Chrome" : "com.apple.Safari")
        }
        if tool.contains("command") || tool.contains("job") || tool == "run_shell" { return .app("com.apple.Terminal") }
        if tool.contains("file") || tool.contains("director") || tool == "spotlight_search" {
            return .app("com.apple.finder")
        }
        if (tool.hasPrefix("mac_") || tool == "open_app" || tool == "screenshot"), !target.isEmpty { return .app(target) }
        if tool.hasPrefix("mac_") || tool == "screenshot" || tool == "computer_plan" { return .symbol("macwindow") }
        if tool.contains("agent") || tool.contains("team") { return .symbol("sparkles") }
        if tool.hasPrefix("memory") || tool.hasPrefix("lesson") { return .symbol("brain") }
        if tool == "http_request" { return .symbol("globe") }
        if tool == "scheduled_tasks" { return .symbol("calendar.badge.clock") }
        if tool.hasPrefix("ask") { return .symbol("questionmark.bubble") }
        if tool.contains("voice") { return .symbol("waveform") }
        return .symbol("wrench.and.screwdriver")
    }

    private static var cache: [String: NSImage] = [:]

    static func image(for key: String) -> NSImage? {
        if let cached = cache[key] { return cached }
        let workspace = NSWorkspace.shared
        var url = workspace.urlForApplication(withBundleIdentifier: key)
        if url == nil {
            for folder in ["/Applications", "/System/Applications", "/System/Applications/Utilities", "/Applications/Utilities"] {
                let candidate = URL(fileURLWithPath: "\(folder)/\(key).app")
                if FileManager.default.fileExists(atPath: candidate.path) { url = candidate; break }
            }
        }
        if url == nil {
            // An app named by its executable or display name (MacMCPMenu, "Mac MCP") is found
            // among the running apps instead of showing a placeholder.
            let wanted = key.lowercased()
            if let running = workspace.runningApplications.first(where: { app in
                [app.localizedName, app.bundleIdentifier, app.executableURL?.lastPathComponent].contains { $0?.lowercased() == wanted }
            }), let icon = running.icon {
                cache[key] = icon
                return icon
            }
        }
        guard let url else { return nil }
        let image = workspace.icon(forFile: url.path)
        cache[key] = image
        return image
    }
}

private struct IconTile: View {
    let icon: IslandIcon
    var size: CGFloat = 18
    var tint: Color = .white

    var body: some View {
        switch icon {
        case .app(let key):
            if let image = IslandIcon.image(for: key) {
                Image(nsImage: image).resizable().interpolation(.high).frame(width: size, height: size)
            } else {
                symbol("app.dashed")
            }
        case .symbol(let name):
            symbol(name)
        case .provider(let name):
            if let image = IslandIcon.logo(name) {
                Image(nsImage: image).resizable().interpolation(.high).frame(width: size, height: size)
                    .clipShape(RoundedRectangle(cornerRadius: size * 0.24, style: .continuous))
            } else {
                symbol("sparkles")
            }
        }
    }

    private func symbol(_ name: String) -> some View {
        RoundedRectangle(cornerRadius: size * 0.26, style: .continuous)
            .fill(Color.white.opacity(0.12))
            .frame(width: size, height: size)
            .overlay(Image(systemName: name).font(.system(size: size * 0.52, weight: .semibold)).foregroundStyle(tint))
    }
}

// MARK: - Model

@MainActor
final class LiveIslandModel: ObservableObject {
    enum Mode: Int { case rest = 0, peek, expanded }

    /// What the right wing shows.
    enum Slot: Equatable {
        case empty
        case working(IslandIcon, count: Int)
        case finished(ok: Bool)
        case waiting(count: Int)
    }

    @Published private(set) var mode: Mode = .rest
    @Published var notch: NotchGeometry
    @Published var slot: Slot = .empty
    @Published var now: [LiveItem] = []
    @Published var needsYou: [LiveItem] = []
    @Published var recent: [LiveHistoryEntry] = []
    @Published var scheduled: [ScheduledTask] = []
    @Published var peek: LivePeek?
    @Published var privateMode = false
    @Published var observationPaused = false
    @Published var contentVisible = false
    @Published var expandedHeight: CGFloat = 220
    @Published var working = false
    var hovering = false
    /// A question to the owner has grown out of the island (OwnerPrompt): stay at rest beneath it.
    var promptCovering = false

    init(notch: NotchGeometry) { self.notch = notch }

    /// Each wing holds one icon; the same width at rest and while working, so work never moves the shape.
    var wing: CGFloat { notch.hasNotch ? 40 : 34 }

    var size: CGSize {
        let rest = CGSize(width: notch.width + wing * 2, height: notch.height)
        switch mode {
        case .rest: return rest
        case .peek: return CGSize(width: max(rest.width, 420), height: notch.height + 54)
        case .expanded: return CGSize(width: max(rest.width, 580), height: notch.height + expandedHeight)
        }
    }

    func go(_ new: Mode) {
        guard new != mode else { return }
        let opening = new.rawValue > mode.rawValue
        if opening {
            withAnimation(IslandMotion.open) { mode = new }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.12) { [weak self] in
                guard let self, self.mode == new else { return }
                withAnimation(.easeOut(duration: 0.2)) { self.contentVisible = true }
            }
        } else {
            // Content leaves first, then the shape settles.
            withAnimation(.easeIn(duration: 0.1)) { contentVisible = false }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.08) { [weak self] in
                guard let self else { return }
                withAnimation(IslandMotion.close) { self.mode = new }
                if new == .peek {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.12) {
                        guard self.mode == .peek else { return }
                        withAnimation(.easeOut(duration: 0.2)) { self.contentVisible = true }
                    }
                }
            }
        }
    }

    func settle() {
        if promptCovering { go(.rest) } else if hovering { go(.expanded) } else if peek != nil { go(.peek) } else { go(.rest) }
    }
}

/// Island data needs quick refreshes around real work, but its idle card does
/// not need to ask the server twenty times a minute.  After one quiet minute we
/// poll every 30 seconds; after five quiet minutes, every 60 seconds.
private enum LiveIslandPolling {
    static let fastIntervalSeconds: TimeInterval = 6
    static let recentlyActiveWindowSeconds: TimeInterval = 60
    static let idleIntervalSeconds: TimeInterval = 30
    static let deepIdleAfterSeconds: TimeInterval = 5 * 60
    static let deepIdleIntervalSeconds: TimeInterval = 60

    static func interval(workIsActive: Bool, lastWorkAt: Date, now: Date = Date()) -> TimeInterval {
        if workIsActive || now.timeIntervalSince(lastWorkAt) < recentlyActiveWindowSeconds {
            return fastIntervalSeconds
        }
        let idleFor = now.timeIntervalSince(lastWorkAt)
        return idleFor < deepIdleAfterSeconds ? idleIntervalSeconds : deepIdleIntervalSeconds
    }
}

// MARK: - Controller

@MainActor
final class LiveIslandController {
    static let shared = LiveIslandController()

    private weak var state: AppState?
    private let model = LiveIslandModel(notch: NotchGeometry.detect(NotchGeometry.targetScreen()))
    private var window: IslandPanel?
    private var subscriptions: Set<AnyCancellable> = []
    private var tools: [String: LiveToolCall] = [:]
    private var agentStatus: [String: String] = [:]
    private var agentsSeeded = false
    private var peekQueue: [LivePeek] = []
    /// Sessions already told that iPhone steps borrow the pointer and keyboard.
    private var iPhoneNoticeShown = Set<String>()
    private var peekTask: Task<Void, Never>?
    private var finishTask: Task<Void, Never>?
    private var pollTask: Task<Void, Never>?
    private var pollSleepTask: Task<Void, Never>?
    private var pollSleepID: UUID?
    private var hoverTask: Task<Void, Never>?
    private var mouseMonitors: [Any] = []
    private var observers: [NSObjectProtocol] = []
    private var shapeChanges: AnyCancellable?
    private var locked = false
    private var serverWasRunning = false
    private var attention: [LiveItem] = []
    private var reviews: [LiveItem] = []
    private var runningTasks: [LiveItem] = []
    private var knownAttention: Set<String> = []
    private var attentionSeeded = false
    private var historySeeded = false
    private var lastWorkAt = Date()
    private var activeAgentIDs: Set<String> = []
    private var scheduledRunningTaskIDs: Set<String> = []
    /// Keep the display timestamp stable between island polls.  A fresh
    /// `Date()` estimate would make an otherwise identical row unequal.
    private var agentStartedAt: [String: Date] = [:]
    /// The latest agents and server state as they were published. @Published emits before it
    /// stores the new value, so reading `state.agents` inside the sink still gave the old list:
    /// a finished agent stayed under Now, its timer running, until something else changed.
    private var agentsSnapshot: [AgentInfo] = []
    private var serverRunningNow = false

    var enabled: Bool { state?.settings.showLiveIsland == true }

    func attach(_ state: AppState) {
        self.state = state
        state.onLiveToolCallStarted = { [weak self] call in self?.toolStarted(call) }
        state.onLiveToolCallFinished = { [weak self] id, ok in self?.toolFinished(id, ok: ok) }
        state.onScheduledRunFinished = { [weak self] run, name in self?.scheduledRunFinished(run, name: name) }
        state.$agents.sink { [weak self] agents in self?.agentsChanged(agents) }.store(in: &subscriptions)
        state.$privacyFlags.sink { [weak self] flags in
            self?.model.privateMode = flags?.privateMode == true
            self?.model.observationPaused = flags?.paused == true
        }.store(in: &subscriptions)
        state.$serverRunning.removeDuplicates().sink { [weak self] running in
            self?.serverRunningNow = running
            if running { self?.serverWasRunning = true }
            self?.rebuild()
        }.store(in: &subscriptions)
        state.settings.$showLiveIsland.removeDuplicates().sink { [weak self] on in
            self?.enabledChanged(on)
        }.store(in: &subscriptions)
        observeSystem()
    }

    /// A short sample from Settings: work in the slot, a tick, then a finished-task row.
    func preview() {
        guard enabled else { return }
        showWindow()
        withAnimation(IslandMotion.slot) { model.slot = .working(.app("com.apple.Safari"), count: 1) }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.6) { [weak self] in
            guard let self else { return }
            withAnimation(IslandMotion.slot) { self.model.slot = .finished(ok: true) }
            self.enqueuePeek(LivePeek(id: "preview", symbol: "checkmark", title: "Weekly ad report finished",
                                      detail: "Scheduled task · Codex · 36 s", tone: .done))
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.4) { self.refreshSlot() }
        }
    }

    // MARK: Inputs

    private func toolStarted(_ call: LiveToolCall) {
        // A stream event can arrive during a 30/60 s idle sleep.  It is the
        // signal to restore the old fast cadence immediately.
        recordWorkActivity(wake: true)
        finishTask?.cancel()
        finishTask = nil
        tools[call.eventID] = call
        rebuild()
        refreshSlot()
        // The first time a session drives the iPhone, say that each step borrows the pointer and keyboard.
        if let session = IPhoneNotice.sessionKey(for: call), iPhoneNoticeShown.insert(session).inserted {
            enqueuePeek(LivePeek(id: "iphone-notice-\(session)", symbol: "iphone", title: IPhoneNotice.title,
                                 detail: IPhoneNotice.detail, tone: .notice))
        }
    }

    private func toolFinished(_ eventID: String, ok: Bool) {
        guard let call = tools.removeValue(forKey: eventID) else { return }
        recordWorkActivity(wake: true)
        remember(LiveHistoryEntry(id: eventID, tool: call.tool, title: call.description, client: call.client,
                                  target: call.target, at: Date(), ok: ok))
        rebuild()
        guard tools.isEmpty else {
            refreshSlot()
            return
        }
        // A tick only when work actually stops, not between back-to-back calls.
        finishTask?.cancel()
        finishTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 600_000_000)
            guard !Task.isCancelled, let self, self.tools.isEmpty else { return }
            withAnimation(IslandMotion.slot) { self.model.slot = .finished(ok: ok) }
            try? await Task.sleep(nanoseconds: 1_400_000_000)
            guard !Task.isCancelled else { return }
            self.finishTask = nil
            self.refreshSlot()
        }
    }

    private func agentsChanged(_ agents: [AgentInfo]) {
        agentsSnapshot = agents
        let nextActiveAgentIDs = Set(agents.filter(\.isActive).map(\.agentID))
        let activeAgentsChanged = nextActiveAgentIDs != activeAgentIDs
        let startedAgents = !nextActiveAgentIDs.subtracting(activeAgentIDs).isEmpty
        activeAgentIDs = nextActiveAgentIDs
        agentStartedAt = agentStartedAt.filter { nextActiveAgentIDs.contains($0.key) }
        for agent in agents where agent.isActive && agentStartedAt[agent.agentID] == nil {
            agentStartedAt[agent.agentID] = agent.durationMS.map { Date().addingTimeInterval(-Double($0) / 1000) } ?? Date()
        }
        if !nextActiveAgentIDs.isEmpty {
            recordWorkActivity(wake: startedAgents)
        } else if activeAgentsChanged {
            recordWorkActivity(wake: true)
        }
        defer { rebuild(); refreshSlot() }
        guard agentsSeeded else {
            agentStatus = Dictionary(agents.map { ($0.agentID, $0.status ?? "") }, uniquingKeysWith: { a, _ in a })
            agentsSeeded = true
            return
        }
        for agent in agents {
            let previous = agentStatus[agent.agentID]
            let current = agent.status ?? ""
            agentStatus[agent.agentID] = current
            guard previous != current, !Self.isScheduled(agent) else { continue }  // scheduled runs announce themselves
            if !agent.isActive, previous.map({ $0 == "running" || $0 == "starting" }) == true {
                let ok = current == "completed"
                let name = Self.agentName(agent)
                let detail = [Self.providerName(agent.provider), agent.durationMS.map { Self.duration(Double($0) / 1000) }]
                    .compactMap { $0 }.joined(separator: " · ")
                enqueuePeek(LivePeek(id: "agent-end-\(agent.agentID)", symbol: ok ? "checkmark" : "xmark",
                                     title: ok ? "\(name) finished" : "\(name) \(current == "cancelled" ? "was stopped" : "failed")",
                                     detail: detail, tone: ok ? .done : .problem, target: .agent(agent.agentID)))
                remember(LiveHistoryEntry(id: "agent-\(agent.agentID)", tool: "spawn_agent", title: name,
                                          client: Self.providerName(agent.provider), target: nil, at: Date(), ok: ok))
            }
        }
    }

    private func scheduledRunFinished(_ run: TaskRun, name: String) {
        recordWorkActivity()
        let tone: LiveTone = run.status == "done" ? .done : run.status == "skipped" ? .attention : .problem
        let title: String
        switch run.status {
        case "done": title = "\(name) finished"
        case "skipped": title = "\(name) was skipped"
        case "timed_out": title = "\(name) stopped at its time limit"
        default: title = "\(name) failed"
        }
        let detail = run.status == "done"
            ? "Scheduled task" + (run.duration.map { " · \(Self.duration($0))" } ?? "")
            : (run.reason ?? "Scheduled task")
        enqueuePeek(LivePeek(id: "run-\(run.id)", symbol: tone == .done ? "checkmark" : tone == .problem ? "xmark" : "forward.end.fill",
                             title: title, detail: detail, tone: tone, target: .settings("scheduled-tasks")))
        remember(LiveHistoryEntry(id: "run-\(run.id)", tool: "scheduled_tasks", title: name, client: "Scheduled task",
                                  target: nil, at: Date(), ok: run.status == "done"))
        Task { await pollOnce() }
    }

    private func enabledChanged(_ on: Bool) {
        if on {
            model.notch = NotchGeometry.detect(NotchGeometry.targetScreen())
            showWindow()
            startPolling()
        } else {
            pollTask?.cancel()
            pollTask = nil
            pollSleepTask?.cancel()
            pollSleepTask = nil
            pollSleepID = nil
            peekQueue.removeAll()
            model.peek = nil
            hideWindow()
        }
    }

    // MARK: Polling

    private func startPolling() {
        pollTask?.cancel()
        pollSleepTask?.cancel()
        lastWorkAt = Date()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                await self.pollOnce()
                guard !Task.isCancelled else { return }
                await self.waitForNextPoll()
            }
        }
    }

    private func recordWorkActivity(wake: Bool = false) {
        lastWorkAt = Date()
        if wake { wakePolling() }
    }

    private func wakePolling() {
        pollSleepTask?.cancel()
    }

    private var islandWorkIsActive: Bool {
        !tools.isEmpty || !activeAgentIDs.isEmpty || !scheduledRunningTaskIDs.isEmpty
    }

    private func waitForNextPoll() async {
        let sleepID = UUID()
        let delay = LiveIslandPolling.interval(workIsActive: islandWorkIsActive, lastWorkAt: lastWorkAt)
        let sleeper = Task<Void, Never> {
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
        }
        pollSleepID = sleepID
        pollSleepTask = sleeper
        await sleeper.value
        if pollSleepID == sleepID {
            pollSleepTask = nil
            pollSleepID = nil
        }
    }

    private func pollHistoryIfNeeded(_ state: AppState) async -> (ok: Bool, object: [String: Any]) {
        guard !historySeeded else { return (ok: false, object: [:]) }
        return await state.taskAPI("dashboard/api/events?limit=40")
    }

    private func pollOnce() async {
        guard enabled, let state else { return }
        // Self-heal: the island is always on screen while its style is on.
        if !locked, !(window?.isVisible ?? false) || (window?.alphaValue ?? 0) < 0.99 { showWindow() }
        async let attentionFetch = state.taskAPI("dashboard/api/attention")
        async let tasksFetch = state.taskAPI("dashboard/api/scheduled-tasks?view=island")
        async let historyFetch = pollHistoryIfNeeded(state)
        let attentionResult = await attentionFetch
        let tasksResult = await tasksFetch
        let historyResult = await historyFetch
        var islandContentChanged = false
        if attentionResult.ok {
            let nextAttention = (attentionResult.object["pending"] as? [[String: Any]] ?? []).map { row in
                let tool = row["tool"] as? String ?? ""
                let origin = (row["origin"] as? String).flatMap { $0.isEmpty ? nil : $0 }
                return LiveItem(id: "approval-\(row["request_id"] as? String ?? UUID().uuidString)", symbol: "hand.raised.fill",
                                title: "Waiting for your answer", detail: [Self.toolWords(tool), origin].compactMap { $0 }.joined(separator: " · "),
                                tone: .attention, startedAt: nil, target: nil, actionTitle: nil)
            }
            if nextAttention != attention {
                attention = nextAttention
                islandContentChanged = true
            }
        }
        if tasksResult.ok {
            let tasks = (tasksResult.object["tasks"] as? [[String: Any]] ?? []).map(ScheduledTask.init)
            let nextReviews = tasks.filter { $0.proposal != nil }.map { task in
                let by = task.proposal?["proposed_by"] as? String ?? "An AI app"
                return LiveItem(id: "review-\(task.id)", symbol: "text.badge.checkmark",
                                title: task.proposal?["name"] as? String ?? task.name,
                                detail: task.approved ? "\(by) suggested a change" : "\(by) suggested this task",
                                tone: .attention, startedAt: nil, target: .settings("scheduled-tasks"), actionTitle: "Review")
            }
            let nextRunningTasks = tasks.filter { $0.state == "running" && !$0.isAI }.map { task in
                LiveItem(id: "task-\(task.id)", symbol: "chevron.left.forwardslash.chevron.right", title: task.name,
                         detail: "Scheduled script", tone: .working, startedAt: task.lastRun?.startedAt,
                         target: .settings("scheduled-tasks"))
            }
            let nextScheduled = tasks.filter { $0.approved }.sorted {
                ($0.nextRunAt ?? .distantFuture) < ($1.nextRunAt ?? .distantFuture)
            }
            let nextScheduledRunningTaskIDs = Set(tasks.filter { $0.state == "running" }.map(\.id))
            if nextReviews != reviews {
                reviews = nextReviews
                islandContentChanged = true
            }
            if nextRunningTasks != runningTasks {
                runningTasks = nextRunningTasks
                islandContentChanged = true
            }
            if nextScheduled != model.scheduled { model.scheduled = nextScheduled }
            if nextScheduledRunningTaskIDs != scheduledRunningTaskIDs {
                scheduledRunningTaskIDs = nextScheduledRunningTaskIDs
            }
            if !nextScheduledRunningTaskIDs.isEmpty { recordWorkActivity() }
        }
        if historyResult.ok {
            historySeeded = true
            let seeded = (historyResult.object["events"] as? [[String: Any]] ?? []).compactMap { event -> LiveHistoryEntry? in
                let arguments = event["arguments"] as? [String: Any] ?? [:]
                guard let text = arguments["description"] as? String, !text.isEmpty,
                      let id = event["event_id"] as? String else { return nil }
                let status = event["status"] as? String ?? "ok"
                return LiveHistoryEntry(id: id, tool: event["tool"] as? String ?? "", title: text, client: nil,
                                        target: (arguments["browser"] as? String) ?? (arguments["app"] as? String),
                                        at: Date(timeIntervalSince1970: (event["ended_at"] as? Double) ?? (event["timestamp"] as? Double) ?? 0),
                                        ok: !["error", "failed", "denied", "blocked", "timeout"].contains(status))
            }
            let known = Set(model.recent.map(\.id))
            let nextRecent = Array((model.recent + seeded.filter { !known.contains($0.id) }).sorted { $0.at > $1.at }.prefix(20))
            if nextRecent != model.recent { model.recent = nextRecent }
        }
        let waiting = attention + reviews
        if attentionSeeded {
            for item in waiting where !knownAttention.contains(item.id) {
                let review = item.id.hasPrefix("review")
                enqueuePeek(LivePeek(id: item.id, symbol: review ? "exclamationmark.bubble.fill" : "hand.raised.fill",
                                     title: review ? item.detail : item.title, detail: review ? item.title : item.detail,
                                     tone: .attention, actionTitle: item.actionTitle, target: item.target))
            }
        }
        let waitingIDs = Set(waiting.map(\.id))
        peekQueue.removeAll { LivePeekQueue.isStale($0.id, waiting: waitingIDs) }
        if let shown = model.peek, LivePeekQueue.isStale(shown.id, waiting: waitingIDs) {
            peekTask?.cancel()
            peekTask = nil
            showNextPeek()
        }
        knownAttention = waitingIDs
        attentionSeeded = true
        if islandContentChanged { rebuild() }
        refreshSlot()
    }

    // MARK: Building what is shown

    private func runningAgents() -> [AgentInfo] { agentsSnapshot.filter(\.isActive) }

    private func newestRunningAgent() -> AgentInfo? {
        runningAgents().min { ($0.durationMS ?? .max) < ($1.durationMS ?? .max) }
    }

    private func rebuild() {
        guard state != nil else { return }
        var now: [LiveItem] = []
        if serverWasRunning && !serverRunningNow {
            now.append(LiveItem(id: "server", symbol: "bolt.horizontal.circle", title: "Mac MCP stopped answering",
                                detail: "Start it from the menu bar", tone: .problem, startedAt: nil, target: nil))
        }
        let agents = runningAgents()
        for agent in agents {
            let step = agent.lastTool.map { Self.toolWords($0) } ?? (agent.phase.map { $0.capitalized } ?? "Working")
            // `symbol` carries the provider here; the row turns it into the maker's logo.
            now.append(LiveItem(id: "agent-\(agent.agentID)", symbol: agent.provider ?? "", title: Self.agentName(agent),
                                detail: "\(Self.providerName(agent.provider)) · \(step)", tone: .working,
                                startedAt: estimatedAgentStartedAt(agent),
                                target: .agent(agent.agentID)))
        }
        now.append(contentsOf: runningTasks)
        let agentIDs = Set(agents.map(\.agentID))
        var waitingTools: [LiveItem] = []
        for call in tools.values.sorted(by: { $0.startedAt < $1.startedAt }) where call.agentID.map({ !agentIDs.contains($0) }) ?? true {
            let asking = ["browser_checkpoint", "ask_user", "ask_confirmation"].contains(call.tool)
            // `symbol` carries the tool name here; the row turns it into the tool's icon.
            let item = LiveItem(id: "tool-\(call.eventID)", symbol: call.tool, title: call.description,
                                detail: [call.client, Self.toolWords(call.tool)].compactMap { $0 }.joined(separator: " · "),
                                tone: asking ? .attention : .working, startedAt: call.startedAt, target: .dashboard)
            if asking { waitingTools.append(item) } else { now.append(item) }
        }
        let nextNeedsYou = waitingTools + attention + reviews
        if now != model.now { model.now = now }
        if nextNeedsYou != model.needsYou { model.needsYou = nextNeedsYou }
        if model.working != !now.isEmpty { model.working = !now.isEmpty }
    }

    private func estimatedAgentStartedAt(_ agent: AgentInfo) -> Date? {
        guard let durationMS = agent.durationMS else { return nil }
        if let estimated = agentStartedAt[agent.agentID] { return estimated }
        let estimated = Date().addingTimeInterval(-Double(durationMS) / 1000)
        agentStartedAt[agent.agentID] = estimated
        return estimated
    }

    /// The right wing: what needs you first, then the icon of the current work, else empty.
    private func refreshSlot() {
        let slot: LiveIslandModel.Slot
        if !model.needsYou.isEmpty {
            slot = .waiting(count: model.needsYou.count)
        } else if let call = tools.values.max(by: { $0.startedAt < $1.startedAt }) {
            var icon = IslandIcon.forTool(call.tool, target: call.target)
            // Starting or waiting for agents: show who does the work, not a generic agent symbol.
            if icon == .symbol("sparkles"), let agent = newestRunningAgent() { icon = IslandIcon.forProvider(agent.provider) }
            // A call that only starts or waits for agents is not extra work beside them.
            let agentCalls = runningAgents().isEmpty ? 0 : tools.values.filter {
                IslandIcon.forTool($0.tool, target: $0.target) == .symbol("sparkles")
            }.count
            slot = .working(icon, count: max(1, tools.count - agentCalls + runningAgents().count))
        } else if let agent = newestRunningAgent() {
            // The maker of the agent that started last; the count covers the rest.
            slot = .working(IslandIcon.forProvider(agent.provider), count: runningAgents().count + runningTasks.count)
        } else if !runningTasks.isEmpty {
            slot = .working(.symbol("calendar.badge.clock"), count: runningTasks.count)
        } else if finishTask != nil {
            return  // the tick is showing; it clears itself
        } else {
            slot = .empty
        }
        if slot != model.slot { withAnimation(IslandMotion.slot) { model.slot = slot } }
    }

    private func remember(_ entry: LiveHistoryEntry) {
        var list = model.recent.filter { $0.id != entry.id }
        list.insert(entry, at: 0)
        let nextRecent = Array(list.prefix(20))
        if nextRecent != model.recent { model.recent = nextRecent }
    }

    private func enqueuePeek(_ peek: LivePeek) {
        guard enabled, !model.observationPaused else { return }
        peekQueue.removeAll { $0.id == peek.id }
        peekQueue.append(peek)
        if peekTask == nil { showNextPeek() }
    }

    private func showNextPeek() {
        guard !peekQueue.isEmpty else {
            peekTask = nil
            model.peek = nil
            model.settle()
            return
        }
        let next = peekQueue.removeFirst()
        if model.mode == .peek {
            withAnimation(IslandMotion.slot) { model.peek = next }
        } else {
            model.peek = next
            model.settle()
        }
        let hold: UInt64 = next.tone == .attention || next.tone == .notice ? 5_000_000_000 : 3_500_000_000
        peekTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: hold)
            guard !Task.isCancelled else { return }
            self?.showNextPeek()
        }
    }

    // MARK: Actions from the island

    func open(_ target: LiveTarget?) {
        guard let state else { return }
        switch target {
        case .agent(let id):
            if let base = state.dashboardURL, let url = URL(string: base.absoluteString + "#agent=\(id)") { NSWorkspace.shared.open(url) }
        case .settings(let pane):
            MenuAppDelegate.openSettings?(pane)
        case .dashboard:
            if let url = state.dashboardURL { NSWorkspace.shared.open(url) }
        case nil:
            break
        }
    }

    func setPromptCovering(_ covering: Bool) {
        guard model.promptCovering != covering else { return }
        model.promptCovering = covering
        model.settle()
    }

    func togglePause() {
        guard let state else { return }
        state.setPrivacy(paused: !(state.privacyFlags?.paused ?? false))
    }

    private func hoverChanged(_ inside: Bool) {
        model.hovering = inside
        hoverTask?.cancel()
        // Open after a short pause so passing over the menu bar does nothing; close a little later
        // so a cursor crossing the edge does not flicker the panel.
        hoverTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: inside ? 160_000_000 : 260_000_000)
            guard !Task.isCancelled, let self, self.model.hovering == inside else { return }
            if inside { await self.pollOnce() }
            self.model.settle()
        }
    }

    // MARK: Window

    private func showWindow() {
        guard enabled, !locked else { return }
        let panel = ensureWindow()
        position(panel)
        if !panel.isVisible {
            panel.alphaValue = 0
            panel.orderFrontRegardless()
        }
        if panel.alphaValue < 1 {
            NSAnimationContext.runAnimationGroup { context in
                context.duration = 0.35
                panel.animator().alphaValue = 1
            }
        }
        installMouseTracking()
        // A pointer already over the island must not wait for a move before its first click lands.
        updateMouseThrough(hover: false)
    }

    private func hideWindow() {
        removeMouseTracking()
        guard let panel = window, panel.isVisible else { return }
        NSAnimationContext.runAnimationGroup({ context in
            context.duration = 0.25
            panel.animator().alphaValue = 0
        }, completionHandler: {
            Task { @MainActor in
                if !self.enabled || self.locked { panel.orderOut(nil) }
            }
        })
    }

    private func ensureWindow() -> IslandPanel {
        if let window { return window }
        let panel = IslandPanel(contentRect: NSRect(origin: .zero, size: IslandPanel.size),
                                styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.level = NSWindow.Level(rawValue: NSWindow.Level.mainMenu.rawValue + 2)
        panel.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle, .fullScreenAuxiliary]
        panel.isMovable = false
        panel.ignoresMouseEvents = true
        // Panels hide when their app goes to the background by default; the island must not.
        panel.hidesOnDeactivate = false
        let host = IslandHostingView(rootView: LiveIslandView(model: model, controller: self))
        host.model = model
        panel.contentView = host
        window = panel
        return panel
    }

    private func position(_ panel: NSPanel) {
        guard let screen = NotchGeometry.targetScreen() else { return }
        let detected = NotchGeometry.detect(screen)
        if detected != model.notch { model.notch = detected }
        let size = IslandPanel.size
        panel.setFrame(NSRect(x: screen.frame.midX - size.width / 2, y: screen.frame.maxY - size.height,
                              width: size.width, height: size.height), display: true)
    }

    /// Clicks pass through everywhere except the island's shape; it never takes focus.
    private func installMouseTracking() {
        guard mouseMonitors.isEmpty else { return }
        let handler: (NSEvent) -> Void = { [weak self] _ in Task { @MainActor in self?.updateMouseThrough() } }
        if let global = NSEvent.addGlobalMonitorForEvents(matching: [.mouseMoved], handler: handler) { mouseMonitors.append(global) }
        if let local = NSEvent.addLocalMonitorForEvents(matching: [.mouseMoved], handler: { handler($0); return $0 }) {
            mouseMonitors.append(local)
        }
    }

    private func removeMouseTracking() {
        mouseMonitors.forEach(NSEvent.removeMonitor)
        mouseMonitors.removeAll()
        window?.ignoresMouseEvents = true
    }

    /// Let clicks through outside the island's shape. Mouse moves decide this and the hover;
    /// a shape or position change (with the pointer still) decides only the click-through (#203),
    /// so a peek appearing under a resting pointer does not open the panel by itself.
    private func updateMouseThrough(hover: Bool = true) {
        guard let panel = window, panel.isVisible else { return }
        let inside = IslandHitArea.takesClick(at: NSEvent.mouseLocation, in: panel.frame, size: model.size)
        if panel.ignoresMouseEvents == inside { panel.ignoresMouseEvents = !inside }
        if hover, inside != model.hovering { hoverChanged(inside) }
    }

    private func observeSystem() {
        let center = NotificationCenter.default
        observers.append(center.addObserver(forName: NSApplication.didChangeScreenParametersNotification, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in
                guard let self, let panel = self.window else { return }
                self.position(panel)
                self.updateMouseThrough(hover: false)
            }
        })
        // The shape grows and shrinks (rest, peek, expanded, a new notch or panel height) while the
        // pointer may stay still: re-decide the click-through once the new values are set.
        shapeChanges = Publishers.Merge3(model.$mode.map { _ in () }, model.$notch.map { _ in () },
                                         model.$expandedHeight.map { _ in () })
            .receive(on: RunLoop.main)
            .sink { [weak self] in self?.updateMouseThrough(hover: false) }
        observers.append(NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.activeSpaceDidChangeNotification, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in
                guard let self, self.enabled, !self.locked else { return }
                self.window?.orderFrontRegardless()
            }
        })
        let distributed = DistributedNotificationCenter.default()
        observers.append(distributed.addObserver(forName: NSNotification.Name("com.apple.screenIsLocked"), object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in
                self?.locked = true
                self?.window?.orderOut(nil)
            }
        })
        observers.append(distributed.addObserver(forName: NSNotification.Name("com.apple.screenIsUnlocked"), object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in
                self?.locked = false
                self?.showWindow()
            }
        })
    }

    // MARK: Words

    static func isScheduled(_ agent: AgentInfo) -> Bool { (agent.title ?? "").hasPrefix("Scheduled: ") }

    static func agentName(_ agent: AgentInfo) -> String {
        let title = (agent.title ?? "").trimmingCharacters(in: .whitespaces)
        if title.hasPrefix("Scheduled: ") { return String(title.dropFirst(11)) }
        return title.isEmpty ? "Agent task" : title
    }

    static func providerName(_ provider: String?) -> String {
        AgentProviders.name(provider) ?? (provider?.capitalized ?? "Agent")
    }

    static func duration(_ seconds: Double) -> String {
        seconds < 60 ? "\(Int(seconds.rounded())) s" : seconds < 3600 ? "\(Int((seconds / 60).rounded())) min" : String(format: "%.1f h", seconds / 3600)
    }

    static func toolWords(_ tool: String) -> String {
        let known = [
            "browser_observe": "Looking at a page", "browser_act": "Working in a page", "browser_do": "Visiting a page",
            "browser_find": "Searching a page", "browser_checkpoint": "Your turn in the browser", "read_file": "Reading a file",
            "write_file": "Writing a file", "edit_file": "Editing a file", "run_command": "Running a command",
            "search_files": "Searching files", "mac_snapshot": "Looking at the screen", "mac_observe": "Looking at an app",
            "mac_act": "Working in an app", "mac_app": "Using an app", "spawn_agent": "Starting an agent",
            "spawn_agents": "Starting agents", "wait_agents": "Waiting for agents", "ask_user": "Asking you",
            "http_request": "Calling a web service", "scheduled_tasks": "Scheduled Tasks", "tool_invoke": "Using a tool",
            "tool_discover": "Finding a tool", "browser_upload_artifact": "Uploading a file",
            // The agents' own tools
            "Bash": "Running a command", "bash": "Running a command", "shell": "Running a command",
            "run_commands": "Running a command", "command_execution": "Running a command",
            "Read": "Reading a file", "read": "Reading a file", "view_file": "Reading a file", "read_files": "Reading files",
            "Write": "Writing a file", "write": "Writing a file", "write_to_file": "Writing a file",
            "Edit": "Editing a file", "edit": "Editing a file", "editor": "Editing a file",
            "replace_file_content": "Editing a file", "file_change": "Editing a file",
            "Grep": "Searching files", "grep": "Searching files", "grep_search": "Searching files",
            "Glob": "Finding files", "glob": "Finding files", "find_by_name": "Finding files",
            "list_dir": "Looking in a folder", "ls": "Looking in a folder",
        ]
        let name = AgentProviders.toolName(tool)
        if let words = known[name] { return words }
        let spaced = name.replacingOccurrences(of: "_", with: " ")
        return spaced.prefix(1).uppercased() + spaced.dropFirst()
    }

    static func ago(_ date: Date) -> String {
        let seconds = Date().timeIntervalSince(date)
        if seconds < 60 { return "just now" }
        if seconds < 3600 { return "\(Int(seconds / 60)) min ago" }
        if Calendar.current.isDateInToday(date) {
            return date.formatted(.dateTime.hour(.twoDigits(amPM: .omitted)).minute(.twoDigits))
        }
        return TaskTime.relativeDay(date)
    }
}

// MARK: - Window types

final class IslandPanel: NSPanel {
    static let size = NSSize(width: 760, height: 560)
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

final class IslandHostingView: NSHostingView<LiveIslandView> {
    var model: LiveIslandModel?

    override func hitTest(_ point: NSPoint) -> NSView? {
        guard let model else { return nil }
        return IslandHitArea.takesClick(at: point, in: bounds, size: model.size) ? super.hitTest(point) : nil
    }

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
}

// MARK: - Views

extension View {
    /// A row with somewhere to go opens it. One with nowhere (a question waiting for the owner, whose dialog
    /// already sits above every window) stays plain text, so it neither looks clickable nor tells VoiceOver
    /// it opens something (#23).
    @ViewBuilder
    func islandActivatable(_ target: LiveTarget?, perform action: @escaping () -> Void) -> some View {
        if target != nil {
            activatable("Opens it in Mac MCP", perform: action)
        } else {
            self
        }
    }
}

/// The Mac MCP mark in the left wing; dimmed with a pause glyph while observation is paused.
private struct IslandMark: View {
    let paused: Bool
    let privateMode: Bool

    static let icon: NSImage = BrandArt.image("BrandMark")  // the island is always dark: champagne

    var body: some View {
        ZStack(alignment: .bottomTrailing) {
            Image(nsImage: Self.icon)
                .resizable()
                .interpolation(.high)
                .frame(width: 20, height: 20)
                .opacity(paused ? 0.45 : 1)
            if paused || privateMode {
                Image(systemName: paused ? "pause.circle.fill" : "eye.slash.circle.fill")
                    .font(.system(size: 9, weight: .bold))
                    .foregroundStyle(.white, .black)
                    .offset(x: 3, y: 3)
            }
        }
        .accessibilityLabel(Self.spokenLabel(paused: paused, privateMode: privateMode))
    }

    /// What VoiceOver says for the mark: every mode its badge stands for, in every island state (#29).
    static func spokenLabel(paused: Bool, privateMode: Bool) -> String {
        ["Mac MCP", paused ? "observation paused" : nil, privateMode ? "private mode" : nil]
            .compactMap { $0 }.joined(separator: ", ")
    }
}

/// A thin ring with a bright arc that turns steadily around the work icon. Core Animation runs the
/// turn on the GPU, so it keeps going whatever SwiftUI transitions do around it and costs no CPU.
private struct SpinnerRing: NSViewRepresentable {
    var diameter: CGFloat
    var lineWidth: CGFloat

    func makeNSView(context: Context) -> SpinnerView { SpinnerView(diameter: diameter, lineWidth: lineWidth) }
    func updateNSView(_ view: SpinnerView, context: Context) {}
}

final class SpinnerView: NSView {
    private let track = CAShapeLayer()
    private let arc = CAShapeLayer()
    private let diameter: CGFloat
    private let lineWidth: CGFloat

    init(diameter: CGFloat, lineWidth: CGFloat) {
        self.diameter = diameter
        self.lineWidth = lineWidth
        super.init(frame: NSRect(x: 0, y: 0, width: diameter + 2, height: diameter + 2))
        wantsLayer = true
        for layer in [track, arc] {
            layer.fillColor = NSColor.clear.cgColor
            layer.lineWidth = lineWidth
            layer.lineCap = .round
            self.layer?.addSublayer(layer)
        }
        track.strokeColor = NSColor.white.withAlphaComponent(0.14).cgColor
        arc.strokeColor = NSColor(red: 0.45, green: 0.56, blue: 1.0, alpha: 1).cgColor
        arc.strokeStart = 0
        arc.strokeEnd = 0.28
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) is not used") }

    override func layout() {
        super.layout()
        let box = CGRect(x: (bounds.width - diameter) / 2, y: (bounds.height - diameter) / 2, width: diameter, height: diameter)
        let path = CGPath(ellipseIn: box, transform: nil)
        for layer in [track, arc] {
            layer.frame = bounds
            layer.path = path
        }
    }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        guard window != nil else { return }
        arc.removeAnimation(forKey: "spin")
        if NSWorkspace.shared.accessibilityDisplayShouldReduceMotion {
            // No turning: the arc breathes instead.
            let pulse = CABasicAnimation(keyPath: "opacity")
            pulse.fromValue = 1
            pulse.toValue = 0.35
            pulse.duration = 0.9
            pulse.autoreverses = true
            pulse.repeatCount = .infinity
            arc.add(pulse, forKey: "spin")
            return
        }
        let spin = CABasicAnimation(keyPath: "transform.rotation.z")
        spin.fromValue = 0
        spin.toValue = -2 * Double.pi  // clockwise
        spin.duration = 1.15
        spin.repeatCount = .infinity
        spin.isRemovedOnCompletion = false
        arc.add(spin, forKey: "spin")
    }
}

/// The right wing: empty at rest, the icon of the current work with a ring, a tick when it ends,
/// an orange count when something waits for the owner.
private struct IslandSlot: View {
    let slot: LiveIslandModel.Slot

    var body: some View {
        ZStack {
            switch slot {
            case .empty:
                Color.clear
            case .working(let icon, let count):
                ZStack {
                    IconTile(icon: icon, size: 16)
                    SpinnerRing(diameter: 24, lineWidth: 1.8)
                        .frame(width: 26, height: 26)
                    if count > 1 {
                        Text("\(count)")
                            .font(.system(size: 8, weight: .heavy, design: .rounded))
                            .foregroundStyle(.black)
                            .padding(.horizontal, 3)
                            .background(Capsule().fill(Color.white))
                            .offset(x: 10, y: 9)
                    }
                }
                .id(icon)
                .transition(.scale(scale: 0.6).combined(with: .opacity))
            case .finished(let ok):
                Image(systemName: ok ? "checkmark.circle.fill" : "xmark.circle.fill")
                    .font(.system(size: 17, weight: .semibold))
                    .foregroundStyle(ok ? LiveTone.done.color : LiveTone.problem.color)
                    .transition(.scale(scale: 0.3).combined(with: .opacity))
            case .waiting(let count):
                ZStack {
                    Circle().fill(LiveTone.attention.color).frame(width: 18, height: 18)
                    Text(count > 9 ? "9+" : "\(count)")
                        .font(.system(size: 10, weight: .heavy, design: .rounded))
                        .foregroundStyle(.black)
                }
                .transition(.scale(scale: 0.4).combined(with: .opacity))
            }
        }
        .frame(width: 26, height: 26)
    }
}

struct LiveIslandView: View {
    @ObservedObject var model: LiveIslandModel
    let controller: LiveIslandController

    private var radius: CGFloat {
        switch model.mode {
        case .expanded: return 28
        case .peek: return 22
        case .rest: return model.notch.hasNotch ? 10 : 9
        }
    }

    var body: some View {
        let size = model.size
        VStack(spacing: 0) {
            ZStack(alignment: .top) {
                IslandSilhouette(bottomRadius: radius)
                    .fill(Color.black)
                    .shadow(color: .black.opacity(model.mode == .rest ? 0 : 0.45), radius: 18, y: 6)

                HStack(spacing: 0) {
                    IslandMark(paused: model.observationPaused, privateMode: model.privateMode)
                        .frame(width: model.wing)
                    Spacer(minLength: model.notch.width)
                    IslandSlot(slot: model.slot)
                        .frame(width: model.wing)
                }
                .frame(width: model.notch.width + model.wing * 2, height: model.notch.height)

                VStack(spacing: 0) {
                    Color.clear.frame(height: model.notch.height)
                    if model.mode == .peek, let peek = model.peek {
                        peekRow(peek)
                    } else if model.mode == .expanded {
                        IslandPanelContent(model: model, controller: controller)
                            .fixedSize(horizontal: false, vertical: true)
                            .background(GeometryReader { proxy in
                                Color.clear.preference(key: IslandHeightKey.self, value: proxy.size.height)
                            })
                    }
                }
                .frame(width: size.width, alignment: .top)
                .opacity(model.contentVisible ? 1 : 0)
                .offset(y: model.contentVisible ? 0 : -6)
            }
            .frame(width: size.width, height: size.height, alignment: .top)
            .contentShape(Rectangle())
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .onPreferenceChange(IslandHeightKey.self) { height in
            if height > 0, abs(height - model.expandedHeight) > 1 {
                withAnimation(IslandMotion.slot) { model.expandedHeight = min(height, 470) }
            }
        }
    }

    private func peekRow(_ peek: LivePeek) -> some View {
        HoverRow { hovering in
        HStack(spacing: 11) {
            ZStack {
                Circle().fill(peek.tone.color.opacity(0.18)).frame(width: 30, height: 30)
                Image(systemName: peek.symbol).font(.system(size: 13, weight: .bold)).foregroundStyle(peek.tone.color)
            }
            VStack(alignment: .leading, spacing: 1) {
                MarqueeText(text: peek.title, font: .system(size: 13, weight: .semibold), color: .white, active: hovering)
                Text(peek.detail).font(.system(size: 11.5)).foregroundStyle(.white.opacity(0.6)).lineLimit(1)
            }
            Spacer(minLength: 0)
            if let action = peek.actionTitle {
                Button(action) { controller.open(peek.target) }.buttonStyle(IslandPillButton(tint: peek.tone.color))
            }
        }
        .padding(.horizontal, 18)
        .frame(height: 54)
        .accessibilityElement(children: .combine)
        .islandActivatable(peek.target) { controller.open(peek.target) }
        }
        .id(peek.id)
        .transition(.asymmetric(insertion: .move(edge: .top).combined(with: .opacity), removal: .opacity))
    }
}

/// The panel that opens on hover.
private struct IslandPanelContent: View {
    @ObservedObject var model: LiveIslandModel
    let controller: LiveIslandController

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            header
            if !model.needsYou.isEmpty {
                section("Needs you") {
                    ForEach(model.needsYou.prefix(3)) { item in liveRow(item) }
                }
            }
            if !model.now.isEmpty {
                section("Now") {
                    ForEach(model.now.prefix(3)) { item in liveRow(item) }
                }
            }
            HStack(alignment: .top, spacing: 14) {
                section("Recent") {
                    if model.recent.isEmpty {
                        quiet("Nothing yet")
                    } else {
                        ForEach(model.recent.prefix(5)) { entry in historyRow(entry) }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .topLeading)
                section("Scheduled") {
                    if model.scheduled.isEmpty {
                        Button { controller.open(.settings("scheduled-tasks")) } label: {
                            quiet("No scheduled tasks · Add one")
                        }
                        .buttonStyle(.plain)
                    } else {
                        ForEach(model.scheduled.prefix(4)) { task in scheduledRow(task) }
                    }
                }
                .frame(width: 200, alignment: .topLeading)
            }
        }
        .padding(.horizontal, 18)
        .padding(.top, 12)
        .padding(.bottom, 16)
    }

    private var header: some View {
        HStack(spacing: 8) {
            Circle()
                .fill(model.observationPaused ? Color.gray : !model.needsYou.isEmpty ? LiveTone.attention.color
                      : model.working ? LiveTone.working.color : LiveTone.done.color)
                .frame(width: 7, height: 7)
            Text(statusText).font(.system(size: 12, weight: .semibold)).foregroundStyle(.white.opacity(0.85))
            Spacer()
            iconButton(model.observationPaused ? "play.fill" : "pause.fill",
                       help: model.observationPaused ? "Resume observation" : "Pause observation") { controller.togglePause() }
            iconButton("gauge.with.dots.needle.33percent", help: "Open the dashboard") { controller.open(.dashboard) }
            iconButton("gearshape.fill", help: "Settings") { controller.open(.settings("activity")) }
        }
    }

    private var statusText: String {
        if model.observationPaused { return "Observation paused" }
        if !model.needsYou.isEmpty { return model.needsYou.count == 1 ? "1 thing needs you" : "\(model.needsYou.count) things need you" }
        if model.now.count == 1 { return "Working on 1 thing" }
        if model.now.count > 1 { return "Working on \(model.now.count) things" }
        return model.privateMode ? "Idle · Private mode" : "Idle"
    }

    private func iconButton(_ symbol: String, help: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 11, weight: .semibold))
                .frame(width: 28, height: 22)
                .background(Capsule().fill(Color.white.opacity(0.09)))
                .foregroundStyle(.white.opacity(0.85))
        }
        .buttonStyle(.plain)
        .help(help)
        .accessibilityLabel(help)
    }

    private func section<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            Text(title.uppercased()).font(.system(size: 9.5, weight: .bold)).kerning(0.8).foregroundStyle(.white.opacity(0.4))
            content()
        }
    }

    private func quiet(_ text: String) -> some View {
        Text(text).font(.system(size: 11.5)).foregroundStyle(.white.opacity(0.45)).padding(.vertical, 2)
    }

    private func liveRow(_ item: LiveItem) -> some View {
        HoverRow { hovering in
        HStack(spacing: 10) {
            iconFor(item)
            VStack(alignment: .leading, spacing: 2) {
                MarqueeText(text: item.title, font: .system(size: 12.5, weight: .semibold), color: .white, active: hovering)
                Text(item.detail).font(.system(size: 11)).foregroundStyle(.white.opacity(0.55)).lineLimit(1)
                    .id(item.detail)
                    .transition(.asymmetric(insertion: .move(edge: .bottom).combined(with: .opacity), removal: .opacity))
            }
            Spacer(minLength: 6)
            if let action = item.actionTitle {
                Button(action) { controller.open(item.target) }.buttonStyle(IslandPillButton(tint: item.tone.color))
            } else if let start = item.startedAt, item.tone == .working {
                Text(timerInterval: start...Date.distantFuture, countsDown: false)
                    .font(.system(size: 11.5, weight: .semibold, design: .rounded).monospacedDigit())
                    .foregroundStyle(.white.opacity(0.7))
                    .frame(width: 48, alignment: .trailing)
            }
        }
        .padding(8)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Color.white.opacity(item.tone == .attention ? 0.09 : 0.06)))
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(item.tone == .attention ? item.tone.color.opacity(0.4) : .clear))
        .accessibilityElement(children: .combine)
        .islandActivatable(item.target) { controller.open(item.target) }
        .animation(IslandMotion.slot, value: item.detail)
        }
    }

    @ViewBuilder
    private func iconFor(_ item: LiveItem) -> some View {
        if item.id.hasPrefix("tool-") {
            IconTile(icon: IslandIcon.forTool(item.symbol, target: nil), size: 26)
        } else if item.id.hasPrefix("agent-") {
            IconTile(icon: IslandIcon.forProvider(item.symbol), size: 26)
        } else {
            RoundedRectangle(cornerRadius: 7, style: .continuous)
                .fill(LinearGradient(colors: [item.tone.color, item.tone.color.opacity(0.6)], startPoint: .topLeading, endPoint: .bottomTrailing))
                .frame(width: 26, height: 26)
                .overlay(Image(systemName: item.symbol).font(.system(size: 11, weight: .bold)).foregroundStyle(.white))
        }
    }

    private func historyRow(_ entry: LiveHistoryEntry) -> some View {
        HoverRow { hovering in
        HStack(spacing: 8) {
            IconTile(icon: IslandIcon.forTool(entry.tool, target: entry.target), size: 18)
            VStack(alignment: .leading, spacing: 1) {
                MarqueeText(text: entry.title, font: .system(size: 11.5, weight: .medium), color: .white.opacity(0.9), active: hovering)
                Text([entry.client, LiveIslandController.ago(entry.at)].compactMap { $0 }.joined(separator: " · "))
                    .font(.system(size: 10)).foregroundStyle(.white.opacity(0.45)).lineLimit(1)
            }
            Spacer(minLength: 4)
            Image(systemName: entry.ok ? "checkmark" : "xmark")
                .font(.system(size: 9, weight: .bold))
                .foregroundStyle(entry.ok ? LiveTone.done.color : LiveTone.problem.color)
        }
        .contentShape(Rectangle())
        }
    }

    private func scheduledRow(_ task: ScheduledTask) -> some View {
        Button { controller.open(.settings("scheduled-tasks")) } label: {
            HStack(spacing: 8) {
                RoundedRectangle(cornerRadius: 5, style: .continuous)
                    .fill(task.isAI ? LiveTone.working.color.opacity(0.85) : LiveTone.done.color.opacity(0.75))
                    .frame(width: 18, height: 18)
                    .overlay(Image(systemName: task.isAI ? "sparkles" : "chevron.left.forwardslash.chevron.right")
                        .font(.system(size: 8.5, weight: .bold)).foregroundStyle(.white))
                VStack(alignment: .leading, spacing: 1) {
                    Text(task.name).font(.system(size: 11.5, weight: .medium)).foregroundStyle(.white.opacity(0.9)).lineLimit(1)
                    Text(scheduleLine(task)).font(.system(size: 10)).foregroundStyle(.white.opacity(0.45)).lineLimit(1)
                }
                Spacer(minLength: 0)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private func scheduleLine(_ task: ScheduledTask) -> String {
        if task.state == "running" { return "Running now" }
        if task.paused { return "Paused" }
        if let next = task.nextRunAt { return "Next " + TaskTime.upcoming(next) }
        if let last = task.lastRun { return last.shortLine }
        return task.scheduleText
    }
}

/// One line of text that fits by fading at its end; while `active` (the row is hovered) and the text
/// is longer than its room, it glides to its end, rests, glides back, and repeats. When `active`
/// ends it returns to the start quickly.
struct MarqueeText: View {
    let text: String
    let font: Font
    let color: Color
    let active: Bool

    @MacMCPState private var room: CGFloat = 0
    @MacMCPState private var full: CGFloat = 0
    @MacMCPState private var offset: CGFloat = 0
    @MacMCPState private var loop: Task<Void, Never>?

    private var overflow: CGFloat { max(0, full - room) }
    private static let speed: CGFloat = 38  // points per second, slow enough to read

    var body: some View {
        // An invisible one-line copy sets the size; the visible full-length text rides in an overlay,
        // so a long title never widens the row.
        Text(text)
            .font(font)
            .lineLimit(1)
            .opacity(0)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(GeometryReader { proxy in
                Color.clear.onAppear { room = proxy.size.width }.onChange(of: proxy.size.width) { room = $0 }
            })
            .overlay(alignment: .leading) {
                Text(text)
                    .font(font)
                    .foregroundStyle(color)
                    .lineLimit(1)
                    .fixedSize(horizontal: true, vertical: false)
                    .background(GeometryReader { proxy in
                        Color.clear.onAppear { full = proxy.size.width }.onChange(of: proxy.size.width) { full = $0 }
                    })
                    .offset(x: offset)
            }
            .clipped()
            .mask(fadeMask)
            .onChange(of: active) { on in on ? start() : stop() }
            .onDisappear { loop?.cancel() }
            .accessibilityLabel(text)
    }

    /// Fade the edge where text continues: the end at rest, the start once it has moved.
    private var fadeMask: some View {
        let leading = offset < -1
        let trailing = overflow > 1 && offset > -overflow + 1
        return LinearGradient(stops: [
            .init(color: leading ? .clear : .black, location: 0),
            .init(color: .black, location: leading ? 0.06 : 0),
            .init(color: .black, location: trailing ? 0.9 : 1),
            .init(color: trailing ? .clear : .black, location: 1),
        ], startPoint: .leading, endPoint: .trailing)
    }

    private func start() {
        loop?.cancel()
        guard overflow > 1, !IslandMotion.reduceMotion else { return }
        loop = Task { @MainActor in
            let travel = Double(overflow / Self.speed)
            try? await Task.sleep(nanoseconds: 500_000_000)
            while !Task.isCancelled {
                withAnimation(.easeInOut(duration: travel)) { offset = -overflow }
                try? await Task.sleep(nanoseconds: UInt64((travel + 1.1) * 1_000_000_000))
                guard !Task.isCancelled else { return }
                withAnimation(.easeInOut(duration: travel)) { offset = 0 }
                try? await Task.sleep(nanoseconds: UInt64((travel + 1.1) * 1_000_000_000))
            }
        }
    }

    private func stop() {
        loop?.cancel()
        loop = nil
        withAnimation(.easeOut(duration: 0.22)) { offset = 0 }
    }
}

/// A row that knows when the cursor is over it, for its marquee title.
private struct HoverRow<Content: View>: View {
    @ViewBuilder let content: (Bool) -> Content
    @MacMCPState private var hovering = false

    var body: some View {
        content(hovering).onHover { hovering = $0 }
    }
}

private struct IslandHeightKey: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = max(value, nextValue()) }
}

private struct IslandPillButton: ButtonStyle {
    let tint: Color
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 11.5, weight: .semibold))
            .padding(.horizontal, 10).padding(.vertical, 4)
            .background(Capsule().fill(tint.opacity(configuration.isPressed ? 0.7 : 1)))
            .foregroundStyle(.black)
    }
}
