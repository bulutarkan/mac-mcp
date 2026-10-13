import AppKit
import Combine
import SwiftUI

/// The live-activity island (#174): grows out of the notch while Mac MCP works, shows what needs
/// the owner, and stays hidden otherwise. One of the two live-activity styles (the other is the
/// tool activity bubble); off unless chosen in Settings › Activity & Notifications.

// MARK: - Notch geometry

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
        return NotchGeometry(width: 190, height: menuBar > 0 ? menuBar : max(24, NSStatusBar.system.thickness), hasNotch: false)
    }

    /// The screen with a notch, else the main one.
    static func targetScreen() -> NSScreen? {
        NSScreen.screens.first(where: { $0.safeAreaInsets.top > 0 }) ?? NSScreen.main
    }
}

/// Flat top flush with the screen edge, small flares where it meets the menu bar and soft
/// bottom corners, so the island reads as the notch itself growing.
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

enum IslandMotion {
    static var reduceMotion: Bool { NSWorkspace.shared.accessibilityDisplayShouldReduceMotion }
    static var open: Animation { reduceMotion ? .easeInOut(duration: 0.18) : .spring(response: 0.42, dampingFraction: 0.80) }
    static var close: Animation { reduceMotion ? .easeInOut(duration: 0.16) : .spring(response: 0.30, dampingFraction: 0.90) }
    static var change: Animation { reduceMotion ? .easeInOut(duration: 0.15) : .spring(response: 0.35, dampingFraction: 0.85) }
}

extension LiveTone {
    var color: Color {
        switch self {
        case .working: return Color(red: 0.45, green: 0.56, blue: 1.0)
        case .done: return Color(red: 0.30, green: 0.85, blue: 0.50)
        case .attention: return Color(red: 1.0, green: 0.66, blue: 0.25)
        case .problem: return Color(red: 1.0, green: 0.38, blue: 0.36)
        }
    }

    var badgeSymbol: String {
        switch self {
        case .done: return "checkmark"
        case .attention: return "exclamationmark"
        case .problem: return "xmark"
        case .working: return "ellipsis"
        }
    }
}

// MARK: - Model

@MainActor
final class LiveIslandModel: ObservableObject {
    enum Mode: Int { case hidden = 0, compact, peek, expanded }

    @Published private(set) var mode: Mode = .hidden
    @Published var notch: NotchGeometry
    @Published var now: [LiveItem] = []
    @Published var needsYou: [LiveItem] = []
    @Published var upNext: (name: String, at: Date)?
    @Published var peek: LivePeek?
    @Published var lingerTone: LiveTone?
    @Published var privateMode = false
    @Published var observationPaused = false
    @Published var contentVisible = false
    @Published var expandedHeight: CGFloat = 200
    var hovering = false

    let wing: CGFloat = 76

    init(notch: NotchGeometry) { self.notch = notch }

    /// The tone the wings show: what needs the owner first, then problems, work, endings.
    var tone: LiveTone {
        if !needsYou.isEmpty { return .attention }
        if now.contains(where: { $0.tone == .problem }) { return .problem }
        if !now.isEmpty { return .working }
        return lingerTone ?? peek?.tone ?? .done
    }

    var hasContent: Bool { !now.isEmpty || !needsYou.isEmpty || peek != nil || lingerTone != nil }

    var size: CGSize {
        let compactWidth = notch.width + wing * 2
        switch mode {
        case .hidden: return CGSize(width: notch.width, height: notch.height)
        case .compact: return CGSize(width: compactWidth, height: notch.height)
        case .peek: return CGSize(width: max(compactWidth, 440), height: notch.height + 54)
        case .expanded: return CGSize(width: max(compactWidth, 560), height: notch.height + expandedHeight)
        }
    }

    func go(_ new: Mode) {
        guard new != mode else { return }
        let opening = new.rawValue > mode.rawValue
        if !opening || new == .compact || new == .hidden {
            withAnimation(.easeOut(duration: 0.10)) { contentVisible = false }
        }
        withAnimation(opening ? IslandMotion.open : IslandMotion.close) { mode = new }
        if new == .peek || new == .expanded {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.10) { [weak self] in
                guard let self, self.mode == new else { return }
                withAnimation(.easeOut(duration: 0.18)) { self.contentVisible = true }
            }
        }
    }

    /// The resting mode for the current content (hover and peeks sit on top of it).
    func settle() {
        if observationPaused || !hasContent {
            go(.hidden)
        } else if hovering {
            go(.expanded)
        } else if peek != nil {
            go(.peek)
        } else {
            go(.compact)
        }
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
    private var peekTask: Task<Void, Never>?
    private var lingerTask: Task<Void, Never>?
    private var pollTask: Task<Void, Never>?
    private var mouseMonitors: [Any] = []
    private var observers: [NSObjectProtocol] = []
    private var locked = false
    private var serverWasRunning = false
    private var attention: [LiveItem] = []
    private var reviews: [LiveItem] = []
    private var runningTasks: [LiveItem] = []
    private var knownProposals: Set<String> = []
    private var proposalsSeeded = false

    var enabled: Bool { state?.settings.showLiveIsland == true }

    func attach(_ state: AppState) {
        self.state = state
        state.onLiveToolCallStarted = { [weak self] call in self?.toolStarted(call) }
        state.onLiveToolCallFinished = { [weak self] id in self?.toolFinished(id) }
        state.onScheduledRunFinished = { [weak self] run, name in self?.scheduledRunFinished(run, name: name) }
        state.$agents
            .sink { [weak self] agents in self?.agentsChanged(agents) }
            .store(in: &subscriptions)
        state.$privacyFlags
            .sink { [weak self] flags in
                self?.model.privateMode = flags?.privateMode == true
                self?.model.observationPaused = flags?.paused == true
                self?.model.settle()
                self?.updateWindow()
            }
            .store(in: &subscriptions)
        state.$serverRunning
            .removeDuplicates()
            .sink { [weak self] running in self?.serverChanged(running) }
            .store(in: &subscriptions)
        state.settings.$showLiveIsland
            .removeDuplicates()
            .sink { [weak self] on in self?.enabledChanged(on) }
            .store(in: &subscriptions)
        observeSystem()
    }

    /// A short sample of every state, from Settings.
    func preview() {
        guard window != nil || enabled else { return }
        enqueuePeek(LivePeek(id: "preview-1", symbol: "sparkles", title: "Codex started “Weekly ad report”",
                             detail: "Scheduled task · Every Monday at 09:00", tone: .working))
        enqueuePeek(LivePeek(id: "preview-2", symbol: "checkmark", title: "Weekly ad report finished",
                             detail: "Codex · 36 s", tone: .done))
    }

    // MARK: Inputs

    private func toolStarted(_ call: LiveToolCall) {
        tools[call.eventID] = call
        rebuild()
    }

    private func toolFinished(_ eventID: String) {
        guard tools.removeValue(forKey: eventID) != nil else { return }
        if tools.isEmpty && runningAgents().isEmpty && runningTasks.isEmpty { linger(.done) }
        rebuild()
    }

    private func agentsChanged(_ agents: [AgentInfo]) {
        defer { rebuild() }
        guard agentsSeeded else {
            agentStatus = Dictionary(agents.map { ($0.agentID, $0.status ?? "") }, uniquingKeysWith: { a, _ in a })
            agentsSeeded = true
            return
        }
        for agent in agents {
            let previous = agentStatus[agent.agentID]
            let current = agent.status ?? ""
            agentStatus[agent.agentID] = current
            guard previous != current, !scheduledTitle(agent) else { continue }  // scheduled runs announce themselves
            let name = Self.agentName(agent)
            let provider = Self.providerName(agent.provider)
            if agent.isActive, previous == nil || previous == "queued" {
                enqueuePeek(LivePeek(id: "agent-start-\(agent.agentID)", symbol: "sparkles",
                                     title: "\(provider) started \(name)", detail: agent.model ?? "Delegated agent",
                                     tone: .working, target: .agent(agent.agentID)))
            } else if !agent.isActive, previous.map({ $0 == "running" || $0 == "starting" }) == true {
                let ok = current == "completed"
                enqueuePeek(LivePeek(id: "agent-end-\(agent.agentID)", symbol: ok ? "checkmark" : "xmark",
                                     title: ok ? "\(name) finished" : "\(name) \(current == "cancelled" ? "was stopped" : "failed")",
                                     detail: [provider, agent.durationMS.map { Self.duration(Double($0) / 1000) }].compactMap { $0 }.joined(separator: " · "),
                                     tone: ok ? .done : .problem, target: .agent(agent.agentID)))
            }
        }
    }

    private func scheduledRunFinished(_ run: TaskRun, name: String) {
        let tone: LiveTone = run.status == "done" ? .done : run.status == "skipped" ? .attention : .problem
        let title: String
        switch run.status {
        case "done": title = "\(name) finished"
        case "skipped": title = "\(name) was skipped"
        case "timed_out": title = "\(name) stopped at its time limit"
        default: title = "\(name) failed"
        }
        let detail = run.status == "done" ? (run.duration.map { "Scheduled task · \(Self.duration($0))" } ?? "Scheduled task")
            : (run.reason ?? "Scheduled task")
        enqueuePeek(LivePeek(id: "run-\(run.id)", symbol: tone == .done ? "checkmark" : tone == .problem ? "xmark" : "forward.end",
                             title: title, detail: detail, tone: tone, target: .settings("scheduled-tasks")))
        Task { await pollOnce() }
    }

    private func serverChanged(_ running: Bool) {
        if running { serverWasRunning = true }
        rebuild()
    }

    private func enabledChanged(_ on: Bool) {
        if on {
            model.notch = NotchGeometry.detect(NotchGeometry.targetScreen())
            startPolling()
        } else {
            pollTask?.cancel()
            pollTask = nil
            peekQueue.removeAll()
            model.peek = nil
            model.lingerTone = nil
            model.go(.hidden)
        }
        rebuild()
    }

    // MARK: Polling what the app does not already have

    private func startPolling() {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.pollOnce()
                try? await Task.sleep(nanoseconds: 5_000_000_000)
            }
        }
    }

    private func pollOnce() async {
        guard enabled, let state else { return }
        async let attentionCall = state.taskAPI("dashboard/api/attention")
        async let tasksCall = state.taskAPI("dashboard/api/scheduled-tasks")
        let (attentionResult, tasksResult) = await (attentionCall, tasksCall)
        if attentionResult.ok {
            attention = (attentionResult.object["pending"] as? [[String: Any]] ?? []).map { row in
                let tool = row["tool"] as? String ?? "an action"
                let origin = (row["origin"] as? String).flatMap { $0.isEmpty ? nil : $0 }
                return LiveItem(id: "approval-\(row["request_id"] as? String ?? UUID().uuidString)", symbol: "hand.raised.fill",
                                title: "Waiting for your answer",
                                detail: [Self.toolWords(tool), origin].compactMap { $0 }.joined(separator: " · ") + " — answer in the Mac MCP prompt",
                                tone: .attention, startedAt: (row["created_at"] as? Double).map { Date(timeIntervalSince1970: $0) }, target: nil)
            }
        }
        if tasksResult.ok {
            let tasks = (tasksResult.object["tasks"] as? [[String: Any]] ?? []).map(ScheduledTask.init)
            reviews = tasks.filter { $0.proposal != nil }.map { task in
                let by = task.proposal?["proposed_by"] as? String ?? "An AI app"
                let name = task.proposal?["name"] as? String ?? task.name
                return LiveItem(id: "review-\(task.id)", symbol: "text.badge.checkmark", title: name,
                                detail: task.approved ? "\(by) suggested a change" : "\(by) suggested this task",
                                tone: .attention, startedAt: nil, target: .settings("scheduled-tasks"), actionTitle: "Review")
            }
            let proposalIDs = Set(reviews.map(\.id))
            if proposalsSeeded {
                for item in reviews where !knownProposals.contains(item.id) {
                    enqueuePeek(LivePeek(id: item.id, symbol: "exclamationmark.bubble.fill", title: item.detail,
                                         detail: item.title, tone: .attention, actionTitle: "Review", target: item.target))
                }
            }
            knownProposals = proposalIDs
            proposalsSeeded = true
            runningTasks = tasks.filter { $0.state == "running" && !$0.isAI }.map { task in
                LiveItem(id: "task-\(task.id)", symbol: "chevron.left.forwardslash.chevron.right", title: task.name,
                         detail: "Scheduled script", tone: .working, startedAt: task.lastRun?.startedAt,
                         target: .settings("scheduled-tasks"))
            }
            if let next = (tasksResult.object["up_next"] as? [[String: Any]])?.first,
               let at = next["at"] as? Double {
                model.upNext = (next["name"] as? String ?? "", Date(timeIntervalSince1970: at))
            } else {
                model.upNext = nil
            }
        }
        rebuild()
    }

    // MARK: Building what is shown

    private func runningAgents() -> [AgentInfo] {
        (state?.agents ?? []).filter(\.isActive)
    }

    private func rebuild() {
        guard enabled, let state else {
            updateWindow()
            return
        }
        var now: [LiveItem] = []
        if serverWasRunning && !state.serverRunning {
            now.append(LiveItem(id: "server", symbol: "bolt.horizontal.circle", title: "Mac MCP stopped answering",
                                detail: "Start it from the menu bar", tone: .problem, startedAt: nil, target: nil))
        }
        for agent in runningAgents() {
            let step = agent.lastTool.map { Self.toolWords($0) } ?? (agent.phase.map { $0.capitalized } ?? "Working")
            now.append(LiveItem(id: "agent-\(agent.agentID)", symbol: "sparkles", title: Self.agentName(agent),
                                detail: "\(Self.providerName(agent.provider)) · \(step)", tone: .working,
                                startedAt: agent.durationMS.map { Date().addingTimeInterval(-Double($0) / 1000) },
                                target: .agent(agent.agentID)))
        }
        now.append(contentsOf: runningTasks)
        // Tool calls made by an agent are already shown on that agent's row.
        let agentIDs = Set(runningAgents().map(\.agentID))
        for call in tools.values.sorted(by: { $0.startedAt < $1.startedAt }) where call.agentID.map({ !agentIDs.contains($0) }) ?? true {
            let app = call.client
            let attention = ["browser_checkpoint", "ask_user", "ask_choice", "ask_confirmation"].contains(call.tool)
            now.append(LiveItem(id: "tool-\(call.eventID)", symbol: Self.toolSymbol(call.tool), title: call.description,
                                detail: [app, Self.toolWords(call.tool)].compactMap { $0 }.joined(separator: " · "),
                                tone: attention ? .attention : .working, startedAt: call.startedAt, target: .dashboard))
        }
        let waiting = now.filter { $0.tone == .attention }
        model.now = now.filter { $0.tone != .attention }
        model.needsYou = waiting + attention + reviews
        model.settle()
        updateWindow()
    }

    private func linger(_ tone: LiveTone) {
        lingerTask?.cancel()
        withAnimation(IslandMotion.change) { model.lingerTone = tone }
        lingerTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 2_500_000_000)
            guard !Task.isCancelled, let self else { return }
            withAnimation(IslandMotion.change) { self.model.lingerTone = nil }
            self.model.settle()
            self.updateWindow()
        }
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
            if model.peek != nil {
                let ended = model.peek?.tone
                withAnimation(IslandMotion.change) { model.peek = nil }
                if let ended, ended != .working, model.now.isEmpty { linger(ended) }
            }
            model.settle()
            updateWindow()
            return
        }
        let next = peekQueue.removeFirst()
        withAnimation(IslandMotion.change) { model.peek = next }
        model.settle()
        updateWindow()
        let hold: UInt64 = next.tone == .attention ? 5_000_000_000 : 3_500_000_000
        peekTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: hold)
            guard !Task.isCancelled else { return }
            self?.showNextPeek()
        }
    }

    func open(_ target: LiveTarget?) {
        guard let state else { return }
        switch target {
        case .agent(let id):
            if let url = state.dashboardURL.flatMap({ URL(string: $0.absoluteString + "#agent=\(id)") }) { NSWorkspace.shared.open(url) }
        case .settings(let pane):
            MenuAppDelegate.openSettings?(pane)
        case .dashboard:
            if let url = state.dashboardURL { NSWorkspace.shared.open(url) }
        case nil:
            break
        }
    }

    func togglePause() {
        guard let state else { return }
        state.setPrivacy(paused: !(state.privacyFlags?.paused ?? false))
    }

    // MARK: Window

    private func updateWindow() {
        let visible = enabled && !locked && model.mode != .hidden
        if visible {
            let panel = ensureWindow()
            if !panel.isVisible {
                position(panel)
                panel.orderFrontRegardless()
                installMouseTracking()
            }
            shapeChanged()
        } else if let panel = window, panel.isVisible {
            removeMouseTracking()
            // Let the closing spring finish before the window goes away.
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.45) { [weak self] in
                guard let self, self.model.mode == .hidden || !self.enabled || self.locked else { return }
                panel.orderOut(nil)
            }
        }
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

    /// Clicks pass through everywhere except the visible island; it never takes focus.
    private func installMouseTracking() {
        guard mouseMonitors.isEmpty else { return }
        let handler: (NSEvent) -> Void = { [weak self] _ in
            Task { @MainActor in self?.updateMouseThrough() }
        }
        if let global = NSEvent.addGlobalMonitorForEvents(matching: [.mouseMoved], handler: handler) {
            mouseMonitors.append(global)
        }
        if let local = NSEvent.addLocalMonitorForEvents(matching: [.mouseMoved], handler: { event in handler(event); return event }) {
            mouseMonitors.append(local)
        }
        updateMouseThrough()
    }

    private func removeMouseTracking() {
        mouseMonitors.forEach(NSEvent.removeMonitor)
        mouseMonitors.removeAll()
        window?.ignoresMouseEvents = true
    }

    private func updateMouseThrough() {
        guard let panel = window else { return }
        let cursor = NSEvent.mouseLocation
        let frame = panel.frame
        let size = model.size
        let rect = NSRect(x: frame.midX - size.width / 2, y: frame.maxY - size.height, width: size.width, height: size.height)
        let inside = model.mode != .hidden && rect.contains(cursor)
        if panel.ignoresMouseEvents == inside { panel.ignoresMouseEvents = !inside }
        if !inside, model.hovering {
            model.hovering = false
            model.settle()
        }
    }

    /// After the shape changes size under a cursor that is not moving.
    func shapeChanged() {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in self?.updateMouseThrough() }
    }

    private func observeSystem() {
        let center = NotificationCenter.default
        observers.append(center.addObserver(forName: NSApplication.didChangeScreenParametersNotification, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in
                guard let self, let panel = self.window else { return }
                self.position(panel)
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
                self?.updateWindow()
            }
        })
    }

    // MARK: Words

    private func scheduledTitle(_ agent: AgentInfo) -> Bool { (agent.title ?? "").hasPrefix("Scheduled: ") }

    static func agentName(_ agent: AgentInfo) -> String {
        let title = (agent.title ?? "").trimmingCharacters(in: .whitespaces)
        if title.hasPrefix("Scheduled: ") { return String(title.dropFirst(11)) }
        return title.isEmpty ? "Agent task" : title
    }

    static func providerName(_ provider: String?) -> String {
        ["codex": "Codex", "claude": "Claude Code", "opencode": "OpenCode"][provider ?? ""] ?? (provider?.capitalized ?? "Agent")
    }

    static func duration(_ seconds: Double) -> String {
        seconds < 60 ? "\(Int(seconds.rounded())) s" : seconds < 3600 ? "\(Int((seconds / 60).rounded())) min" : String(format: "%.1f h", seconds / 3600)
    }

    static func toolWords(_ tool: String) -> String {
        let known = [
            "browser_observe": "Looking at a page", "browser_act": "Working in a page", "browser_do": "Visiting a page",
            "browser_checkpoint": "Your turn in the browser", "read_file": "Reading a file", "write_file": "Writing a file",
            "edit_file": "Editing a file", "run_command": "Running a command", "search_files": "Searching files",
            "mac_snapshot": "Looking at the screen", "mac_observe": "Looking at an app", "mac_act": "Working in an app",
            "spawn_agent": "Starting an agent", "spawn_agents": "Starting agents", "wait_agents": "Waiting for agents",
            "ask_user": "Asking you", "http_request": "Calling a web service", "scheduled_tasks": "Scheduled Tasks",
        ]
        if let words = known[tool] { return words }
        let spaced = tool.replacingOccurrences(of: "_", with: " ")
        return spaced.prefix(1).uppercased() + spaced.dropFirst()
    }

    static func toolSymbol(_ tool: String) -> String {
        if tool.hasPrefix("browser") { return "safari" }
        if tool.contains("file") { return "doc.text" }
        if tool.contains("command") || tool.contains("job") { return "terminal" }
        if tool.hasPrefix("mac_") || tool == "screenshot" { return "macwindow" }
        if tool.contains("agent") { return "sparkles" }
        if tool.hasPrefix("ask") { return "questionmark.bubble" }
        return "wrench.and.screwdriver"
    }
}

// MARK: - Window types

final class IslandPanel: NSPanel {
    static let size = NSSize(width: 720, height: 420)
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

final class IslandHostingView: NSHostingView<LiveIslandView> {
    var model: LiveIslandModel?

    override func hitTest(_ point: NSPoint) -> NSView? {
        guard let model else { return nil }
        let size = model.size
        let rect = NSRect(x: bounds.midX - size.width / 2, y: bounds.maxY - size.height, width: size.width, height: size.height)
        return rect.contains(point) ? super.hitTest(point) : nil
    }

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
}

// MARK: - Views

/// The Mac MCP icon; a thin ring turns around it while work runs and a badge settles on it otherwise.
private struct IslandMark: View {
    let tone: LiveTone
    let privateMode: Bool
    @MacMCPState private var spin = false

    var body: some View {
        ZStack {
            Image(nsImage: NSApp.applicationIconImage)
                .resizable()
                .interpolation(.high)
                .frame(width: 18, height: 18)
                .clipShape(RoundedRectangle(cornerRadius: 4.5, style: .continuous))
            Circle()
                .trim(from: 0, to: 0.28)
                .stroke(LiveTone.working.color, style: StrokeStyle(lineWidth: 2, lineCap: .round))
                .frame(width: 26, height: 26)
                .rotationEffect(.degrees(spin ? 360 : 0))
                .animation(IslandMotion.reduceMotion ? nil : .linear(duration: 1.1).repeatForever(autoreverses: false), value: spin)
                .opacity(tone == .working ? 1 : 0)
            if tone != .working {
                ZStack {
                    Circle().fill(tone.color).frame(width: 11, height: 11)
                    Image(systemName: tone.badgeSymbol).font(.system(size: 6.5, weight: .black)).foregroundStyle(.black)
                }
                .overlay(Circle().stroke(Color.black, lineWidth: 1.5))
                .offset(x: 8, y: 8)
                .transition(.scale(scale: 0.1).combined(with: .opacity))
            }
            if privateMode {
                Image(systemName: "eye.slash.fill")
                    .font(.system(size: 7, weight: .bold))
                    .foregroundStyle(.white.opacity(0.8))
                    .offset(x: -9, y: -9)
            }
        }
        .frame(width: 30, height: 28)
        .onAppear { spin = true }
        .accessibilityLabel(tone == .working ? "Mac MCP is working" : tone == .attention ? "Mac MCP needs you" : "Mac MCP")
    }
}

struct LiveIslandView: View {
    @ObservedObject var model: LiveIslandModel
    let controller: LiveIslandController

    private var radius: CGFloat {
        switch model.mode {
        case .expanded: return 26
        case .peek: return 20
        default: return 12
        }
    }

    var body: some View {
        let size = model.size
        VStack(spacing: 0) {
            ZStack(alignment: .top) {
                IslandSilhouette(bottomRadius: radius)
                    .fill(Color.black)
                    .shadow(color: model.tone.color.opacity(model.mode == .hidden ? 0 : 0.32), radius: 14, y: 2)
                    .overlay(IslandSilhouette(bottomRadius: radius).stroke(Color.white.opacity(model.mode == .expanded ? 0.08 : 0), lineWidth: 1))

                wings
                    .opacity(model.mode == .hidden ? 0 : 1)
                    .scaleEffect(model.mode == .hidden ? 0.6 : 1, anchor: .top)

                VStack(spacing: 0) {
                    Color.clear.frame(height: model.notch.height)
                    if model.mode == .peek, let peek = model.peek {
                        peekRow(peek)
                    } else if model.mode == .expanded {
                        expanded
                            .background(GeometryReader { proxy in
                                Color.clear.preference(key: IslandHeightKey.self, value: proxy.size.height)
                            })
                    }
                }
                .opacity(model.contentVisible ? 1 : 0)
                .offset(y: model.contentVisible ? 0 : -8)
                .blur(radius: model.contentVisible ? 0 : 3)
            }
            .frame(width: size.width, height: size.height, alignment: .top)
            .contentShape(Rectangle())
            .onHover { inside in
                model.hovering = inside
                if inside {
                    // A short pause so passing over the menu bar does not open the panel.
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.18) {
                        if model.hovering { model.settle() }
                    }
                } else {
                    model.settle()
                }
                controller.shapeChanged()
            }
            .onTapGesture {
                if model.mode == .compact { model.hovering = true; model.settle() }
            }
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .onPreferenceChange(IslandHeightKey.self) { height in
            if height > 0, abs(height - model.expandedHeight) > 1 {
                withAnimation(IslandMotion.change) { model.expandedHeight = height }
            }
        }
    }

    private var wings: some View {
        HStack(spacing: 0) {
            IslandMark(tone: model.tone, privateMode: model.privateMode)
                .frame(width: model.wing)
            Spacer(minLength: model.notch.width)
            rightWing
                .frame(width: model.wing)
        }
        .frame(width: model.notch.width + model.wing * 2, height: model.notch.height)
    }

    @ViewBuilder
    private var rightWing: some View {
        if !model.needsYou.isEmpty {
            Text(model.needsYou.count == 1 ? "Review" : "\(model.needsYou.count) waiting")
                .font(.system(size: 12, weight: .semibold, design: .rounded))
                .foregroundStyle(LiveTone.attention.color)
        } else if let problem = model.now.first(where: { $0.tone == .problem }) {
            Text(problem.id == "server" ? "Stopped" : "Failed")
                .font(.system(size: 12, weight: .semibold, design: .rounded))
                .foregroundStyle(LiveTone.problem.color)
        } else if model.now.count == 1, let start = model.now.first?.startedAt {
            Text(timerInterval: start...Date.distantFuture, countsDown: false)
                .font(.system(size: 12, weight: .semibold, design: .rounded).monospacedDigit())
                .foregroundStyle(.white)
                .multilineTextAlignment(.center)
        } else if model.now.count > 1 {
            Text("\(model.now.count) live")
                .font(.system(size: 12, weight: .semibold, design: .rounded))
                .foregroundStyle(.white)
        } else if let tone = model.lingerTone ?? model.peek?.tone, tone != .working {
            Text(tone == .done ? "Done" : tone == .attention ? "Skipped" : "Failed")
                .font(.system(size: 12, weight: .semibold, design: .rounded))
                .foregroundStyle(tone.color)
        }
    }

    private func peekRow(_ peek: LivePeek) -> some View {
        HStack(spacing: 10) {
            ZStack {
                Circle().fill(peek.tone.color.opacity(0.18)).frame(width: 30, height: 30)
                Image(systemName: peek.symbol).font(.system(size: 13, weight: .bold)).foregroundStyle(peek.tone.color)
            }
            VStack(alignment: .leading, spacing: 1) {
                Text(peek.title).font(.system(size: 13, weight: .semibold)).foregroundStyle(.white).lineLimit(1)
                Text(peek.detail).font(.system(size: 11.5)).foregroundStyle(.white.opacity(0.6)).lineLimit(1)
            }
            Spacer(minLength: 0)
            if let action = peek.actionTitle {
                Button(action) { controller.open(peek.target) }
                    .buttonStyle(IslandPillButton(tint: peek.tone.color))
            }
        }
        .padding(.horizontal, 16)
        .frame(height: 54)
        .contentShape(Rectangle())
        .onTapGesture { controller.open(peek.target) }
        .id(peek.id)
        .transition(.asymmetric(insertion: .move(edge: .top).combined(with: .opacity), removal: .opacity))
    }

    private var expanded: some View {
        VStack(alignment: .leading, spacing: 10) {
            if !model.needsYou.isEmpty {
                section("NEEDS YOU")
                ForEach(model.needsYou.prefix(3)) { item in row(item) }
            }
            if !model.now.isEmpty {
                section("NOW")
                ForEach(model.now.prefix(4)) { item in row(item) }
            }
            if model.now.isEmpty && model.needsYou.isEmpty {
                HStack(spacing: 8) {
                    Image(systemName: "checkmark.circle.fill").foregroundStyle(LiveTone.done.color)
                    Text(model.peek?.title ?? "All quiet").font(.system(size: 13, weight: .medium)).foregroundStyle(.white)
                }
                .padding(.vertical, 4)
            }
            if let next = model.upNext {
                HStack(spacing: 6) {
                    Image(systemName: "clock").font(.system(size: 10))
                    Text("Up next: \(TaskTime.upcoming(next.at)) · \(next.name)").font(.system(size: 11)).lineLimit(1)
                }
                .foregroundStyle(.white.opacity(0.5))
            }
            Divider().overlay(Color.white.opacity(0.08))
            HStack(spacing: 8) {
                Button {
                    controller.togglePause()
                } label: {
                    Label(model.observationPaused ? "Resume" : "Pause observation",
                          systemImage: model.observationPaused ? "play.fill" : "pause.fill")
                }
                .buttonStyle(IslandFooterButton())
                Spacer()
                Button { controller.open(.dashboard) } label: { Label("Dashboard", systemImage: "gauge.with.dots.needle.33percent") }
                    .buttonStyle(IslandFooterButton())
                Button { controller.open(.settings("scheduled-tasks")) } label: { Label("Settings", systemImage: "gearshape") }
                    .buttonStyle(IslandFooterButton())
            }
        }
        .padding(.horizontal, 18)
        .padding(.top, 10)
        .padding(.bottom, 14)
    }

    private func section(_ title: String) -> some View {
        Text(title).font(.system(size: 10, weight: .bold)).foregroundStyle(.white.opacity(0.42)).kerning(0.8)
    }

    private func row(_ item: LiveItem) -> some View {
        HStack(spacing: 10) {
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(LinearGradient(colors: [item.tone.color, item.tone.color.opacity(0.6)], startPoint: .topLeading, endPoint: .bottomTrailing))
                .frame(width: 30, height: 30)
                .overlay(Image(systemName: item.symbol).font(.system(size: 12, weight: .bold)).foregroundStyle(.white))
            VStack(alignment: .leading, spacing: 2) {
                Text(item.title).font(.system(size: 13, weight: .semibold)).foregroundStyle(.white).lineLimit(1)
                Text(item.detail).font(.system(size: 11.5)).foregroundStyle(.white.opacity(0.6)).lineLimit(1)
                    .id(item.detail)
                    .transition(.asymmetric(insertion: .move(edge: .bottom).combined(with: .opacity), removal: .opacity))
            }
            Spacer(minLength: 6)
            if let action = item.actionTitle {
                Button(action) { controller.open(item.target) }
                    .buttonStyle(IslandPillButton(tint: item.tone.color))
            } else if let start = item.startedAt, item.tone == .working {
                Text(timerInterval: start...Date.distantFuture, countsDown: false)
                    .font(.system(size: 12, weight: .semibold, design: .rounded).monospacedDigit())
                    .foregroundStyle(.white.opacity(0.8))
                    .frame(width: 52, alignment: .trailing)
            }
        }
        .padding(8)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Color.white.opacity(0.06)))
        .contentShape(Rectangle())
        .onTapGesture { controller.open(item.target) }
        .animation(IslandMotion.change, value: item.detail)
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

private struct IslandFooterButton: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 11.5, weight: .medium))
            .labelStyle(.titleAndIcon)
            .padding(.horizontal, 9).padding(.vertical, 5)
            .background(Capsule().fill(Color.white.opacity(configuration.isPressed ? 0.16 : 0.08)))
            .foregroundStyle(.white.opacity(0.85))
    }
}
