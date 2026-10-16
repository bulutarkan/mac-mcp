import AppKit
import SwiftUI

/// Settings › Scheduled Tasks (#175): AI tasks and scripts that run on their own. The server keeps the
/// tasks and runs them (scheduled_tasks.py, scheduled_task_runner.py); this view lists them, edits them
/// and is the only place a task an AI app suggested can be approved.

// MARK: - Look

private enum TaskStyle {
    static let aiGradient = LinearGradient(colors: [Color(red: 0.48, green: 0.38, blue: 1.0), Color(red: 0.23, green: 0.63, blue: 1.0)],
                                           startPoint: .topLeading, endPoint: .bottomTrailing)
    static let scriptGradient = LinearGradient(colors: [Color(red: 0.17, green: 0.71, blue: 0.45), Color(red: 0.11, green: 0.56, blue: 0.54)],
                                               startPoint: .topLeading, endPoint: .bottomTrailing)
    static let accentStrip = LinearGradient(colors: [Color.accentColor.opacity(0.20), Color.accentColor.opacity(0.05)],
                                            startPoint: .leading, endPoint: .trailing)
}

struct TaskIcon: View {
    let ai: Bool
    var size: CGFloat = 34

    var body: some View {
        RoundedRectangle(cornerRadius: size * 0.27, style: .continuous)
            .fill(ai ? TaskStyle.aiGradient : TaskStyle.scriptGradient)
            .frame(width: size, height: size)
            .overlay(
                Image(systemName: ai ? "sparkles" : "chevron.left.forwardslash.chevron.right")
                    .font(.system(size: size * 0.44, weight: .semibold))
                    .foregroundStyle(.white)
            )
            .shadow(color: (ai ? Color.purple : Color.green).opacity(0.25), radius: 4, y: 2)
            .accessibilityLabel(ai ? "AI task" : "Script")
    }
}

private struct CardBackground: ViewModifier {
    var highlight: Color?
    var hovering = false

    func body(content: Content) -> some View {
        content
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
            .background(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .fill(Color(nsColor: .controlBackgroundColor).opacity(hovering ? 1 : 0.75))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .strokeBorder(highlight?.opacity(0.55) ?? Color.primary.opacity(hovering ? 0.16 : 0.08), lineWidth: 1)
            )
    }
}

private extension View {
    func taskCard(highlight: Color? = nil, hovering: Bool = false) -> some View {
        modifier(CardBackground(highlight: highlight, hovering: hovering))
    }
}

// MARK: - Pane

struct ScheduledTasksPane: View {
    @ObservedObject var state: AppState

    @MacMCPState private var tasks: [ScheduledTask] = []
    @MacMCPState private var upNext: [[String: Any]] = []
    @MacMCPState private var agents: [[String: Any]] = []
    @MacMCPState private var defaultAgent: String?
    @MacMCPState private var observationPaused = false
    @MacMCPState private var loaded = false
    @MacMCPState private var message = ""
    @MacMCPState private var openTaskID: String?
    @MacMCPState private var editor: TaskEditorModel?
    @MacMCPState private var reviewing: ScheduledTask?
    @MacMCPState private var hovered: String?

    var body: some View {
        ZStack {
            if let id = openTaskID, let task = tasks.first(where: { $0.id == id }) {
                TaskDetailView(state: state, task: task, agents: agents,
                               onBack: { openTaskID = nil },
                               onEdit: { editor = TaskEditorModel(task: task) },
                               onReview: { reviewing = task },
                               onChanged: { await load() })
                    .transition(.move(edge: .trailing).combined(with: .opacity))
            } else {
                list.transition(.move(edge: .leading).combined(with: .opacity))
            }
        }
        .animation(.easeInOut(duration: 0.18), value: openTaskID)
        .task { await load() }
        .task {
            // Keep "Running now" and new results current while the page is open.
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 4_000_000_000)
                await load()
            }
        }
        .sheet(item: Binding(get: { editor }, set: { editor = $0 })) { model in
            TaskEditorSheet(state: state, model: model, agents: agents, defaultAgent: defaultAgent) { saved in
                editor = nil
                if let saved { openTaskID = saved }
                Task { await load() }
            }
        }
        .sheet(item: Binding(get: { reviewing }, set: { reviewing = $0 })) { task in
            TaskReviewSheet(state: state, task: task) {
                reviewing = nil
                Task { await load() }
            }
        }
    }

    private var list: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 3) {
                        HStack(spacing: 8) {
                            Text("Scheduled Tasks").font(.title2.weight(.semibold))
                            Text("Experimental")
                                .font(.caption2.weight(.semibold))
                                .padding(.horizontal, 7).padding(.vertical, 2)
                                .background(Capsule().fill(Color.accentColor.opacity(0.15)))
                                .foregroundStyle(Color.accentColor)
                        }
                        Text("AI tasks and scripts that run on their own, on the schedule you choose.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button {
                        editor = TaskEditorModel()
                    } label: {
                        Label("New Task", systemImage: "plus")
                    }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.large)
                    .disabled(!loaded)
                }

                if loaded, !message.isEmpty {
                    Label(message, systemImage: "exclamationmark.triangle")
                        .font(.caption).foregroundStyle(.secondary)
                }

                if observationPaused, tasks.contains(where: { $0.isAI && $0.state == "active" }) {
                    Label("Observation is paused in Privacy, so AI tasks are skipped until you turn it back on. Scripts still run.",
                          systemImage: "pause.circle")
                        .font(.caption)
                        .padding(10)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(RoundedRectangle(cornerRadius: 9).fill(Color.orange.opacity(0.12)))
                }

                if let next = upNext.first, let at = next["at"] as? Double {
                    HStack(spacing: 8) {
                        Image(systemName: "clock").foregroundStyle(Color.accentColor)
                        Text("Up next: ").fontWeight(.semibold) + Text("\(TaskTime.upcoming(Date(timeIntervalSince1970: at))) · \(next["name"] as? String ?? "")")
                    }
                    .font(.callout)
                    .padding(.horizontal, 12).padding(.vertical, 9)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(TaskStyle.accentStrip))
                    .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(Color.accentColor.opacity(0.35)))
                }

                if !loaded {
                    if message.isEmpty {
                        ProgressView().controlSize(.small).frame(maxWidth: .infinity)
                    } else {
                        LoadProblem(message: message) { await load() }
                    }
                } else if tasks.isEmpty {
                    emptyState
                } else {
                    VStack(spacing: 9) {
                        ForEach(tasks) { task in card(task) }
                    }
                }
            }
            .padding(22)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var emptyState: some View {
        VStack(spacing: 14) {
            HStack(spacing: -8) {
                TaskIcon(ai: true, size: 46)
                TaskIcon(ai: false, size: 46).offset(y: 8)
            }
            .padding(.top, 18)
            Text("Nothing scheduled yet").font(.headline)
            Text("Have an agent write your Monday report, or run a script every evening. You can also ask your AI app to suggest a task; it waits here for your approval.")
                .font(.callout).foregroundStyle(.secondary).multilineTextAlignment(.center)
                .frame(maxWidth: 420)
            HStack(spacing: 8) {
                templateButton("Weekly report", ai: true) { model in
                    model.name = "Weekly report"
                    model.prompt = "Summarize last week's results and compare them with the week before. Point out anything unusual."
                    model.when = .weekly
                    model.weekdays = [0]
                }
                templateButton("Morning digest", ai: true) { model in
                    model.name = "Morning digest"
                    model.prompt = "Give me a short digest of what changed in my projects since yesterday."
                    model.when = .weekly
                    model.weekdays = [0, 1, 2, 3, 4]
                    model.time = TaskEditorModel.clock(8, 30)
                }
                templateButton("Evening script", ai: false) { model in
                    model.name = "Evening script"
                    model.when = .daily
                    model.time = TaskEditorModel.clock(18, 0)
                }
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 26)
        .taskCard()
    }

    private func templateButton(_ title: String, ai: Bool, configure: @escaping (TaskEditorModel) -> Void) -> some View {
        Button {
            let model = TaskEditorModel()
            model.isAI = ai
            configure(model)
            editor = model
        } label: {
            HStack(spacing: 6) {
                Image(systemName: ai ? "sparkles" : "chevron.left.forwardslash.chevron.right")
                Text(title)
            }
        }
        .buttonStyle(.bordered)
    }

    @ViewBuilder
    private func card(_ task: ScheduledTask) -> some View {
        let waiting = task.proposal != nil
        let hovering = hovered == task.id
        HStack(spacing: 12) {
            TaskIcon(ai: (task.proposal?["kind"] as? String ?? task.kind) == "ai")
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(waiting && !task.approved ? (task.proposal?["name"] as? String ?? task.name) : task.name)
                        .font(.body.weight(.semibold)).lineLimit(1)
                    if waiting {
                        Text(task.approved ? "Change suggested" : "Needs your approval")
                            .font(.caption2.weight(.semibold))
                            .padding(.horizontal, 7).padding(.vertical, 2)
                            .background(Capsule().fill(Color.orange.opacity(0.16)))
                            .foregroundStyle(Color.orange)
                    }
                }
                subtitle(task).font(.caption).lineLimit(1)
            }
            Spacer(minLength: 8)
            if waiting {
                Button("Review") { reviewing = task }
                    .buttonStyle(.borderedProminent)
                    .tint(.orange)
            } else {
                Image(systemName: "chevron.right").font(.caption.weight(.semibold)).foregroundStyle(.tertiary)
            }
        }
        .taskCard(highlight: waiting ? .orange : nil, hovering: hovering)
        .opacity(task.paused ? 0.55 : 1)
        .onHover { inside in hovered = inside ? task.id : (hovered == task.id ? nil : hovered) }
        .accessibilityElement(children: .combine)
        .activatable(task.approved ? "Opens the task" : "Reviews the suggestion") {
            if task.approved { openTaskID = task.id } else { reviewing = task }
        }
    }

    private func subtitle(_ task: ScheduledTask) -> Text {
        if let proposal = task.proposal, !task.approved {
            let by = proposal["proposed_by"] as? String ?? "An AI app"
            return Text("\(by) suggested this · \(proposal["schedule_text"] as? String ?? "")").foregroundColor(.secondary)
        }
        let schedule = Text(task.scheduleText).foregroundColor(.secondary)
        if task.paused { return schedule + Text(" · Paused").foregroundColor(.secondary) }
        if task.state == "running" { return schedule + Text(" · Running now").foregroundColor(.accentColor) }
        if let run = task.lastRun {
            return schedule + Text(" · ").foregroundColor(.secondary) + Text(run.shortLine).foregroundColor(run.color)
        }
        if task.watchesFolder { return schedule + Text(" · Waiting for a file").foregroundColor(.secondary) }
        if task.state == "finished" { return schedule + Text(" · No more runs").foregroundColor(.secondary) }
        if let next = task.nextRunAt { return schedule + Text(" · First run \(TaskTime.upcoming(next))").foregroundColor(.secondary) }
        return schedule
    }

    private func load() async {
        let result = await state.taskAPI("dashboard/api/scheduled-tasks")
        guard result.ok else {
            message = result.object["message"] as? String ?? "Start Mac MCP to see your scheduled tasks."
            return
        }
        tasks = (result.object["tasks"] as? [[String: Any]] ?? []).map(ScheduledTask.init)
        upNext = result.object["up_next"] as? [[String: Any]] ?? []
        agents = result.object["agents"] as? [[String: Any]] ?? []
        defaultAgent = result.object["default_agent"] as? String
        observationPaused = ((result.object["privacy"] as? [String: Any])?["paused"] as? Bool) == true
        message = ""
        loaded = true
        if let id = openTaskID, !tasks.contains(where: { $0.id == id }) { openTaskID = nil }
    }
}

// MARK: - Detail

private struct TaskDetailView: View {
    @ObservedObject var state: AppState
    let task: ScheduledTask
    let agents: [[String: Any]]
    let onBack: () -> Void
    let onEdit: () -> Void
    let onReview: () -> Void
    let onChanged: () async -> Void

    @MacMCPState private var runs: [TaskRun] = []
    @MacMCPState private var runsProblem: String?
    @MacMCPState private var openRun: TaskRun?
    @MacMCPState private var confirmDelete = false
    @MacMCPState private var busy = false
    @MacMCPState private var message = ""

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                Button(action: onBack) {
                    Label("Scheduled Tasks", systemImage: "chevron.left").font(.callout)
                }
                .buttonStyle(.plain)
                .foregroundStyle(Color.accentColor)

                HStack(alignment: .top, spacing: 14) {
                    TaskIcon(ai: task.isAI, size: 48)
                    VStack(alignment: .leading, spacing: 4) {
                        Text(task.name).font(.title2.weight(.semibold))
                        Text(([task.scheduleText, task.isAI ? "AI task" : "Script"] + (task.isAI ? [agentName] : [])).joined(separator: " · "))
                            .font(.callout).foregroundStyle(.secondary)
                    }
                    Spacer()
                    if task.paused {
                        Text("Paused").font(.caption.weight(.semibold))
                            .padding(.horizontal, 8).padding(.vertical, 3)
                            .background(Capsule().fill(Color.secondary.opacity(0.15)))
                    }
                }

                if !task.description.isEmpty {
                    Text(task.description).font(.callout).fixedSize(horizontal: false, vertical: true)
                }

                if task.proposal != nil {
                    HStack {
                        Label("\(task.proposal?["proposed_by"] as? String ?? "An AI app") suggested a change. The task runs as before until you approve it.",
                              systemImage: "exclamationmark.bubble")
                            .font(.caption)
                        Spacer()
                        Button("Review", action: onReview).buttonStyle(.borderedProminent).tint(.orange)
                    }
                    .padding(10)
                    .background(RoundedRectangle(cornerRadius: 9).fill(Color.orange.opacity(0.12)))
                }

                HStack(spacing: 8) {
                    Button {
                        act("run_now")
                    } label: {
                        Label(task.state == "running" ? "Running…" : "Run now", systemImage: "play.fill")
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(busy || task.state == "running")
                    Button("Edit", action: onEdit)
                    Button(task.paused ? "Resume" : "Pause") { act(task.paused ? "resume" : "pause") }
                        .disabled(busy)
                    Spacer()
                    Button(role: .destructive) { confirmDelete = true } label: { Label("Delete", systemImage: "trash") }
                        .disabled(busy)
                }
                if !message.isEmpty {
                    Text(message).font(.caption).foregroundStyle(.secondary)
                }

                facts

                VStack(alignment: .leading, spacing: 6) {
                    Text("RECENT RUNS").font(.caption2.weight(.semibold)).foregroundStyle(.secondary).kerning(0.6)
                    if let problem = runsProblem, runs.isEmpty {
                        LoadProblem(message: problem) { await loadRuns() }
                    } else if runs.isEmpty {
                        Text(task.watchesFolder ? "No runs yet. It runs when a new file arrives."
                             : task.nextRunAt.map { "No runs yet. The first one is \(TaskTime.upcoming($0))." } ?? "No runs yet.")
                            .font(.callout).foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .taskCard()
                    } else {
                        VStack(spacing: 0) {
                            ForEach(Array(runs.enumerated()), id: \.element.id) { index, run in
                                if index > 0 { Divider() }
                                runRow(run)
                            }
                        }
                        .taskCard()
                    }
                }
            }
            .padding(22)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .task(id: task.raw["updated_at"] as? Double ?? 0) { await loadRuns() }
        .task {
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 4_000_000_000)
                await loadRuns()
            }
        }
        .sheet(item: Binding(get: { openRun }, set: { openRun = $0 })) { run in
            RunReportSheet(state: state, taskName: task.name, run: run)
        }
        .alert("Delete “\(task.name)”?", isPresented: $confirmDelete) {
            Button("Delete", role: .destructive) { act("delete") }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("The task and its run history are removed. Files its runs created stay where they are.")
        }
    }

    private var agentName: String {
        guard let provider = task.provider else { return "Default agent" }
        return agents.first(where: { ($0["id"] as? String) == provider })?["name"] as? String ?? provider.capitalized
    }

    private var facts: some View {
        VStack(spacing: 0) {
            if task.watchesFolder {
                factRow("tray.and.arrow.down", "Runs when", task.paused ? "Paused" : "A new file arrives, once for each file")
            } else {
                factRow("calendar", "Next run", task.paused ? "Paused" : task.nextRunAt.map { TaskTime.upcoming($0).capitalizedFirst } ?? "No more runs")
            }
            Divider()
            if task.isAI {
                if let step = task.raw["data_step"] as? [String: Any] {
                    factRow("tablecells", "Data first", Self.program(step), mono: true, lines: 2)
                    Divider()
                }
                factRow("text.bubble", "Asks the agent", task.raw["prompt"] as? String ?? "", lines: 4)
            } else if let command = task.raw["command"] as? String {
                factRow("terminal", "Runs", command, mono: true)
            } else {
                factRow("doc.text", "Runs", (task.raw["script"] as? String).map { URL(fileURLWithPath: $0).lastPathComponent } ?? "", mono: true)
            }
            Divider()
            factRow("folder", "Works in", task.cwd.map(Self.tilde) ?? (task.isAI ? "Home folder" : "The script's folder"))
            Divider()
            if let checks = Self.checks(task.raw["verify"]) {
                factRow("checkmark.seal", "Checks", checks, mono: true, lines: 5)
                Divider()
            }
            if let limit = task.usageLimit {
                factRow("gauge.with.dots.needle.67percent", "Usage limit", "Stops at " + ScheduledTask.describeLimit(limit))
                Divider()
            }
            factRow("bell", "When it finishes", task.notify ? "Notifies you" : "No notification")
        }
        .taskCard()
    }

    /// A script with its arguments, or a command, as one line.
    static func program(_ step: [String: Any]) -> String {
        if let command = step["command"] as? String, !command.isEmpty { return command }
        let name = (step["script"] as? String).map { URL(fileURLWithPath: $0).lastPathComponent } ?? ""
        return [name, step["args"] as? String ?? ""].filter { !$0.isEmpty }.joined(separator: " ")
    }

    /// The expected files of a task, one per line, or nil when it has none.
    static func checks(_ value: Any?) -> String? {
        guard let checks = value as? [[String: Any]], !checks.isEmpty else { return nil }
        return checks.map { check in
            var line = tilde(check["path"] as? String ?? "")
            let least = check["min_bytes"] as? Int ?? 1
            if least > 1 { line += " · at least \(ByteCountFormatter.string(fromByteCount: Int64(least), countStyle: .file))" }
            if (check["written_this_run"] as? Bool) ?? true { line += " · written by the run" }
            return line
        }.joined(separator: "\n")
    }

    static func tilde(_ path: String) -> String {
        let home = NSHomeDirectory()
        return path.hasPrefix(home) ? "~" + path.dropFirst(home.count) : path
    }

    private func factRow(_ symbol: String, _ title: String, _ value: String, mono: Bool = false, lines: Int = 1) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Image(systemName: symbol).foregroundStyle(.secondary).frame(width: 16)
            Text(title).foregroundStyle(.secondary).frame(width: 110, alignment: .leading)
            Text(value)
                .font(mono ? .system(.callout, design: .monospaced) : .callout)
                .lineLimit(lines)
                .textSelection(.enabled)
            Spacer(minLength: 0)
        }
        .font(.callout)
        .padding(.vertical, 7)
    }

    @ViewBuilder
    private func runRow(_ run: TaskRun) -> some View {
        let row = HStack(alignment: .top, spacing: 10) {
            Image(systemName: run.symbol).foregroundStyle(run.color).font(.body)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Text(TaskTime.stamp(run.scheduledFor ?? run.startedAt)).font(.callout.weight(.semibold))
                    Text(runMeta(run)).font(.caption).foregroundStyle(.secondary)
                }
                if let text = runText(run) {
                    Text(text).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                }
            }
            Spacer()
            if run.status != "skipped", run.status != "running" {
                Image(systemName: "chevron.right").font(.caption.weight(.semibold)).foregroundStyle(.tertiary)
            }
        }
        .padding(.vertical, 9)
        .accessibilityElement(children: .combine)
        if run.status == "skipped" {
            row
        } else {
            row.activatable("Opens the run report") { openRun = run }
        }
    }

    private func runMeta(_ run: TaskRun) -> String {
        var parts: [String] = []
        if run.trigger == "manual" { parts.append("Run now") }
        if let file = run.triggerFileName { parts.append(file) }
        switch run.status {
        case "running": parts.append("Running…")
        case "skipped": parts.append(run.skippedCount > 1 ? "Skipped \(run.skippedCount) runs" : "Skipped")
        case "done":
            if let d = run.duration { parts.append(TaskTime.duration(d)) }
            if run.checkFailed { parts.append("Check failed") } else if run.verificationState == "verified" { parts.append("Checked") }
        case "timed_out": parts.append("Stopped at the time limit")
        default: parts.append("Failed" + (run.duration.map { " after \(TaskTime.duration($0))" } ?? ""))
        }
        return parts.joined(separator: " · ")
    }

    private func runText(_ run: TaskRun) -> String? {
        switch run.status {
        case "skipped": return (run.reason ?? "Skipped") + ". Run it now if you still need it."
        case "done": return run.summary
        case "running": return task.isAI ? "The agent is working on it." : "The script is running."
        default: return [run.reason, run.summary].compactMap { $0 }.joined(separator: " — ")
        }
    }

    private func loadRuns() async {
        let result = await state.taskAPI("dashboard/api/scheduled-tasks/runs?task_id=\(task.id)")
        if result.ok {
            runs = (result.object["runs"] as? [[String: Any]] ?? []).map(TaskRun.init)
            runsProblem = nil
        } else {
            runsProblem = "The run history could not be loaded. " + (result.object["message"] as? String ?? "")
        }
    }

    private func act(_ action: String) {
        busy = true
        Task {
            let result = await state.taskAPI("dashboard/api/scheduled-tasks/action", body: ["id": task.id, "action": action])
            busy = false
            message = result.ok ? (action == "run_now" ? "Started. The result appears below when it finishes." : "") : (result.object["message"] as? String ?? "Couldn’t do that.")
            if action == "delete", result.ok { onBack() }
            await onChanged()
            await loadRuns()
        }
    }
}

private extension String {
    var capitalizedFirst: String { prefix(1).uppercased() + dropFirst() }
}

// MARK: - New / Edit

enum TaskWhen: String, CaseIterable, Identifiable {
    case once, daily, weekly, monthly, hours, file
    var id: String { rawValue }
    var title: String {
        switch self {
        case .once: return "Once"
        case .daily: return "Daily"
        case .weekly: return "Weekly"
        case .monthly: return "Monthly"
        case .hours: return "Every few hours"
        case .file: return "New file"
        }
    }
}

final class TaskEditorModel: ObservableObject, Identifiable {
    let id = UUID()
    var taskID: String?
    @Published var isAI = true
    @Published var name = ""
    @Published var description = ""
    @Published var prompt = ""
    @Published var provider = ""
    @Published var model = ""
    @Published var cwd = ""
    @Published var script = ""
    @Published var command = ""
    @Published var args = ""
    @Published var interpreter = ""
    @Published var dataOn = false
    @Published var dataUsesCommand = false
    @Published var dataScript = ""
    @Published var dataCommand = ""
    @Published var dataArgs = ""
    @Published var dataInterpreter = ""
    @Published var when: TaskWhen = .weekly
    @Published var time = TaskEditorModel.clock(9, 0)
    @Published var onceAt = Calendar.current.date(byAdding: .day, value: 1, to: TaskEditorModel.clock(9, 0)) ?? Date()
    @Published var weekdays: Set<Int> = [0]
    @Published var monthDay = 1
    @Published var everyHours = 3
    @Published var cron = ""
    /// .file (#193): the folder to watch and, optionally, which files count ("pdf, Invoice*").
    @Published var watchFolder = ""
    @Published var watchMatch = ""
    /// The usage limit (#211): "none", "tokens" or "cost", and its amount as typed.
    @Published var usageKind = "none"
    @Published var usageAmount = ""
    @Published var notify = true
    @Published var timeoutMin = 0
    @Published var showAdvanced = false
    /// The first output check (#198) is edited here; checks an AI app added beyond it are kept as they are.
    @Published var expectedFile = ""
    private var firstCheck: [String: Any] = [:]
    private var otherChecks: [[String: Any]] = []

    init() {}

    init(task: ScheduledTask) {
        taskID = task.id
        let raw = task.raw
        isAI = task.isAI
        name = task.name
        description = task.description
        prompt = raw["prompt"] as? String ?? ""
        provider = raw["provider"] as? String ?? ""
        model = raw["model"] as? String ?? ""
        cwd = raw["cwd"] as? String ?? ""
        script = raw["script"] as? String ?? ""
        command = raw["command"] as? String ?? ""
        args = raw["args"] as? String ?? ""
        interpreter = raw["interpreter"] as? String ?? ""
        if let step = raw["data_step"] as? [String: Any] {
            dataOn = true
            dataScript = step["script"] as? String ?? ""
            dataCommand = step["command"] as? String ?? ""
            dataUsesCommand = !dataCommand.isEmpty
            dataArgs = step["args"] as? String ?? ""
            dataInterpreter = step["interpreter"] as? String ?? ""
        }
        notify = task.notify
        timeoutMin = raw["timeout_min"] as? Int ?? 0
        if let checks = raw["verify"] as? [[String: Any]], let first = checks.first {
            firstCheck = first
            expectedFile = TaskRun.displayPath(first["path"] as? String ?? "")
            otherChecks = Array(checks.dropFirst())
            showAdvanced = true
        }
        let schedule = task.schedule
        let type = schedule["type"] as? String ?? "weekly"
        if let clock = schedule["time"] as? String ?? schedule["start"] as? String { time = Self.parse(clock) }
        switch type {
        case "once":
            when = .once
            if let at = schedule["at"] as? String, let date = Self.isoMinute.date(from: at) { onceAt = date }
        case "daily": when = .daily
        case "monthly": when = .monthly; monthDay = schedule["day"] as? Int ?? 1
        case "hours": when = .hours; everyHours = schedule["every"] as? Int ?? 3
        case "cron": cron = schedule["expr"] as? String ?? ""; showAdvanced = true
        case "folder":
            when = .file
            watchFolder = schedule["folder"] as? String ?? ""
            watchMatch = schedule["match"] as? String ?? ""
        default: when = .weekly; weekdays = Set(schedule["weekdays"] as? [Int] ?? [0])
        }
        if let limit = task.usageLimit {
            if let tokens = limit["max_tokens"] as? Int {
                usageKind = "tokens"; usageAmount = String(tokens)
            } else if let cost = limit["max_cost_usd"] as? Double {
                usageKind = "cost"; usageAmount = String(format: "%.2f", cost)
            }
            showAdvanced = true
        }
        if !command.isEmpty || !args.isEmpty || !interpreter.isEmpty || !model.isEmpty { showAdvanced = true }
    }

    static func clock(_ hour: Int, _ minute: Int) -> Date {
        Calendar.current.date(bySettingHour: hour, minute: minute, second: 0, of: Date()) ?? Date()
    }

    static func parse(_ clock: String) -> Date {
        let parts = clock.split(separator: ":").compactMap { Int($0) }
        return parts.count == 2 ? Self.clock(parts[0], parts[1]) : Self.clock(9, 0)
    }

    static let isoMinute: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd'T'HH:mm"
        return formatter
    }()

    private func hhmm(_ date: Date) -> String {
        let parts = Calendar.current.dateComponents([.hour, .minute], from: date)
        return String(format: "%02d:%02d", parts.hour ?? 0, parts.minute ?? 0)
    }

    var schedule: [String: Any] {
        let custom = cron.trimmingCharacters(in: .whitespaces)
        if !custom.isEmpty { return ["type": "cron", "expr": custom] }
        switch when {
        case .once: return ["type": "once", "at": Self.isoMinute.string(from: onceAt)]
        case .daily: return ["type": "daily", "time": hhmm(time)]
        case .weekly: return ["type": "weekly", "time": hhmm(time), "weekdays": weekdays.sorted()]
        case .monthly: return ["type": "monthly", "time": hhmm(time), "day": monthDay]
        case .hours: return ["type": "hours", "every": everyHours, "start": hhmm(time)]
        case .file: return ["type": "folder", "folder": watchFolder, "match": watchMatch]
        }
    }

    /// The usage limit as the server reads it; empty removes it on an edit.
    var usageLimit: [String: Any] {
        let amount = usageAmount.trimmingCharacters(in: .whitespaces).replacingOccurrences(of: ",", with: "")
        switch usageKind {
        case "tokens": return Int(amount).map { ["max_tokens": $0] } ?? [:]
        case "cost": return Double(amount).map { ["max_cost_usd": $0] } ?? [:]
        default: return [:]
        }
    }

    var usageLimitReady: Bool { usageKind == "none" || !usageLimit.isEmpty }

    var definition: [String: Any] {
        var out: [String: Any] = [
            "name": name, "description": description, "kind": isAI ? "ai" : "script",
            "schedule": schedule, "notify": notify, "cwd": cwd,
        ]
        if timeoutMin > 0 { out["timeout_min"] = timeoutMin }
        var checks = otherChecks
        let expected = expectedFile.trimmingCharacters(in: .whitespaces)
        if !expected.isEmpty {
            var first = firstCheck
            first["path"] = expected
            checks.insert(first, at: 0)
        }
        out["verify"] = checks  // empty removes the checks on an edit
        if isAI {
            out["prompt"] = prompt
            out["provider"] = provider
            out["model"] = model
            out["data_step"] = dataOn ? dataStep : [String: Any]()  // empty removes it on an edit
            out["usage_limit"] = usageLimit
        } else {
            let useCommand = !command.trimmingCharacters(in: .whitespaces).isEmpty
            out["script"] = useCommand ? "" : script
            out["command"] = useCommand ? command : ""
            out["args"] = useCommand ? "" : args
            out["interpreter"] = useCommand ? "" : interpreter
        }
        return out
    }

    /// The data step as the server reads it: a file with its arguments, or a command.
    var dataStep: [String: Any] {
        dataUsesCommand ? ["command": dataCommand]
            : ["script": dataScript, "args": dataArgs, "interpreter": dataInterpreter]
    }

    var dataStepReady: Bool {
        !(dataUsesCommand ? dataCommand : dataScript).trimmingCharacters(in: .whitespaces).isEmpty
    }

    var previewKey: String {
        let data = (try? JSONSerialization.data(withJSONObject: [
            "s": schedule, "f": script, "i": interpreter, "k": isAI, "a": args, "c": command,
            "d": dataOn ? dataStep : [:],
        ], options: [.sortedKeys])) ?? Data()
        return String(decoding: data, as: UTF8.self)
    }
}

private struct TaskEditorSheet: View {
    @ObservedObject var state: AppState
    @ObservedObject var model: TaskEditorModel
    let agents: [[String: Any]]
    let defaultAgent: String?
    let done: (String?) -> Void

    @MacMCPState private var sentence = ""
    @MacMCPState private var firstRun: Date?
    @MacMCPState private var scheduleProblem = ""
    @MacMCPState private var scriptInfo: [String: Any]?
    @MacMCPState private var dates: [[String: String]] = []
    @MacMCPState private var dataPreview: [String: Any]?
    @MacMCPState private var argsPreview: [String: Any]?
    @MacMCPState private var error = ""
    @MacMCPState private var saving = false

    private let weekdayLetters = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text(model.taskID == nil ? "New Task" : "Edit Task").font(.title3.weight(.semibold))
                Spacer()
            }
            .padding(.horizontal, 22).padding(.top, 18).padding(.bottom, 8)

            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    kindPicker
                    field("Name") {
                        TextField(model.isAI ? "e.g. Weekly ad report" : "e.g. Export leads to Excel", text: $model.name)
                            .textFieldStyle(.roundedBorder)
                    }
                    field("Description", optional: true) {
                        TextField("What it's for, so you remember later", text: $model.description)
                            .textFieldStyle(.roundedBorder)
                    }
                    if model.isAI { aiFields } else { scriptFields }
                    whenSection
                    Toggle("Notify me when it finishes", isOn: $model.notify)
                    advanced
                    if !error.isEmpty {
                        Label(error, systemImage: "exclamationmark.circle.fill")
                            .font(.callout).foregroundStyle(.red)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(.horizontal, 22)
                .padding(.bottom, 16)
            }

            Divider()
            HStack {
                Spacer()
                Button("Cancel") { done(nil) }.keyboardShortcut(.cancelAction)
                Button(model.taskID == nil ? "Create Task" : "Save") { save() }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
                    .disabled(saving || !canSave)
            }
            .padding(.horizontal, 22).padding(.vertical, 14)
            .background(Color(nsColor: .windowBackgroundColor).opacity(0.6))
        }
        .frame(width: 560, height: 700)
        .task(id: model.previewKey) { await preview() }
    }

    private var canSave: Bool {
        guard !model.name.trimmingCharacters(in: .whitespaces).isEmpty, scheduleProblem.isEmpty else { return false }
        if model.when == .file && model.cron.isEmpty && model.watchFolder.isEmpty { return false }
        if model.isAI && !model.usageLimitReady { return false }
        if model.isAI {
            return !model.prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                && (!model.dataOn || model.dataStepReady) && (dataPreview?["ok"] as? Bool) != false
        }
        if model.when == .weekly && model.cron.isEmpty && model.weekdays.isEmpty { return false }
        return !model.script.isEmpty || !model.command.trimmingCharacters(in: .whitespaces).isEmpty
    }

    private var kindPicker: some View {
        HStack(spacing: 10) {
            kindCard(ai: true, title: "AI task", detail: "Tell an agent what to do")
            kindCard(ai: false, title: "Script", detail: "Run a file or command")
        }
    }

    private func kindCard(ai: Bool, title: String, detail: String) -> some View {
        let on = model.isAI == ai
        return Button {
            model.isAI = ai
        } label: {
            HStack(spacing: 10) {
                TaskIcon(ai: ai, size: 30)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).font(.callout.weight(.semibold))
                    Text(detail).font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Image(systemName: on ? "checkmark.circle.fill" : "circle")
                    .foregroundStyle(on ? Color.accentColor : Color.secondary.opacity(0.5))
            }
            .padding(10)
            .background(RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(on ? Color.accentColor.opacity(0.12) : Color(nsColor: .controlBackgroundColor)))
            .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous)
                .strokeBorder(on ? Color.accentColor.opacity(0.8) : Color.primary.opacity(0.1), lineWidth: on ? 1.5 : 1))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(model.taskID != nil)
        .accessibilityAddTraits(on ? .isSelected : [])
    }

    private func field<Content: View>(_ title: String, optional: Bool = false, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            (Text(title).foregroundColor(.secondary) + Text(optional ? "  (optional)" : "").foregroundColor(Color.secondary.opacity(0.6)))
                .font(.caption)
            content()
        }
    }

    private var aiFields: some View {
        VStack(alignment: .leading, spacing: 14) {
            field("What should the agent do?") {
                TextEditor(text: $model.prompt)
                    .font(.callout)
                    .frame(minHeight: 84)
                    .padding(4)
                    .background(RoundedRectangle(cornerRadius: 6).fill(Color(nsColor: .textBackgroundColor)))
                    .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(Color.primary.opacity(0.15)))
            }
            dataSection
            HStack(alignment: .top, spacing: 12) {
                field("Agent") {
                    Picker("", selection: $model.provider) {
                        Text("Default agent").tag("")
                        ForEach(Array(agents.enumerated()), id: \.offset) { _, agent in
                            Text(agent["name"] as? String ?? "").tag(agent["id"] as? String ?? "")
                        }
                    }
                    .labelsHidden()
                    .fixedSize()
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                field("Works in") { folderButton(placeholder: "Home folder") }
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            if agents.isEmpty {
                Label("No agent is turned on. Turn one on in Settings › Agents first.", systemImage: "exclamationmark.triangle")
                    .font(.caption).foregroundStyle(.orange)
            }
        }
    }

    private var scriptFields: some View {
        VStack(alignment: .leading, spacing: 14) {
            if model.command.trimmingCharacters(in: .whitespaces).isEmpty {
                field("Script") {
                    if model.script.isEmpty {
                        Button { chooseScript() } label: {
                            VStack(spacing: 6) {
                                Image(systemName: "doc.badge.plus").font(.title2)
                                Text("Choose a script…").font(.callout.weight(.medium))
                                Text("Shell, Python, Node, AppleScript or any program").font(.caption).foregroundStyle(.secondary)
                            }
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 16)
                            .background(RoundedRectangle(cornerRadius: 10).strokeBorder(style: StrokeStyle(lineWidth: 1.5, dash: [5, 4]))
                                .foregroundStyle(Color.secondary.opacity(0.5)))
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                    } else {
                        HStack(spacing: 10) {
                            TaskIcon(ai: false, size: 30)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(URL(fileURLWithPath: model.script).lastPathComponent).font(.callout.weight(.semibold))
                                Text(scriptLine).font(.caption)
                                    .foregroundStyle((scriptInfo?["ok"] as? Bool) == false ? Color.red : Color.secondary)
                            }
                            Spacer()
                            Button("Change") { chooseScript() }.controlSize(.small)
                        }
                        .padding(10)
                        .background(RoundedRectangle(cornerRadius: 10).fill(Color(nsColor: .controlBackgroundColor)))
                        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Color.primary.opacity(0.1)))
                    }
                }
            } else {
                Label("Runs the command from Advanced instead of a file.", systemImage: "terminal")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    private var scriptLine: String { Self.scriptLine(path: model.script, info: scriptInfo) }

    private static func scriptLine(path: String, info: [String: Any]?) -> String {
        guard let info else { return DetailPath.folder(path) }
        if (info["ok"] as? Bool) == false { return info["message"] as? String ?? "Can’t run this file." }
        var line = info["label"] as? String ?? "Script"
        if let program = info["program"] as? String, info["program_source"] as? String == "venv" {
            line += " · runs with " + TaskDetailView.tilde(program)
        }
        return line + " · " + DetailPath.folder(path)
    }

    // MARK: Data first

    private static let dataTint = Color.green

    @ViewBuilder
    private var dataSection: some View {
        if model.dataOn {
            dataCard.transition(.opacity.combined(with: .move(edge: .top)))
        } else {
            Button {
                withAnimation(.spring(response: 0.32, dampingFraction: 0.86)) { model.dataOn = true }
            } label: {
                HStack(spacing: 10) {
                    dataTile
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Give the agent data from a script first").font(.callout.weight(.medium))
                        Text("Your script gets the numbers; the agent reads and explains them.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Image(systemName: "plus.circle.fill").font(.title3).foregroundStyle(Self.dataTint.opacity(0.85))
                }
                .padding(10)
                .background(RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .strokeBorder(style: StrokeStyle(lineWidth: 1.2, dash: [5, 4]))
                    .foregroundStyle(Color.secondary.opacity(0.45)))
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Give the agent data from a script first")
        }
    }

    private var dataTile: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 8, style: .continuous).fill(Self.dataTint.opacity(0.15)).frame(width: 30, height: 30)
            Image(systemName: "tablecells").font(.system(size: 13, weight: .semibold)).foregroundStyle(Self.dataTint)
        }
    }

    private var dataCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 10) {
                dataTile
                VStack(alignment: .leading, spacing: 1) {
                    Text("Data first").font(.callout.weight(.semibold))
                    Text("Runs before the agent, for up to 10 minutes").font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Picker("", selection: $model.dataUsesCommand) {
                    Text("File").tag(false)
                    Text("Command").tag(true)
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .fixedSize()
                Button {
                    withAnimation(.spring(response: 0.3, dampingFraction: 0.9)) { model.dataOn = false }
                } label: {
                    Image(systemName: "xmark.circle.fill").font(.title3).foregroundStyle(.secondary)
                }
                .buttonStyle(.borderless)
                .help("Remove the data step")
                .accessibilityLabel("Remove the data step")
            }
            if model.dataUsesCommand {
                HStack(spacing: 6) {
                    TextField("e.g. python3 ~/scripts/report.py --since {last_week_start}", text: $model.dataCommand)
                        .textFieldStyle(.roundedBorder)
                        .font(.system(.callout, design: .monospaced))
                    dateMenu($model.dataCommand)
                }
            } else {
                dataScriptRow
                HStack(spacing: 6) {
                    TextField("Arguments, e.g. --since {last_week_start}", text: $model.dataArgs)
                        .textFieldStyle(.roundedBorder)
                        .font(.system(.callout, design: .monospaced))
                    dateMenu($model.dataArgs)
                }
            }
            nextRunLine(dataPreview, source: model.dataUsesCommand ? model.dataCommand : model.dataArgs,
                        prefix: model.dataUsesCommand || model.dataScript.isEmpty ? "" : URL(fileURLWithPath: model.dataScript).lastPathComponent)
            Label("What it prints goes to the agent, and so to its provider. If it fails or prints nothing, the agent doesn’t start and the run says why.",
                  systemImage: "info.circle")
                .font(.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Self.dataTint.opacity(0.06)))
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(Self.dataTint.opacity(0.28)))
    }

    private var dataScriptRow: some View {
        Button { chooseDataScript() } label: {
            HStack(spacing: 10) {
                Image(systemName: model.dataScript.isEmpty ? "doc.badge.plus" : "doc.text")
                    .font(.body).foregroundStyle(.secondary).frame(width: 20)
                VStack(alignment: .leading, spacing: 1) {
                    Text(model.dataScript.isEmpty ? "Choose a script…" : URL(fileURLWithPath: model.dataScript).lastPathComponent)
                        .font(.callout.weight(.medium))
                    let info = dataPreview?["script"] as? [String: Any]
                    Text(model.dataScript.isEmpty ? "Python, shell, Node or any program that prints the data"
                                                  : Self.scriptLine(path: model.dataScript, info: info))
                        .font(.caption)
                        .foregroundStyle((info?["ok"] as? Bool) == false ? Color.red : Color.secondary)
                        .lineLimit(1)
                }
                Spacer()
                if !model.dataScript.isEmpty { Text("Change").font(.caption).foregroundStyle(Color.accentColor) }
            }
            .padding(.horizontal, 10).padding(.vertical, 8)
            .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(Color(nsColor: .controlBackgroundColor)))
            .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous).strokeBorder(Color.primary.opacity(0.1)))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    /// Inserts a date that moves with each run, such as {last_week_start}.
    private func dateMenu(_ text: Binding<String>) -> some View {
        Menu {
            if model.when == .file && model.cron.trimmingCharacters(in: .whitespaces).isEmpty {
                Section("The file that started the run") {
                    Button("Its full path   {file}") {
                        let current = text.wrappedValue
                        let space = current.isEmpty || current.hasSuffix(" ") ? "" : " "
                        text.wrappedValue = current + space + "{file}"
                    }
                }
            }
            Section("Dates that move with each run") {
                ForEach(dates, id: \.self) { choice in
                    Button("\(choice["label"] ?? "")   \(choice["value"] ?? "")") {
                        let current = text.wrappedValue
                        let space = current.isEmpty || current.hasSuffix(" ") ? "" : " "
                        text.wrappedValue = current + space + (choice["token"] ?? "")
                    }
                }
            }
        } label: {
            Image(systemName: "calendar.badge.plus")
        }
        .menuStyle(.borderlessButton)
        .menuIndicator(.hidden)
        .fixedSize()
        .disabled(dates.isEmpty)
        .help("Insert a date that moves with each run")
        .accessibilityLabel("Insert a date")
    }

    /// "Next run: report.py --since 2026-10-06", shown once the text names a date.
    @ViewBuilder
    private func nextRunLine(_ preview: [String: Any]?, source: String, prefix: String) -> some View {
        if let preview, (preview["ok"] as? Bool) == false {
            Label(preview["message"] as? String ?? "", systemImage: "exclamationmark.circle.fill")
                .font(.caption).foregroundStyle(.red)
                .fixedSize(horizontal: false, vertical: true)
        } else if source.contains("{"), let resolved = preview?["next_run"] as? String, resolved != source {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text("Next run").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                Text([prefix, resolved].filter { !$0.isEmpty }.joined(separator: " "))
                    .font(.system(.caption, design: .monospaced))
                    .textSelection(.enabled)
                    .lineLimit(2)
            }
        }
    }

    private var whenSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            field("When") {
                Picker("", selection: $model.when) {
                    ForEach(TaskWhen.allCases) { Text($0.title).tag($0) }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .disabled(!model.cron.trimmingCharacters(in: .whitespaces).isEmpty)
            }
            HStack(spacing: 10) {
                switch model.when {
                case .once:
                    DateField(date: $model.onceAt)
                    TimeField(date: $model.onceAt)
                    Spacer()
                case .weekly:
                    HStack(spacing: 4) {
                        ForEach(0..<7, id: \.self) { day in
                            let on = model.weekdays.contains(day)
                            Button {
                                if on { model.weekdays.remove(day) } else { model.weekdays.insert(day) }
                            } label: {
                                Text(weekdayLetters[day]).font(.caption.weight(.semibold))
                                    .frame(width: 32, height: 26)
                                    .background(RoundedRectangle(cornerRadius: 7).fill(on ? Color.accentColor : Color(nsColor: .controlBackgroundColor)))
                                    .overlay(RoundedRectangle(cornerRadius: 7).strokeBorder(on ? Color.clear : Color.primary.opacity(0.12)))
                                    .foregroundStyle(on ? Color.white : Color.primary)
                            }
                            .buttonStyle(.plain)
                            .accessibilityLabel(Calendar.current.weekdaySymbols[(day + 1) % 7])
                            .accessibilityAddTraits(on ? .isSelected : [])
                        }
                    }
                    Spacer()
                    timePicker
                case .monthly:
                    SchedulePill(symbol: "calendar") {
                        Text("On day").foregroundStyle(.secondary)
                        Menu {
                            ForEach(1...31, id: \.self) { day in Button("\(day)") { model.monthDay = day } }
                        } label: {
                            Text("\(model.monthDay)").font(.body.monospacedDigit().weight(.medium))
                        }
                        .menuStyle(.borderlessButton)
                        .fixedSize()
                    }
                    Spacer()
                    timePicker
                case .hours:
                    SchedulePill(symbol: "repeat") {
                        Text("Every").foregroundStyle(.secondary)
                        Menu {
                            ForEach([1, 2, 3, 4, 6, 8, 12], id: \.self) { hours in
                                Button("\(hours) hour\(hours == 1 ? "" : "s")") { model.everyHours = hours }
                            }
                        } label: {
                            Text("\(model.everyHours) hour\(model.everyHours == 1 ? "" : "s")").font(.body.weight(.medium))
                        }
                        .menuStyle(.borderlessButton)
                        .fixedSize()
                    }
                    Spacer()
                    Text("from").foregroundStyle(.secondary)
                    timePicker
                case .daily:
                    Spacer()
                    timePicker
                case .file:
                    watchFolderButton
                    TextField("Only these, e.g. pdf, Invoice*", text: $model.watchMatch)
                        .textFieldStyle(.roundedBorder)
                        .frame(width: 190)
                        .help("Leave empty for any file. A word such as pdf means files ending in .pdf.")
                }
            }
            .disabled(!model.cron.trimmingCharacters(in: .whitespaces).isEmpty)
            .opacity(model.cron.trimmingCharacters(in: .whitespaces).isEmpty ? 1 : 0.4)

            HStack(alignment: .top, spacing: 8) {
                Image(systemName: scheduleProblem.isEmpty ? "clock" : "exclamationmark.triangle")
                    .foregroundStyle(scheduleProblem.isEmpty ? Color.accentColor : Color.orange)
                if scheduleProblem.isEmpty && model.when == .file && model.cron.trimmingCharacters(in: .whitespaces).isEmpty {
                    (Text("Runs ") + Text(sentence.isEmpty ? "when a file arrives" : sentence.lowercasedFirst).fontWeight(.semibold)
                     + Text(", once for each file, after it has finished copying. Files already there don’t start it. ")
                     + Text(model.isAI ? "The agent is told which file it was; put {file} in the prompt to say where."
                                       : "The script gets it as {file} in its arguments and as $MAC_MCP_TRIGGER_FILE."))
                        .fixedSize(horizontal: false, vertical: true)
                } else if scheduleProblem.isEmpty {
                    (Text("Runs ") + Text(sentence.isEmpty ? "…" : sentence.lowercasedFirst).fontWeight(.semibold) + Text(". ")
                     + Text(firstRun.map { "First run: \(TaskTime.upcoming($0)). " } ?? "")
                     + Text("If your Mac is asleep then, the run is skipped and shown in the history."))
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    Text(scheduleProblem).fixedSize(horizontal: false, vertical: true)
                }
            }
            .font(.callout)
            .padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 9, style: .continuous).fill(TaskStyle.accentStrip))
            .overlay(RoundedRectangle(cornerRadius: 9, style: .continuous).strokeBorder(Color.accentColor.opacity(0.35)))
        }
    }

    private var timePicker: some View {
        TimeField(date: $model.time)
    }

    private var watchFolderButton: some View {
        Button {
            let panel = NSOpenPanel()
            panel.canChooseDirectories = true
            panel.canChooseFiles = false
            panel.allowsMultipleSelection = false
            panel.prompt = "Watch This Folder"
            panel.directoryURL = URL(fileURLWithPath: model.watchFolder.isEmpty
                                     ? NSHomeDirectory() + "/Downloads" : model.watchFolder)
            if panel.runModal() == .OK, let url = panel.url { model.watchFolder = url.path }
        } label: {
            Label(model.watchFolder.isEmpty ? "Choose a folder…" : URL(fileURLWithPath: model.watchFolder).lastPathComponent,
                  systemImage: "folder")
                .lineLimit(1)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .help(model.watchFolder.isEmpty ? "The folder to watch for new files" : model.watchFolder)
    }

    /// The selected agent (or the Default Agent) and the usage limits it can keep (#211).
    private var limitAgent: (name: String, tokens: Bool, cost: Bool)? {
        let id = model.provider.isEmpty ? (defaultAgent ?? "") : model.provider
        guard let agent = agents.first(where: { ($0["id"] as? String) == id }) else { return nil }
        let limits = agent["limits"] as? [String: Any] ?? [:]
        return (agent["name"] as? String ?? id, limits["tokens"] as? Bool ?? false, limits["cost"] as? Bool ?? false)
    }

    private var limitNote: (text: String, problem: Bool) {
        guard let agent = limitAgent else {
            return ("Choose the agent first: whether it can be stopped at a limit depends on what it reports.", true)
        }
        if model.usageKind == "tokens" && !agent.tokens {
            return ("\(agent.name) reports its usage only when it finishes, so it can’t be stopped at a token limit. "
                    + "Claude Code, OpenCode, Kilo Code and pi can.", true)
        }
        if model.usageKind == "cost" && !agent.cost {
            return ("\(agent.name) doesn’t report its price as it works. OpenCode and Kilo Code do; or use a token limit.", true)
        }
        return ("The agent is stopped a few seconds after it reaches the limit. The time limit still applies.", false)
    }

    private var usageLimitField: some View {
        field("Usage limit", optional: true) {
            HStack(spacing: 8) {
                Picker("", selection: $model.usageKind) {
                    Text("No limit").tag("none")
                    Text("Tokens").tag("tokens")
                    Text("Cost (USD)").tag("cost")
                }
                .labelsHidden()
                .fixedSize()
                if model.usageKind != "none" {
                    TextField(model.usageKind == "tokens" ? "e.g. 500000" : "e.g. 2.00", text: $model.usageAmount)
                        .textFieldStyle(.roundedBorder)
                        .font(.body.monospacedDigit())
                        .frame(width: 130)
                }
                Spacer()
            }
            if model.usageKind != "none" {
                let note = limitNote
                Text(note.text)
                    .font(.caption2).foregroundStyle(note.problem ? Color.orange : Color.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var advanced: some View {
        DisclosureGroup(isExpanded: $model.showAdvanced) {
            VStack(alignment: .leading, spacing: 12) {
                if model.isAI {
                    field("Model", optional: true) {
                        TextField("The agent's default model", text: $model.model).textFieldStyle(.roundedBorder)
                    }
                    usageLimitField
                } else {
                    field("Run with", optional: true) {
                        HStack {
                            TextField("Picked from the file type, e.g. a project's .venv Python", text: $model.interpreter)
                                .textFieldStyle(.roundedBorder)
                            Button("Choose…") { chooseInterpreter() }
                        }
                    }
                    field("Arguments", optional: true) {
                        HStack(spacing: 6) {
                            TextField("e.g. --since {last_week_start} --output ~/Desktop/reports", text: $model.args)
                                .textFieldStyle(.roundedBorder)
                                .font(.system(.callout, design: .monospaced))
                            dateMenu($model.args)
                        }
                        if model.command.trimmingCharacters(in: .whitespaces).isEmpty {
                            nextRunLine(argsPreview, source: model.args, prefix: "")
                        }
                    }
                    field("Or run a command instead of a file", optional: true) {
                        HStack(spacing: 6) {
                            TextField("e.g. shortcuts run \"Daily Backup\"", text: $model.command)
                                .textFieldStyle(.roundedBorder)
                                .font(.system(.callout, design: .monospaced))
                            dateMenu($model.command)
                        }
                        if !model.command.trimmingCharacters(in: .whitespaces).isEmpty {
                            nextRunLine(argsPreview, source: model.command, prefix: "")
                        }
                    }
                    field("Works in", optional: true) { folderButton(placeholder: "The script's folder") }
                }
                field("Check the result", optional: true) {
                    TextField("A file the run should leave, e.g. ~/Desktop/reports/weekly.csv", text: $model.expectedFile)
                        .textFieldStyle(.roundedBorder)
                        .font(.system(.callout, design: .monospaced))
                    Text("After a run that finishes, Mac MCP checks the file is there, is not empty and was written by that run. It never opens the file.")
                        .font(.caption2).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                field("Time limit") {
                    Picker("", selection: $model.timeoutMin) {
                        Text("Default (\(model.isAI ? 30 : 10) minutes)").tag(0)
                        ForEach([5, 10, 15, 30, 60, 120, 240], id: \.self) { minutes in
                            Text(minutes < 60 ? "\(minutes) minutes" : "\(minutes / 60) hour\(minutes == 60 ? "" : "s")").tag(minutes)
                        }
                    }
                    .labelsHidden()
                    .frame(width: 220)
                }
                field("Custom schedule (cron)", optional: true) {
                    TextField("minute hour day month weekday, e.g. 0 18 * * 1-5", text: $model.cron)
                        .textFieldStyle(.roundedBorder)
                        .font(.system(.callout, design: .monospaced))
                    Text("Overrides “When”. Uses this Mac’s time zone.").font(.caption2).foregroundStyle(.secondary)
                }
            }
            .padding(.top, 8)
        } label: {
            HStack {
                Text("Advanced").font(.callout.weight(.medium))
                Spacer()
                Text(model.isAI ? "model, usage limit, check, time limit, cron" : "program, arguments, command, check, cron")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    private func folderButton(placeholder: String) -> some View {
        HStack(spacing: 6) {
            Button {
                let panel = NSOpenPanel()
                panel.canChooseDirectories = true
                panel.canChooseFiles = false
                panel.allowsMultipleSelection = false
                panel.prompt = "Choose"
                if !model.cwd.isEmpty { panel.directoryURL = URL(fileURLWithPath: model.cwd) }
                if panel.runModal() == .OK, let url = panel.url { model.cwd = url.path }
            } label: {
                Label(model.cwd.isEmpty ? placeholder : URL(fileURLWithPath: model.cwd).lastPathComponent, systemImage: "folder")
                    .lineLimit(1)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .help(model.cwd.isEmpty ? placeholder : model.cwd)
            if !model.cwd.isEmpty {
                Button { model.cwd = "" } label: { Image(systemName: "xmark.circle.fill") }
                    .buttonStyle(.borderless)
                    .foregroundStyle(.secondary)
                    .accessibilityLabel("Use the default folder")
            }
        }
    }

    private func chooseScript() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.prompt = "Choose Script"
        if !model.script.isEmpty { panel.directoryURL = URL(fileURLWithPath: model.script).deletingLastPathComponent() }
        if panel.runModal() == .OK, let url = panel.url {
            model.script = url.path
            if model.name.isEmpty {
                model.name = url.deletingPathExtension().lastPathComponent
                    .replacingOccurrences(of: "_", with: " ").replacingOccurrences(of: "-", with: " ").capitalizedFirst
            }
        }
    }

    private func chooseDataScript() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.prompt = "Choose Script"
        if !model.dataScript.isEmpty { panel.directoryURL = URL(fileURLWithPath: model.dataScript).deletingLastPathComponent() }
        if panel.runModal() == .OK, let url = panel.url { model.dataScript = url.path }
    }

    private func chooseInterpreter() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.allowsMultipleSelection = false
        panel.treatsFilePackagesAsDirectories = true
        panel.showsHiddenFiles = true
        panel.prompt = "Use This Program"
        if panel.runModal() == .OK, let url = panel.url { model.interpreter = url.path }
    }

    private func preview() async {
        try? await Task.sleep(nanoseconds: 200_000_000)
        guard !Task.isCancelled else { return }
        var body: [String: Any] = ["schedule": model.schedule]
        if !model.isAI, !model.script.isEmpty {
            body["script"] = model.script
            body["interpreter"] = model.interpreter
        }
        if !model.isAI {
            let command = model.command.trimmingCharacters(in: .whitespaces)
            body["args"] = command.isEmpty ? model.args : ""
            body["command"] = command
        }
        if model.isAI, model.dataOn { body["data"] = model.dataStep }
        let result = await state.taskAPI("dashboard/api/scheduled-tasks/preview", body: body)
        guard result.ok else { return }
        if let schedule = result.object["schedule"] as? [String: Any] {
            if (schedule["ok"] as? Bool) == true {
                sentence = schedule["text"] as? String ?? ""
                firstRun = ((schedule["next_runs"] as? [Double])?.first).map { Date(timeIntervalSince1970: $0) }
                scheduleProblem = ""
            } else {
                scheduleProblem = schedule["message"] as? String ?? "This schedule can’t be used."
            }
        }
        if model.when == .weekly, model.cron.isEmpty, model.weekdays.isEmpty {
            scheduleProblem = "Pick at least one day."
        }
        scriptInfo = result.object["script"] as? [String: Any]
        dates = result.object["dates"] as? [[String: String]] ?? dates
        dataPreview = result.object["data"] as? [String: Any]
        argsPreview = result.object["args"] as? [String: Any]
    }

    private func save() {
        saving = true
        error = ""
        Task {
            var body: [String: Any] = ["definition": model.definition]
            if let id = model.taskID { body["id"] = id }
            let result = await state.taskAPI("dashboard/api/scheduled-tasks", body: body)
            saving = false
            if result.ok {
                done((result.object["task"] as? [String: Any])?["id"] as? String)
            } else {
                error = result.object["message"] as? String ?? "Couldn’t save the task."
            }
        }
    }
}

// MARK: - Schedule controls

/// A rounded field with an icon, roomy enough for its value (the system date fields are tight).
struct SchedulePill<Content: View>: View {
    let symbol: String
    @ViewBuilder let content: Content

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: symbol).font(.system(size: 13)).foregroundStyle(.secondary)
            content
        }
        .padding(.horizontal, 12)
        .frame(height: 30)
        .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(Color(nsColor: .controlBackgroundColor)))
        .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous).strokeBorder(Color.primary.opacity(0.14)))
    }
}

/// Hour and minute as two menus in one pill.
struct TimeField: View {
    @Binding var date: Date

    private var hour: Int { Calendar.current.component(.hour, from: date) }
    private var minute: Int { Calendar.current.component(.minute, from: date) }

    private func set(hour: Int? = nil, minute: Int? = nil) {
        date = Calendar.current.date(bySettingHour: hour ?? self.hour, minute: minute ?? self.minute, second: 0, of: date) ?? date
    }

    private var minutes: [Int] {
        let steps = Array(stride(from: 0, to: 60, by: 5))
        return steps.contains(minute) ? steps : (steps + [minute]).sorted()
    }

    var body: some View {
        SchedulePill(symbol: "clock") {
            HStack(spacing: 3) {
                Menu {
                    ForEach(0..<24, id: \.self) { value in Button(String(format: "%02d", value)) { set(hour: value) } }
                } label: {
                    Text(String(format: "%02d", hour)).font(.body.monospacedDigit().weight(.medium))
                }
                .menuStyle(.borderlessButton)
                .menuIndicator(.hidden)
                .fixedSize()
                .accessibilityLabel("Hour")
                Text(":").font(.body.weight(.medium)).foregroundStyle(.secondary)
                Menu {
                    ForEach(minutes, id: \.self) { value in Button(String(format: "%02d", value)) { set(minute: value) } }
                } label: {
                    Text(String(format: "%02d", minute)).font(.body.monospacedDigit().weight(.medium))
                }
                .menuStyle(.borderlessButton)
                .menuIndicator(.hidden)
                .fixedSize()
                .accessibilityLabel("Minute")
            }
        }
    }
}

/// The day, opening a calendar; the time stays in its own TimeField.
struct DateField: View {
    @Binding var date: Date
    @MacMCPState private var open = false

    private var day: Binding<Date> {
        Binding(get: { date }, set: { picked in
            let calendar = Calendar.current
            let clock = calendar.dateComponents([.hour, .minute], from: date)
            date = calendar.date(bySettingHour: clock.hour ?? 9, minute: clock.minute ?? 0, second: 0, of: picked) ?? picked
        })
    }

    var body: some View {
        Button { open.toggle() } label: {
            SchedulePill(symbol: "calendar") {
                Text(date.formatted(.dateTime.weekday(.abbreviated).day().month(.abbreviated).year()))
                    .font(.body.weight(.medium))
                Image(systemName: "chevron.down").font(.caption2.weight(.semibold)).foregroundStyle(.secondary)
            }
        }
        .buttonStyle(.plain)
        .popover(isPresented: $open, arrowEdge: .bottom) {
            DatePicker("", selection: day, in: Calendar.current.startOfDay(for: Date())..., displayedComponents: .date)
                .labelsHidden()
                .datePickerStyle(.graphical)
                .padding(12)
        }
        .accessibilityLabel("Date")
    }
}

private enum DetailPath {
    static func folder(_ path: String) -> String {
        TaskDetailView.tilde(URL(fileURLWithPath: path).deletingLastPathComponent().path)
    }
}

private extension String {
    var lowercasedFirst: String { prefix(1).lowercased() + dropFirst() }
}

// MARK: - Review (approval)

private struct TaskReviewSheet: View {
    @ObservedObject var state: AppState
    let task: ScheduledTask
    let done: () -> Void

    @MacMCPState private var busy = false
    @MacMCPState private var error = ""

    private var proposal: [String: Any] { task.proposal ?? [:] }
    private var isNew: Bool { (proposal["is_new"] as? Bool) ?? !task.approved }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 12) {
                ZStack {
                    Circle().fill(Color.orange.opacity(0.16)).frame(width: 42, height: 42)
                    Image(systemName: "checkmark.shield").font(.title3).foregroundStyle(.orange)
                }
                VStack(alignment: .leading, spacing: 2) {
                    Text(isNew ? "Review suggested task" : "Review suggested change").font(.title3.weight(.semibold))
                    Text("\(proposal["proposed_by"] as? String ?? "An AI app") suggested this \(suggestedAgo). Nothing runs until you approve it.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }

            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    HStack(spacing: 10) {
                        TaskIcon(ai: (proposal["kind"] as? String) == "ai", size: 32)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(proposal["name"] as? String ?? "").font(.headline)
                            if let text = proposal["description"] as? String, !text.isEmpty {
                                Text(text).font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    }
                    .padding(.bottom, 8)
                    ForEach(rows, id: \.title) { row in
                        Divider()
                        reviewRow(row)
                    }
                }
                .taskCard()
            }

            Label("It runs with your account's access, like an app you start yourself. Approve only what you'd run yourself.",
                  systemImage: "info.circle")
                .font(.caption).foregroundStyle(.secondary)
            if !error.isEmpty {
                Text(error).font(.callout).foregroundStyle(.red)
            }
            HStack {
                Spacer()
                Button(isNew ? "Decline" : "Keep current", role: .destructive) { act("decline") }
                    .disabled(busy)
                Button("Approve") { act("approve") }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
                    .disabled(busy)
            }
        }
        .padding(22)
        .frame(width: 560, height: 560)
    }

    private var suggestedAgo: String {
        guard let at = proposal["proposed_at"] as? Double else { return "" }
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .full
        return formatter.localizedString(for: Date(timeIntervalSince1970: at), relativeTo: Date())
    }

    private struct Row {
        let title: String
        let value: String
        let before: String?
        var mono = false
    }

    private var rows: [Row] {
        let current = task.approved ? task.raw : [:]
        func value(_ key: String) -> String {
            if let text = proposal[key] as? String { return text }
            if let flag = proposal[key] as? Bool { return flag ? "Yes" : "No" }
            if let number = proposal[key] as? Int { return "\(number)" }
            return ""
        }
        func before(_ key: String, _ shown: String) -> String? {
            guard !isNew else { return nil }
            let old = current[key].map { "\($0)" } ?? ""
            let oldText = key == "schedule_text" ? (current["schedule_text"] as? String ?? "") : old
            return oldText == shown ? nil : (oldText.isEmpty ? "—" : oldText)
        }
        var out: [Row] = []
        let schedule = value("schedule_text")
        out.append(Row(title: "When", value: schedule, before: before("schedule_text", schedule)))
        if (proposal["kind"] as? String) == "ai" {
            let step = (proposal["data_step"] as? [String: Any]).map(TaskDetailView.program) ?? ""
            let oldStep = (current["data_step"] as? [String: Any]).map(TaskDetailView.program) ?? ""
            if !step.isEmpty || !oldStep.isEmpty {
                out.append(Row(title: "Data first", value: step.isEmpty ? "None" : step,
                               before: isNew || step == oldStep ? nil : (oldStep.isEmpty ? "—" : oldStep), mono: true))
            }
            let prompt = value("prompt")
            out.append(Row(title: "Asks the agent", value: prompt, before: before("prompt", prompt)))
            let agent = value("provider").isEmpty ? "Default agent" : value("provider").capitalized
            out.append(Row(title: "Agent", value: agent, before: nil))
            // A suggested change can raise or remove the limit; it is shown like the rest (#211).
            let limit = (proposal["usage_limit"] as? [String: Any]).map(ScheduledTask.describeLimit) ?? ""
            let oldLimit = (current["usage_limit"] as? [String: Any]).map(ScheduledTask.describeLimit) ?? ""
            if !limit.isEmpty || !oldLimit.isEmpty {
                out.append(Row(title: "Usage limit", value: limit.isEmpty ? "None" : "Stops at " + limit,
                               before: isNew || limit == oldLimit ? nil : (oldLimit.isEmpty ? "None" : "Stops at " + oldLimit)))
            }
        } else if !value("command").isEmpty {
            out.append(Row(title: "Runs command", value: value("command"), before: before("command", value("command")), mono: true))
        } else {
            out.append(Row(title: "Runs file", value: TaskDetailView.tilde(value("script")), before: nil, mono: true))
            if !value("interpreter").isEmpty {
                out.append(Row(title: "With", value: TaskDetailView.tilde(value("interpreter")), before: nil, mono: true))
            }
            if !value("args").isEmpty {
                out.append(Row(title: "Arguments", value: value("args"), before: before("args", value("args")), mono: true))
            }
        }
        let folder = value("cwd")
        out.append(Row(title: "Works in", value: folder.isEmpty ? ((proposal["kind"] as? String) == "ai" ? "Home folder" : "The script's folder") : TaskDetailView.tilde(folder), before: nil))
        let checks = TaskDetailView.checks(proposal["verify"]) ?? ""
        let oldChecks = TaskDetailView.checks(current["verify"]) ?? ""
        if !checks.isEmpty || !oldChecks.isEmpty {
            out.append(Row(title: "Checks", value: checks.isEmpty ? "None" : checks,
                           before: isNew || checks == oldChecks ? nil : (oldChecks.isEmpty ? "—" : oldChecks), mono: true))
        }
        out.append(Row(title: "Time limit", value: "\(proposal["timeout_min"] as? Int ?? 0) minutes", before: nil))
        return out
    }

    private func reviewRow(_ row: Row) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text(row.title).font(.callout).foregroundStyle(.secondary).frame(width: 110, alignment: .leading)
            VStack(alignment: .leading, spacing: 3) {
                if let before = row.before {
                    Text(before).strikethrough().foregroundStyle(.secondary)
                        .font(row.mono ? .system(.caption, design: .monospaced) : .caption)
                }
                Text(row.value)
                    .font(row.mono ? .system(.callout, design: .monospaced) : .callout)
                    .foregroundStyle(row.before != nil ? Color.orange : Color.primary)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
        }
        .padding(.vertical, 8)
    }

    private func act(_ action: String) {
        busy = true
        Task {
            let result = await state.taskAPI("dashboard/api/scheduled-tasks/action", body: ["id": task.id, "action": action])
            busy = false
            if result.ok { done() } else { error = result.object["message"] as? String ?? "Couldn’t do that." }
        }
    }
}
