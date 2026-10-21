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
                    .font(.caption2).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 8)
                Button(rescanning ? "Scanning…" : "Scan Again") { Task { await rescan() } }
                    .controlSize(.small)
                    .disabled(rescanning)
            }
            if let notice {
                Text(notice).font(.caption).foregroundStyle(.secondary)
            }
            if !loaded {
                if let problem {
                    LoadProblem(message: problem) { await load() }
                } else {
                    ProgressView().controlSize(.small)
                }
            } else if skills.isEmpty {
                Text("No skills were found.").font(.callout).foregroundStyle(.secondary)
            } else {
                if skills.count > 8 {
                    TextField("Filter \(skills.count) skills", text: $filter)
                        .textFieldStyle(.roundedBorder)
                        .accessibilityLabel("Filter skills")
                }
                let list = shown
                ForEach(list.indices, id: \.self) { index in row(list[index]) }
                if list.isEmpty {
                    Text("No skill matches.").font(.callout).foregroundStyle(.secondary)
                }
                Text("A skill that is off is left out of Mac MCP's skill list and search. Its files and the tool that owns it are not changed.")
                    .font(.caption2).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .task { await load() }
    }

    private func row(_ skill: [String: Any]) -> some View {
        let name = skill["name"] as? String ?? ""
        let location = skill["location"] as? String ?? ""
        let excluded = skill["excluded"] as? Bool ?? false
        return HStack(alignment: .top, spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text(name).font(.callout.weight(.medium)).foregroundStyle(excluded ? .secondary : .primary)
                Text(skill["description"] as? String ?? "").font(.caption).foregroundStyle(.secondary).lineLimit(2)
                Text([skill["source"] as? String, changed(skill["updated_at"] as? String)].compactMap { $0 }.joined(separator: " · "))
                    .font(.caption2).foregroundStyle(.tertiary)
                    .help(location)
            }
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
        .padding(.vertical, 3)
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
