import AppKit
import SwiftUI

/// Settings › Automation: what runs without you typing it each time. Scheduled Tasks run on their own
/// (ScheduledTasksPane); Recipes are saved steps you run again on demand, from here, Shortcuts, Siri or
/// a macmcp://recipe/run link (recipes.py). One sidebar item keeps Settings short.

enum AutomationTab: String, CaseIterable, Identifiable {
    case tasks
    case recipes

    var id: String { rawValue }
    var title: String { self == .tasks ? "Scheduled Tasks" : "Recipes" }
    var symbol: String { self == .tasks ? "calendar.badge.clock" : "play.square.stack" }
}

/// Which tab Automation shows; a macmcp://settings/recipes link sets it before Settings opens.
@MainActor
final class AutomationNavigation: ObservableObject {
    static let shared = AutomationNavigation()
    @Published var tab: AutomationTab = .tasks
}

struct AutomationPane: View {
    @ObservedObject var state: AppState
    @ObservedObject private var navigation = AutomationNavigation.shared

    var body: some View {
        VStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 13) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("Automation").font(.title2.weight(.semibold))
                    Text("Work that runs without you asking each time: on a schedule, or with one click from Shortcuts and Siri.")
                        .font(SettingsType.note).foregroundStyle(.secondary)
                }
                Picker("", selection: $navigation.tab) {
                    ForEach(AutomationTab.allCases) { tab in
                        Label(tab.title, systemImage: tab.symbol).tag(tab)
                    }
                }
                .labelsHidden()
                .pickerStyle(.segmented)
                .fixedSize()
            }
            .padding(.horizontal, 22)
            .padding(.top, 22)
            .padding(.bottom, 12)
            .frame(maxWidth: .infinity, alignment: .leading)

            Divider()

            switch navigation.tab {
            case .tasks: ScheduledTasksPane(state: state, embedded: true)
            case .recipes: RecipesPane(state: state)
            }
        }
    }
}

// MARK: - Recipes

struct Recipe: Identifiable {
    let raw: [String: Any]

    var id: String { raw["recipe_id"] as? String ?? "" }
    var name: String { raw["name"] as? String ?? id }
    var summary: String { raw["description"] as? String ?? "" }
    var status: String { raw["status"] as? String ?? "draft" }
    var changesThings: Bool { (raw["consequential"] as? Bool) == true }
    var stepCount: Int { raw["step_count"] as? Int ?? 0 }
    var runCount: Int { raw["run_count"] as? Int ?? 0 }
    var lastRun: [String: Any]? { raw["last_run"] as? [String: Any] }
    var parameters: [(name: String, spec: [String: Any])] {
        ((raw["parameters"] as? [String: Any]) ?? [:]).keys.sorted().map { key in
            (key, (raw["parameters"] as? [String: Any])?[key] as? [String: Any] ?? [:])
        }
    }
    var link: String { "macmcp://recipe/run?id=\(id)" }

    var statusLabel: String {
        switch status {
        case "active": return "Ready"
        case "paused": return "Paused"
        default: return "Draft"
        }
    }

    var statusColor: Color {
        switch status {
        case "active": return .green
        case "paused": return .secondary
        default: return .orange
        }
    }

    /// "3 steps · ran 4 times, last today at 12:02"
    var metaLine: String {
        var parts = ["\(stepCount) step\(stepCount == 1 ? "" : "s")"]
        if let at = (lastRun?["at"] as? NSNumber)?.doubleValue {
            let when = TaskTime.stamp(Date(timeIntervalSince1970: at))
            let outcome = (lastRun?["ok"] as? Bool) == true ? "" : " (didn't finish)"
            parts.append("ran \(runCount) time\(runCount == 1 ? "" : "s"), last \(when)\(outcome)")
        } else {
            parts.append("never run")
        }
        return parts.joined(separator: " · ")
    }
}

struct RecipeIcon: View {
    var size: CGFloat = 34

    var body: some View {
        RoundedRectangle(cornerRadius: size * 0.27, style: .continuous)
            .fill(LinearGradient(colors: [Color(red: 1.0, green: 0.55, blue: 0.27), Color(red: 0.93, green: 0.30, blue: 0.48)],
                                 startPoint: .topLeading, endPoint: .bottomTrailing))
            .frame(width: size, height: size)
            .overlay(
                Image(systemName: "play.square.stack")
                    .font(.system(size: size * 0.42, weight: .semibold))
                    .foregroundStyle(.white)
            )
            .shadow(color: Color.orange.opacity(0.25), radius: 4, y: 2)
            .accessibilityLabel("Recipe")
    }
}

struct RecipesPane: View {
    @ObservedObject var state: AppState

    @MacMCPState private var recipes: [Recipe] = []
    @MacMCPState private var loaded = false
    @MacMCPState private var problem = ""
    @MacMCPState private var openID: String?
    @MacMCPState private var hovered: String?
    @MacMCPState private var showInfo = false

    var body: some View {
        ZStack {
            if let id = openID, let recipe = recipes.first(where: { $0.id == id }) {
                RecipeDetailView(state: state, recipe: recipe, onBack: { openID = nil }, onChanged: { await load() })
                    .transition(.move(edge: .trailing).combined(with: .opacity))
            } else {
                list.transition(.move(edge: .leading).combined(with: .opacity))
            }
        }
        .animation(.easeInOut(duration: 0.18), value: openID)
        .task { await load() }
    }

    private var list: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text("Saved steps you run again with one click: here, from the Shortcuts app and Siri (“Run Mac MCP Recipe”), or with a link.")
                        .font(SettingsType.note).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    Button { showInfo.toggle() } label: { Image(systemName: "info.circle") }
                        .buttonStyle(.borderless)
                        .help("What is a recipe?")
                        .accessibilityLabel("What is a recipe?")
                        .popover(isPresented: $showInfo, arrowEdge: .bottom) { AutomationExplainer.recipes }
                }
                if !loaded {
                    if problem.isEmpty {
                        ProgressView().controlSize(.small).frame(maxWidth: .infinity)
                    } else {
                        LoadProblem(message: problem) { await load() }
                    }
                } else if recipes.isEmpty {
                    emptyState
                } else {
                    // #125: one list box with hairlines between rows, as in the Usage tabs.
                    VStack(spacing: 0) {
                        ForEach(Array(recipes.enumerated()), id: \.element.id) { index, recipe in
                            if index > 0 { Divider().padding(.leading, 54) }
                            row(recipe)
                        }
                    }
                    .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                    .usageListBox()
                    howToMake
                }
            }
            .padding(22)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func row(_ recipe: Recipe) -> some View {
        let hovering = hovered == recipe.id
        return HStack(spacing: 12) {
            RecipeIcon(size: 28)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 8) {
                    Text(recipe.name).font(SettingsType.title).lineLimit(1)
                    StatusDot(text: recipe.statusLabel, color: recipe.statusColor)
                    if recipe.changesThings { SettingsBadge(text: "Changes things") }
                }
                Text(recipe.summary.isEmpty ? recipe.metaLine : "\(recipe.summary) · \(recipe.metaLine)")
                    .font(SettingsType.note).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer(minLength: 8)
            Image(systemName: "chevron.right")
                .font(.system(size: 9, weight: .bold)).foregroundStyle(.tertiary)
        }
        .padding(.horizontal, 14).padding(.vertical, 10)
        .background(Color.primary.opacity(hovering ? 0.04 : 0))
        .contentShape(Rectangle())
        .onHover { inside in hovered = inside ? recipe.id : (hovered == recipe.id ? nil : hovered) }
        .accessibilityElement(children: .combine)
        .activatable("Opens the recipe") { openID = recipe.id }
    }

    private var emptyState: some View {
        VStack(spacing: 10) {
            RecipeIcon(size: 40).padding(.top, 6)
            Text("No recipes yet").font(SettingsType.title)
            Text("A recipe is something an AI app did for you once, saved so it can run again. Ask your AI app to do a task and save it as a recipe; it shows up here as a draft for you to check and turn on.")
                .font(SettingsType.note).foregroundStyle(.secondary).multilineTextAlignment(.center)
                .frame(maxWidth: 440)
            howToMake
        }
        .frame(maxWidth: .infinity)
    }

    private var howToMake: some View {
        VStack(alignment: .leading, spacing: 6) {
            Label("To make one, ask your AI app, for example:", systemImage: "lightbulb")
                .font(SettingsType.note.weight(.semibold)).foregroundStyle(.secondary)
            ForEach(["Read tomorrow's calendar and my reminders, and save it as a recipe called “Tomorrow”.",
                     "Open my Meta Ads report in Safari and save that as a recipe called “Ads report”."], id: \.self) { example in
                HStack(alignment: .top, spacing: 6) {
                    Text("“\(example)”").font(SettingsType.note).textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 4)
                    Button {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(example, forType: .string)
                    } label: { Image(systemName: "doc.on.doc") }
                    .buttonStyle(.borderless)
                    .help("Copy")
                }
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .usageListBox()
    }

    private func load() async {
        let result = await state.taskAPI("dashboard/api/recipes/manage")
        if result.ok {
            recipes = (result.object["recipes"] as? [[String: Any]] ?? []).map(Recipe.init)
            loaded = true
            problem = ""
        } else if !loaded {
            problem = result.object["message"] as? String ?? "Recipes could not be loaded. Is Mac MCP running?"
        }
    }
}

/// The (i) on each Automation tab: what the feature is, in a few short points.
struct AutomationExplainer: View {
    struct Point: Identifiable {
        let symbol: String
        let title: String
        let text: String
        var id: String { title }
    }

    let icon: AnyView
    let title: String
    let points: [Point]
    let footer: String

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                icon
                Text(title).font(.headline)
            }
            ForEach(points) { point in
                HStack(alignment: .top, spacing: 10) {
                    Image(systemName: point.symbol).foregroundStyle(Color.accentColor).frame(width: 18)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(point.title).font(.callout.weight(.semibold))
                        Text(point.text).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            Divider()
            Text(footer).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
        .padding(16)
        .frame(width: 390)
    }

    static let recipes = AutomationExplainer(
        icon: AnyView(RecipeIcon(size: 26)), title: "What is a recipe?",
        points: [
            Point(symbol: "sparkles", title: "Made once by your AI app",
                  text: "Ask it to do a job and save it as a recipe. Mac MCP keeps the exact steps it took, if the job worked."),
            Point(symbol: "play.fill", title: "Run again without the AI",
                  text: "Mac MCP repeats those steps itself: fast, the same every time, no model cost."),
            Point(symbol: "slider.horizontal.3", title: "Values that change",
                  text: "A fixed word (a month, a client, a search) can become a value you fill in at each run."),
            Point(symbol: "square.stack.3d.up", title: "From anywhere",
                  text: "Here, the Shortcuts app and Siri (“Run Mac MCP Recipe”), a macmcp:// link, or on a schedule as a Scheduled Task. Shortcuts, Siri and links ask you first."),
            Point(symbol: "checkmark.shield", title: "You stay in charge",
                  text: "A new recipe is a draft until it's turned on, one with a password or key in it can't be, and every step follows your permissions."),
        ],
        footer: "Recipes repeat the same steps when you ask. To run one by itself at a set time or when a file arrives, make a Scheduled Task and choose Recipe.")

    static let scheduledTasks = AutomationExplainer(
        icon: AnyView(TaskIcon(ai: true, size: 26)), title: "What are Scheduled Tasks?",
        points: [
            Point(symbol: "clock", title: "Work that runs by itself",
                  text: "Once, daily, weekly, monthly, every few hours or a cron schedule, or each time a file lands in a folder."),
            Point(symbol: "sparkles", title: "An AI task, a script or a recipe",
                  text: "An AI task gives an agent a prompt, optionally with data a script prints first; a script task runs your file or command; a recipe task replays saved steps with no AI model."),
            Point(symbol: "doc.text.magnifyingglass", title: "A report for every run",
                  text: "The result, the steps the agent took and its usage, plus an optional notification. A check can confirm a file, a page or a sheet really changed."),
            Point(symbol: "stethoscope", title: "When something goes wrong",
                  text: "Diagnose asks a read-only agent why, Resume goes on from the agent step, Check Again looks again. A run that never started is flagged."),
            Point(symbol: "checkmark.shield", title: "You stay in charge",
                  text: "Tasks an AI app suggests wait for your approval, an approved script that changes waits again, and time and usage limits stop runaway runs."),
        ],
        footer: "A run missed while your Mac slept is skipped, or runs once when it's back if you choose that. Recipes, the other tab, are the saved steps a recipe task replays.")
}

// MARK: - One recipe

private struct RecipeDetailView: View {
    @ObservedObject var state: AppState
    let recipe: Recipe
    let onBack: () -> Void
    let onChanged: () async -> Void

    @MacMCPState private var steps: [[String: Any]] = []
    @MacMCPState private var values: [String: String] = [:]
    @MacMCPState private var busy = false
    @MacMCPState private var message = ""
    @MacMCPState private var outcome: (ok: Bool, text: String)?
    @MacMCPState private var confirmDelete = false
    @MacMCPState private var confirmRun = false
    @MacMCPState private var copied = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                Button(action: onBack) {
                    Label("Recipes", systemImage: "chevron.left")
                }
                .buttonStyle(.link)

                HStack(alignment: .top, spacing: 12) {
                    RecipeIcon(size: 48)
                    VStack(alignment: .leading, spacing: 4) {
                        Text(recipe.name).font(.title3.weight(.semibold))
                        if !recipe.summary.isEmpty {
                            Text(recipe.summary).font(SettingsType.body).foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        HStack(spacing: 8) {
                            StatusDot(text: recipe.statusLabel, color: recipe.statusColor)
                            Text(recipe.metaLine).font(SettingsType.note).foregroundStyle(.secondary)
                        }
                    }
                    Spacer()
                }

                if recipe.status == "draft" {
                    Label("A draft: an AI app saved it, and it runs only once it's turned on. Check the steps below first.",
                          systemImage: "exclamationmark.bubble")
                        .font(SettingsType.note)
                        .padding(10)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(RoundedRectangle(cornerRadius: 9).fill(Color.orange.opacity(0.12)))
                }

                actions

                if let outcome {
                    Label(outcome.text, systemImage: outcome.ok ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                        .font(.callout)
                        .foregroundStyle(outcome.ok ? Color.green : Color.orange)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if !message.isEmpty {
                    Text(message).font(.callout).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true)
                }

                if !recipe.parameters.isEmpty {
                    section("Values it asks for") {
                        ForEach(recipe.parameters, id: \.name) { parameter in
                            HStack(spacing: 10) {
                                Text(parameter.name).font(SettingsType.body).frame(width: 120, alignment: .leading)
                                TextField(placeholder(parameter.spec), text: Binding(
                                    get: { values[parameter.name] ?? "" },
                                    set: { values[parameter.name] = $0 }))
                                    .textFieldStyle(.roundedBorder)
                            }
                        }
                    }
                }

                section("Steps") {
                    if steps.isEmpty {
                        ProgressView().controlSize(.small)
                    } else {
                        ForEach(Array(steps.enumerated()), id: \.offset) { index, step in
                            let line = Self.describe(step)
                            if index > 0 { Divider() }
                            HStack(alignment: .firstTextBaseline, spacing: 10) {
                                Text("\(index + 1)").font(SettingsType.meta.weight(.bold)).foregroundStyle(.secondary)
                                    .monospacedDigit()
                                    .frame(width: 18, alignment: .trailing)
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(line.title).font(SettingsType.body)
                                    if !line.detail.isEmpty {
                                        Text(line.detail).font(SettingsType.meta.monospaced()).foregroundStyle(.secondary)
                                            .lineLimit(2).truncationMode(.tail)
                                    }
                                }
                            }
                        }
                    }
                }

                section("Run it from elsewhere") {
                    Text("Shortcuts: add the “Run Mac MCP Recipe” action and pick this recipe, or an “Open URLs” action with the link below. Each run asks you first.")
                        .font(SettingsType.note).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    HStack(spacing: 8) {
                        Text(recipe.link).font(SettingsType.note.monospaced()).textSelection(.enabled)
                            .lineLimit(1).truncationMode(.middle)
                        Spacer()
                        Button(copied ? "Copied" : "Copy Link") {
                            NSPasteboard.general.clearContents()
                            NSPasteboard.general.setString(recipe.link, forType: .string)
                            copied = true
                        }
                        .controlSize(.small)
                    }
                }
            }
            .padding(22)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .task(id: recipe.id) { await loadSteps() }
        .confirmationDialog("Delete “\(recipe.name)”?", isPresented: $confirmDelete) {
            Button("Delete", role: .destructive) { act("delete") }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Shortcuts and links that run it stop working. This can't be undone.")
        }
        .confirmationDialog("Run “\(recipe.name)”?", isPresented: $confirmRun) {
            Button("Run") { run() }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This recipe changes things on your Mac or in apps.")
        }
    }

    private var actions: some View {
        HStack(spacing: 8) {
            if recipe.status == "active" {
                Button {
                    if recipe.changesThings { confirmRun = true } else { run() }
                } label: { Label(busy ? "Running…" : "Run", systemImage: "play.fill") }
                .buttonStyle(.borderedProminent)
                .disabled(busy)
                Button("Pause") { act("pause") }.disabled(busy)
            } else {
                Button(recipe.status == "draft" ? "Turn On" : "Resume") { act(recipe.status == "draft" ? "activate" : "resume") }
                    .buttonStyle(.borderedProminent)
                    .disabled(busy)
            }
            Spacer()
            Button(role: .destructive) { confirmDelete = true } label: { Label("Delete", systemImage: "trash") }
                .disabled(busy)
        }
    }

    /// A titled group as every Settings group is drawn: the title above, the content in the quiet list box.
    private func section<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title).font(SettingsType.title)
            VStack(alignment: .leading, spacing: 8) { content() }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 14).padding(.vertical, 12)
                .usageListBox()
        }
    }

    private func placeholder(_ spec: [String: Any]) -> String {
        var parts = [spec["type"] as? String ?? "text"]
        if let fallback = spec["default"] { parts.append("default \(fallback)") }
        if (spec["required"] as? Bool) == false { parts.append("optional") }
        return parts.joined(separator: " · ")
    }

    /// A step in words: what it does, and the values it uses.
    static func describe(_ step: [String: Any]) -> (title: String, detail: String) {
        let tool = step["tool"] as? String ?? ""
        let args = step["arguments"] as? [String: Any] ?? [:]
        func text(_ key: String) -> String? { (args[key] as? String).flatMap { $0.isEmpty ? nil : $0 } }
        let title: String
        switch tool {
        case "mac_snapshot": title = "Read this Mac's status"
        case "mac_app":
            let action = (text("action") ?? "use").replacingOccurrences(of: "_", with: " ")
            title = "\((text("app") ?? "App").capitalized): \(action)"
        case "open_app": title = "Open \(text("app_name") ?? text("app") ?? "an app")"
        case "mac_observe": title = "Look at \(text("app") ?? "an app")"
        case "mac_act": title = "Click or type in \(text("app") ?? "an app")"
        case "browser_do", "browser_act": title = text("url").map { "In the browser: \($0)" } ?? "Act in the browser"
        case "browser_list_tabs": title = "List browser tabs"
        default: title = tool.isEmpty ? (step["kind"] as? String ?? "Step").capitalized : tool
        }
        let skip: Set<String> = ["description", "action", "app", "app_name", "background", "new_tab"]
        let detail = args.keys.sorted().filter { !skip.contains($0) }.compactMap { key -> String? in
            guard let value = args[key] else { return nil }
            let shown: String
            if let string = value as? String { shown = string }
            else if let data = try? JSONSerialization.data(withJSONObject: value, options: [.fragmentsAllowed]) {
                shown = String(decoding: data, as: UTF8.self)
            } else { shown = "\(value)" }
            return "\(key): \(shown.count > 80 ? String(shown.prefix(77)) + "…" : shown)"
        }.joined(separator: "  ")
        return (title, detail)
    }

    private func loadSteps() async {
        let result = await state.taskAPI("dashboard/api/recipes/detail?recipe_id=\(recipe.id)")
        if result.ok, let raw = result.object["recipe"] as? [String: Any] {
            steps = raw["steps"] as? [[String: Any]] ?? []
        } else {
            message = result.object["message"] as? String ?? "The steps could not be loaded."
        }
    }

    private func act(_ action: String) {
        busy = true
        message = ""
        Task {
            let result = await state.taskAPI("dashboard/api/recipes/action", body: ["recipe_id": recipe.id, "action": action])
            busy = false
            if !result.ok { message = result.object["message"] as? String ?? "That didn't work." }
            if action == "delete", result.ok { onBack() }
            await onChanged()
        }
    }

    private func run() {
        busy = true
        message = ""
        outcome = nil
        Task {
            let filled = values.filter { !$0.value.trimmingCharacters(in: .whitespaces).isEmpty }
            let result = await state.taskAPI("dashboard/api/recipes/run", body: ["recipe_id": recipe.id, "values": filled])
            busy = false
            let status = result.object["status"] as? String ?? ""
            if result.ok {
                let count = result.object["steps_executed"] as? Int ?? 0
                outcome = (true, "Finished (\(count) step\(count == 1 ? "" : "s")).")
            } else if status == "approval_required" {
                outcome = (false, "Waiting for approval in Mac MCP.")
            } else {
                outcome = (false, result.object["message"] as? String ?? "It didn't finish.")
            }
            await onChanged()
        }
    }
}
