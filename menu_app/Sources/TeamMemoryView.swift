import SwiftUI

/// Settings › Agents › Teams › Memory (#141): what a saved team remembers, read and fixed by the owner.
///
/// The team has one file and each member one more. The text above the Notes line is the owner's (rules
/// for the team or that member); the notes below are added by members after their runs and by the lead,
/// the oldest leaving first when a file is full. Members get all of it in their prompt each time the
/// team runs. Everything goes through the local dashboard API, so private mode and the secret guard
/// apply here exactly as they do to agents.
struct TeamMemoryView: View {
    let preset: TeamPreset
    @EnvironmentObject private var state: AppState
    @Environment(\.dismiss) private var dismiss

    /// "team" or a member id.
    @MacMCPState private var selection = "team"
    @MacMCPState private var texts: [String: String] = [:]
    @MacMCPState private var drafts: [String: String] = [:]
    @MacMCPState private var limits: [String: Int] = [:]
    @MacMCPState private var noteCounts: [String: Int] = [:]
    @MacMCPState private var loaded = false
    @MacMCPState private var problem: String?
    @MacMCPState private var notice: String?
    @MacMCPState private var noticeIsError = false
    @MacMCPState private var saving = false
    @MacMCPState private var confirmingReset = false

    var body: some View {
        VStack(spacing: 0) {
            HStack(alignment: .firstTextBaseline) {
                Text("\(preset.name) memory").font(.title3.weight(.semibold))
                Spacer()
            }
            .padding(.horizontal, 20).padding(.top, 18).padding(.bottom, 12)
            Divider()
            if loaded {
                HStack(spacing: 0) {
                    sidebar
                    Divider()
                    editor
                }
            } else if let problem {
                LoadProblem(message: problem) { await load() }
                    .padding(20)
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
            } else {
                ProgressView().controlSize(.small).frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            Divider()
            HStack {
                if let notice {
                    Label(notice, systemImage: noticeIsError ? "exclamationmark.triangle.fill" : "checkmark.circle.fill")
                        .font(.caption)
                        .foregroundStyle(noticeIsError ? Color.orange : Color.secondary)
                        .lineLimit(2)
                }
                Spacer()
                Button("Done") { dismiss() }.keyboardShortcut(.cancelAction)
            }
            .padding(.horizontal, 20).padding(.vertical, 12)
        }
        .frame(width: 720, height: 560)
        .task { await load() }
        .confirmationDialog("Reset \(title(selection))?", isPresented: $confirmingReset) {
            Button("Reset", role: .destructive) { Task { await reset() } }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Your text and every note in it are removed. The team starts this part from nothing next time.")
        }
    }

    private var sidebar: some View {
        List(selection: Binding(get: { selection }, set: { if let value = $0 { selection = value } })) {
            Section("Team") {
                row(id: "team", name: "Whole team", detail: "Rules and shared notes", symbol: "person.3")
            }
            Section("Members") {
                ForEach(preset.members) { member in
                    row(id: member.id, name: member.name, detail: member.role?.title ?? "No fixed role",
                        symbol: "person")
                }
            }
        }
        .listStyle(.sidebar)
        .frame(width: 210)
    }

    private func row(id: String, name: String, detail: String, symbol: String) -> some View {
        HStack(spacing: 8) {
            Image(systemName: symbol).foregroundStyle(.secondary).frame(width: 18).accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 1) {
                Text(name).lineLimit(1)
                Text(rowDetail(id, fallback: detail)).font(.caption).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer(minLength: 4)
            if drafts[id] != nil && drafts[id] != texts[id] {
                Circle().fill(Color.accentColor).frame(width: 6, height: 6)
                    .accessibilityLabel("Unsaved changes")
            }
        }
        .tag(id)
    }

    private var editor: some View {
        let id = selection
        let draft = Binding(get: { drafts[id] ?? texts[id] ?? "" }, set: { drafts[id] = $0 })
        let length = (drafts[id] ?? texts[id] ?? "").count
        let limit = limits[id] ?? 0
        let changed = drafts[id] != nil && drafts[id] != texts[id]
        return VStack(alignment: .leading, spacing: 10) {
            Text(explanation(id))
                .font(.callout).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            TextEditor(text: draft)
                .font(.system(.body, design: .monospaced))
                .scrollContentBackground(.hidden)
                .padding(8)
                .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(Color.primary.opacity(0.04)))
                .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous).strokeBorder(Color.primary.opacity(0.12)))
                .overlay(alignment: .topLeading) {
                    if length == 0 {
                        Text(placeholder(id))
                            .font(.system(.body, design: .monospaced))
                            .foregroundStyle(.tertiary)
                            .padding(.horizontal, 13).padding(.vertical, 8)
                            .allowsHitTesting(false)
                            .accessibilityHidden(true)
                    }
                }
                .accessibilityLabel("Memory of \(title(id))")
            HStack {
                Text(limit > 0 ? "\(length) of \(limit) characters" : "\(length) characters")
                    .font(.caption).foregroundStyle(length > limit && limit > 0 ? Color.orange : Color.secondary)
                    .monospacedDigit()
                Spacer()
                Button("Reset…") { confirmingReset = true }
                    .disabled(saving || (texts[id] ?? "").isEmpty)
                Button("Revert") { drafts[id] = nil }
                    .disabled(!changed || saving)
                Button("Save") { Task { await save(id) } }
                    .keyboardShortcut("s", modifiers: .command)
                    .buttonStyle(.borderedProminent)
                    .disabled(!changed || saving)
            }
        }
        .padding(20)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    private func explanation(_ id: String) -> String {
        if id == "team" {
            return "Every member reads this each time the team runs. Write the team's rules above the Notes line. "
                + "Below it, members and the lead add notes after their work; the oldest go first when it is full."
        }
        return "Only \(title(id)) reads this, along with the team's memory. Above the Notes line is your text "
            + "for \(title(id)); below it are the notes \(title(id)) kept from earlier runs."
    }

    /// The file's shape, shown while it is empty.
    private func placeholder(_ id: String) -> String {
        let rules = id == "team" ? "The team's rules, e.g. how to build and test this project." : "Notes for \(title(id)) only."
        return rules + "\n\n## Notes\n- Notes kept after runs appear here, one per line."
    }

    private func title(_ id: String) -> String {
        id == "team" ? "the whole team" : (preset.members.first { $0.id == id }?.name ?? "this member")
    }

    private func rowDetail(_ id: String, fallback: String) -> String {
        guard let count = noteCounts[id], count > 0 else {
            return (texts[id] ?? "").isEmpty ? "Empty" : fallback
        }
        return "\(count) note\(count == 1 ? "" : "s")"
    }

    // MARK: Server

    private func apply(_ object: [String: Any]) {
        if let team = object["team"] as? [String: Any] {
            texts["team"] = team["text"] as? String ?? ""
            limits["team"] = team["max_chars"] as? Int ?? 0
            noteCounts["team"] = team["note_count"] as? Int ?? 0
        }
        for member in object["members"] as? [[String: Any]] ?? [] {
            guard let id = member["id"] as? String else { continue }
            texts[id] = member["text"] as? String ?? ""
            limits[id] = member["max_chars"] as? Int ?? 0
            noteCounts[id] = member["note_count"] as? Int ?? 0
        }
    }

    private func load() async {
        let query = preset.id.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? preset.id
        let result = await state.taskAPI("dashboard/api/team-memory?preset=\(query)")
        guard result.ok else {
            problem = message(result.object, fallback: "Could not read the team's memory.")
            return
        }
        apply(result.object)
        problem = nil
        loaded = true
    }

    private func save(_ id: String) async {
        guard let draft = drafts[id] else { return }
        saving = true
        defer { saving = false }
        var body: [String: Any] = ["action": "save", "preset": preset.id, "text": draft]
        if id != "team" { body["member"] = id }
        let result = await state.taskAPI("dashboard/api/team-memory", body: body)
        guard result.ok else {
            show(message(result.object, fallback: "Could not save."), error: true)
            return
        }
        apply(result.object)
        drafts[id] = nil
        show("Saved \(title(id) == "the whole team" ? "the team's memory" : title(id) + "'s memory").", error: false)
    }

    private func reset() async {
        let id = selection
        saving = true
        defer { saving = false }
        var body: [String: Any] = ["action": "reset", "preset": preset.id]
        if id != "team" { body["member"] = id }
        let result = await state.taskAPI("dashboard/api/team-memory", body: body)
        guard result.ok else {
            show(message(result.object, fallback: "Could not reset."), error: true)
            return
        }
        apply(result.object)
        drafts[id] = nil
        show("Reset \(title(id)).", error: false)
    }

    private func show(_ text: String, error: Bool) {
        notice = text
        noticeIsError = error
    }

    /// The server's own words, without its "code: " prefix.
    private func message(_ object: [String: Any], fallback: String) -> String {
        guard let text = object["message"] as? String, !text.isEmpty else { return fallback }
        if let colon = text.firstIndex(of: ":"), !text[..<colon].contains(" ") {
            return String(text[text.index(after: colon)...]).trimmingCharacters(in: .whitespaces)
        }
        return text
    }
}
