import SwiftUI

/// Settings › Agents › Skills (#61): the skills Mac MCP found, where each comes from, and whether Mac MCP
/// uses it. Turning one off keeps it out of Mac MCP's skill list and search only; its files and the CLI that
/// owns them stay as they are.
struct SkillsSection: View {
    @ObservedObject var state: AppState
    @MacMCPState private var skills: [[String: Any]] = []
    @MacMCPState private var loaded = false
    @MacMCPState private var problem: String?
    @MacMCPState private var notice: String?
    @MacMCPState private var rescanning = false
    @MacMCPState private var filter = ""

    /// A long list (tools' shared folders can hold dozens) narrows by name, description or source.
    private var shown: [[String: Any]] {
        let words = filter.trimmingCharacters(in: .whitespaces).lowercased()
        guard !words.isEmpty else { return skills }
        return skills.filter { skill in
            ["name", "description", "source"].contains { key in
                (skill[key] as? String ?? "").lowercased().contains(words)
            }
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline) {
                Text("Skills are written instructions agents read before a task. Mac MCP finds them in its own folder and in your AI tools' folders.")
                    .font(SettingsType.meta).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 8)
                Button(rescanning ? "Scanning…" : "Scan Again") { Task { await rescan() } }
                    .controlSize(.small)
                    .disabled(rescanning)
            }
            if let notice {
                Text(notice).font(SettingsType.note).foregroundStyle(.secondary)
            }
            if !loaded {
                if let problem {
                    LoadProblem(message: problem) { await load() }
                } else {
                    ProgressView().controlSize(.small)
                }
            } else if skills.isEmpty {
                Text("No skills were found.").font(SettingsType.body).foregroundStyle(.secondary)
            } else {
                if skills.count > 8 {
                    TextField("Filter \(skills.count) skills", text: $filter)
                        .textFieldStyle(.roundedBorder)
                        .accessibilityLabel("Filter skills")
                }
                let list = shown
                ForEach(groups(list), id: \.source) { group in
                    VStack(alignment: .leading, spacing: 8) {
                        HStack(alignment: .firstTextBaseline) {
                            Text(group.source).font(SettingsType.title)
                            Spacer()
                            Text("\(group.skills.filter { !($0["excluded"] as? Bool ?? false) }.count) of \(group.skills.count) on")
                                .font(SettingsType.meta).foregroundStyle(.secondary).monospacedDigit()
                        }
                        LazyVStack(spacing: 0) {
                            ForEach(group.skills.indices, id: \.self) { index in
                                if index > 0 { Divider().padding(.leading, 14) }
                                row(group.skills[index])
                            }
                        }
                        .usageListBox()
                    }
                    .padding(.top, 6)
                }
                if list.isEmpty {
                    Text("No skill matches.").font(SettingsType.body).foregroundStyle(.secondary)
                }
                Text("A skill that is off is left out of Mac MCP's skill list and search. Its files and the tool that owns it are not changed.")
                    .font(SettingsType.meta).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .task { await load() }
    }

    private func row(_ skill: [String: Any]) -> some View {
        let name = skill["name"] as? String ?? ""
        let location = skill["location"] as? String ?? ""
        let excluded = skill["excluded"] as? Bool ?? false
        let description = skill["description"] as? String ?? ""
        return HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(name).font(SettingsType.title).foregroundStyle(excluded ? .secondary : .primary)
                    if let when = changed(skill["updated_at"] as? String) {
                        Text(when).font(SettingsType.meta).foregroundStyle(.tertiary)
                    }
                }
                Text(description).font(SettingsType.note).foregroundStyle(.secondary).lineLimit(1)
                    .help(description)
            }
            .help(location)
            Spacer(minLength: 8)
            Toggle("Use in Mac MCP", isOn: Binding(
                get: { !excluded },
                set: { use in Task { await setUsed(location: location, name: name, use: use) } }
            ))
            .toggleStyle(.switch)
            .labelsHidden()
            .controlSize(.small)
            .accessibilityLabel("Use \(name) in Mac MCP")
        }
        .padding(.horizontal, 14).padding(.vertical, 8)
        .opacity(excluded ? 0.7 : 1)
    }

    /// Skills by where they come from, Mac MCP's own first, each group in name order as the server sends them.
    private func groups(_ list: [[String: Any]]) -> [(source: String, skills: [[String: Any]])] {
        var order: [String] = []
        var bySource: [String: [[String: Any]]] = [:]
        for skill in list {
            let source = skill["source"] as? String ?? "Other"
            if bySource[source] == nil { order.append(source) }
            bySource[source, default: []].append(skill)
        }
        order.sort { lhs, rhs in
            if (lhs == "Mac MCP") != (rhs == "Mac MCP") { return lhs == "Mac MCP" }
            return bySource[lhs]!.count > bySource[rhs]!.count
        }
        return order.map { ($0, bySource[$0]!) }
    }

    private func changed(_ iso: String?) -> String? {
        guard let iso, let date = ISO8601DateFormatter().date(from: iso) else { return nil }
        return "changed " + date.formatted(date: .abbreviated, time: .omitted)
    }

    private func load() async {
        let result = await state.taskAPI("dashboard/api/skills")
        apply(result, failure: "Skills could not be read.")
    }

    private func rescan() async {
        rescanning = true
        let result = await state.taskAPI("dashboard/api/skills/reindex", body: [:])
        rescanning = false
        apply(result, failure: "Skills could not be scanned again.")
        if result.ok { notice = "Scanned again: \(skills.count) skills." }
    }

    private func apply(_ result: (ok: Bool, object: [String: Any]), failure: String) {
        if result.ok {
            skills = result.object["skills"] as? [[String: Any]] ?? []
            loaded = true
            problem = nil
        } else {
            problem = result.object["message"] as? String ?? failure
        }
    }

    private func setUsed(location: String, name: String, use: Bool) async {
        let result = await state.taskAPI("dashboard/api/skills/exclude", body: ["location": location, "excluded": !use])
        notice = result.ok ? (use ? "\(name) is used again." : "\(name) is left out of Mac MCP.")
            : (result.object["message"] as? String ?? "The change could not be saved.")
        await load()
    }
}
