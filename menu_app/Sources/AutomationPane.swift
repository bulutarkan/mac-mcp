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
                        .font(.caption).foregroundStyle(.secondary)
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
                Text("Saved steps you run again with one click: here, from the Shortcuts app and Siri (“Run Mac MCP Recipe”), or with a link.")
                    .font(.caption).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                if !loaded {
                    if problem.isEmpty {
                        ProgressView().controlSize(.small).frame(maxWidth: .infinity)
                    } else {
                        LoadProblem(message: problem) { await load() }
                    }
                } else if recipes.isEmpty {
                    emptyState
                } else {
                    VStack(spacing: 9) {
                        ForEach(recipes) { recipe in card(recipe) }
                    }
                    howToMake
                }
            }
            .padding(22)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func card(_ recipe: Recipe) -> some View {
        let hovering = hovered == recipe.id
        return HStack(spacing: 12) {
            RecipeIcon()
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(recipe.name).font(.body.weight(.semibold)).lineLimit(1)
                    Text(recipe.statusLabel)
                        .font(.caption2.weight(.semibold))
                        .padding(.horizontal, 7).padding(.vertical, 2)
                        .background(Capsule().fill(recipe.statusColor.opacity(0.16)))
                        .foregroundStyle(recipe.statusColor)
                    if recipe.changesThings {
                        Text("Changes things")
                            .font(.caption2)
                            .padding(.horizontal, 7).padding(.vertical, 2)
                            .background(Capsule().fill(Color.primary.opacity(0.07)))
                            .foregroundStyle(.secondary)
                    }
                }
                Text(recipe.summary.isEmpty ? recipe.metaLine : "\(recipe.summary) · \(recipe.metaLine)")
                    .font(.caption).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer(minLength: 8)
            Image(systemName: "chevron.right").font(.caption.weight(.semibold)).foregroundStyle(.tertiary)
        }
        .taskCard(highlight: recipe.status == "draft" ? .orange : nil, hovering: hovering)
        .onHover { inside in hovered = inside ? recipe.id : (hovered == recipe.id ? nil : hovered) }
        .accessibilityElement(children: .combine)
        .activatable("Opens the recipe") { openID = recipe.id }
    }

    private var emptyState: some View {
        VStack(spacing: 12) {
            RecipeIcon(size: 46).padding(.top, 14)
            Text("No recipes yet").font(.headline)
            Text("A recipe is something an AI app did for you once, saved so it can run again. Ask your AI app to do a task and save it as a recipe; it shows up here as a draft for you to check and turn on.")
                .font(.callout).foregroundStyle(.secondary).multilineTextAlignment(.center)
                .frame(maxWidth: 440)
            howToMake
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 20)
        .taskCard()
    }

    private var howToMake: some View {
        VStack(alignment: .leading, spacing: 6) {
            Label("To make one, ask your AI app, for example:", systemImage: "lightbulb")
                .font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            ForEach(["Read tomorrow's calendar and my reminders, and save it as a recipe called “Tomorrow”.",
                     "Open my Meta Ads report in Safari and save that as a recipe called “Ads report”."], id: \.self) { example in
                HStack(alignment: .top, spacing: 6) {
                    Text("“\(example)”").font(.caption).textSelection(.enabled)
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
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 9).fill(Color.primary.opacity(0.04)))
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
                            Text(recipe.summary).font(.callout).foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        Text(recipe.metaLine).font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                }

                if recipe.status == "draft" {
                    Label("A draft: an AI app saved it, and it runs only once it's turned on. Check the steps below first.",
                          systemImage: "exclamationmark.bubble")
                        .font(.caption)
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
                                Text(parameter.name).font(.callout).frame(width: 120, alignment: .leading)
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
                            HStack(alignment: .firstTextBaseline, spacing: 10) {
                                Text("\(index + 1)").font(.caption.weight(.bold)).foregroundStyle(.secondary)
                                    .frame(width: 18, alignment: .trailing)
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(line.title).font(.callout)
                                    if !line.detail.isEmpty {
                                        Text(line.detail).font(.system(.caption, design: .monospaced)).foregroundStyle(.secondary)
                                            .lineLimit(2).truncationMode(.tail)
                                    }
                                }
                            }
                        }
                    }
                }

                section("Run it from elsewhere") {
                    Text("Shortcuts: add the “Run Mac MCP Recipe” action and pick this recipe, or an “Open URLs” action with the link below. Each run asks you first.")
                        .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    HStack(spacing: 8) {
                        Text(recipe.link).font(.system(.caption, design: .monospaced)).textSelection(.enabled)
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

    private func section<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title.uppercased()).font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 8) { content() }
                .frame(maxWidth: .infinity, alignment: .leading)
                .taskCard()
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
        case "open_app": title = "Open \(text("app") ?? text("name") ?? "an app")"
        case "mac_observe": title = "Look at \(text("app") ?? "an app")"
        case "mac_act": title = "Click or type in \(text("app") ?? "an app")"
        case "browser_do", "browser_act": title = text("url").map { "In the browser: \($0)" } ?? "Act in the browser"
        case "browser_list_tabs": title = "List browser tabs"
        default: title = tool.isEmpty ? (step["kind"] as? String ?? "Step").capitalized : tool
        }
        let skip: Set<String> = ["description", "action", "app"]
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
