import SwiftUI

/// Settings › Memory › Review (#60): what agents saved and learned, read and fixed by the owner.
///
/// Memories are searched, edited and deleted one at a time; lessons are filtered by state and
/// approved, turned on or off, or deleted. Everything goes through the local dashboard API, so
/// private mode and the secret guard apply exactly as they do to agents.
struct MemoryReviewView: View {
    @EnvironmentObject private var state: AppState
    @Environment(\.dismiss) private var dismiss
    @MacMCPState private var tab = "memories"

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("What agents remember").font(.title3.weight(.semibold))
                Spacer()
                Picker("", selection: $tab) {
                    Text("Memories").tag("memories")
                    Text("Lessons").tag("lessons")
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(width: 220)
                .accessibilityLabel("Show memories or lessons")
            }
            .padding(.horizontal, 20).padding(.top, 18).padding(.bottom, 12)
            Divider()
            if tab == "memories" {
                MemoryList()
            } else {
                LessonList()
            }
            Divider()
            HStack {
                Spacer()
                Button("Done") { dismiss() }.keyboardShortcut(.defaultAction)
            }
            .padding(.horizontal, 20).padding(.vertical, 12)
        }
        .frame(width: 660, height: 580)
    }
}

// MARK: - Memories

private struct MemoryRow: Identifiable {
    let raw: [String: Any]
    var id: String { raw["memory_id"] as? String ?? "" }
    var content: String { raw["content"] as? String ?? "" }
    var importance: String { raw["importance"] as? String ?? "normal" }
    var tags: [String] { raw["tags"] as? [String] ?? [] }
    var when: String { [raw["date"] as? String, raw["time"] as? String].compactMap { $0 }.joined(separator: " ") }
}

private struct MemoryList: View {
    @EnvironmentObject private var state: AppState
    @MacMCPState private var query = ""
    @MacMCPState private var importance = ""
    @MacMCPState private var rows: [MemoryRow] = []
    @MacMCPState private var loaded = false
    @MacMCPState private var problem: String?
    @MacMCPState private var notice: String?
    @MacMCPState private var editing: String?
    @MacMCPState private var draft = ""
    @MacMCPState private var draftTags = ""
    @MacMCPState private var draftImportance = "normal"
    @MacMCPState private var deleting: MemoryRow?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                TextField("Search memories", text: $query)
                    .textFieldStyle(.roundedBorder)
                    .accessibilityLabel("Search memories")
                Picker("Importance", selection: $importance) {
                    Text("Any importance").tag("")
                    Text("Critical").tag("critical")
                    Text("High").tag("high")
                    Text("Normal").tag("normal")
                    Text("Low").tag("low")
                }
                .labelsHidden()
                .frame(width: 150)
                .accessibilityLabel("Importance")
            }
            if let notice {
                Text(notice).font(.caption).foregroundStyle(.secondary)
            }
            content
        }
        .padding(20)
        .task(id: "\(query)|\(importance)") {
            // A short pause while typing, so each key does not start a search.
            try? await Task.sleep(nanoseconds: query.isEmpty ? 0 : 300_000_000)
            await load()
        }
        .confirmationDialog("Delete this memory?", isPresented: Binding(get: { deleting != nil },
                                                                        set: { if !$0 { deleting = nil } }),
                            presenting: deleting) { row in
            Button("Delete", role: .destructive) { Task { await delete(row) } }
        } message: { row in
            Text(row.content.prefix(160) + (row.content.count > 160 ? "…" : ""))
        }
    }

    @ViewBuilder private var content: some View {
        if !loaded {
            if let problem {
                LoadProblem(message: problem) { await load() }
            } else {
                ProgressView().controlSize(.small).frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        } else if rows.isEmpty {
            VStack(spacing: 6) {
                Image(systemName: "brain").font(.system(size: 28)).foregroundStyle(.tertiary)
                Text(query.isEmpty && importance.isEmpty ? "No memories are stored." : "No memory matches.")
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            ScrollView {
                VStack(spacing: 8) {
                    ForEach(rows) { row in card(row) }
                }
            }
        }
    }

    private func card(_ row: MemoryRow) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            if editing == row.id {
                TextEditor(text: $draft)
                    .font(.callout)
                    .frame(minHeight: 70, maxHeight: 140)
                    .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(Color.secondary.opacity(0.3)))
                    .accessibilityLabel("Memory text")
                HStack(spacing: 8) {
                    TextField("Tags, comma-separated", text: $draftTags)
                        .textFieldStyle(.roundedBorder)
                        .accessibilityLabel("Tags")
                    Picker("Importance", selection: $draftImportance) {
                        Text("Critical").tag("critical")
                        Text("High").tag("high")
                        Text("Normal").tag("normal")
                        Text("Low").tag("low")
                    }
                    .labelsHidden()
                    .frame(width: 110)
                    .accessibilityLabel("Importance")
                    Button("Cancel") { editing = nil }
                    Button("Save") { Task { await save(row) } }
                        .buttonStyle(.borderedProminent)
                        .disabled(draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            } else {
                Text(row.content).font(.callout).lineLimit(4).textSelection(.enabled)
                HStack(spacing: 6) {
                    Text(row.when).font(.caption).foregroundStyle(.secondary)
                    if row.importance != "normal" {
                        Text(row.importance.capitalized)
                            .font(.caption2.weight(.semibold))
                            .padding(.horizontal, 6).padding(.vertical, 2)
                            .background(Capsule().fill(row.importance == "low" ? Color.secondary.opacity(0.15)
                                                       : Color.orange.opacity(0.18)))
                    }
                    ForEach(row.tags, id: \.self) { tag in
                        Text("#\(tag)").font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button { begin(row) } label: { Image(systemName: "pencil") }
                        .buttonStyle(.borderless).help("Edit").accessibilityLabel("Edit memory")
                    Button { deleting = row } label: { Image(systemName: "trash") }
                        .buttonStyle(.borderless).help("Delete").accessibilityLabel("Delete memory")
                }
            }
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color.primary.opacity(0.04)))
    }

    private func begin(_ row: MemoryRow) {
        draft = row.content
        draftTags = row.tags.joined(separator: ", ")
        draftImportance = row.importance
        editing = row.id
    }

    private func load() async {
        var parts = ["limit=100"]
        if let encoded = query.trimmingCharacters(in: .whitespaces).addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed),
           !encoded.isEmpty { parts.append("query=\(encoded)") }
        if !importance.isEmpty { parts.append("importance=\(importance)") }
        let result = await state.taskAPI("dashboard/api/memory/items?" + parts.joined(separator: "&"))
        if result.ok {
            rows = (result.object["results"] as? [[String: Any]] ?? []).map(MemoryRow.init)
            loaded = true
            problem = nil
        } else {
            problem = result.object["message"] as? String ?? "Memories could not be read."
            if !loaded { rows = [] }
        }
    }

    private func save(_ row: MemoryRow) async {
        let tags = draftTags.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        let result = await state.taskAPI("dashboard/api/memory/item", body: [
            "memory_id": row.id, "content": draft, "tags": tags, "importance": draftImportance,
        ])
        if result.ok {
            editing = nil
            notice = "Saved."
            await load()
        } else {
            notice = result.object["message"] as? String ?? "The memory could not be saved."
        }
    }

    private func delete(_ row: MemoryRow) async {
        let result = await state.taskAPI("dashboard/api/memory/item/delete", body: ["memory_id": row.id])
        deleting = nil
        notice = result.ok ? "Deleted." : (result.object["message"] as? String ?? "The memory could not be deleted.")
        await load()
        await state.refreshMemory()
    }
}

// MARK: - Lessons

private struct LessonRow: Identifiable {
    let raw: [String: Any]
    var id: String { raw["lesson_id"] as? String ?? "" }
    var role: String { raw["role"] as? String ?? "" }
    var state: String { raw["state"] as? String ?? "candidate" }
    var action: String { raw["preferred_action"] as? String ?? "" }
    var when: String { raw["trigger_context"] as? String ?? "" }
    var mistake: String { raw["mistake_pattern"] as? String ?? "" }
    var confidence: Double { raw["effective_confidence"] as? Double ?? 0 }
    var stateWords: String {
        switch state {
        case "active": return "On"
        case "disabled": return "Off"
        default: return "Waiting for your approval"
        }
    }
}

private struct LessonList: View {
    @EnvironmentObject private var state: AppState
    @MacMCPState private var query = ""
    @MacMCPState private var stateFilter = ""
    @MacMCPState private var rows: [LessonRow] = []
    @MacMCPState private var loaded = false
    @MacMCPState private var problem: String?
    @MacMCPState private var notice: String?
    @MacMCPState private var deleting: LessonRow?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                TextField("Search lessons", text: $query)
                    .textFieldStyle(.roundedBorder)
                    .accessibilityLabel("Search lessons")
                Picker("State", selection: $stateFilter) {
                    Text("All lessons").tag("")
                    Text("On").tag("active")
                    Text("Waiting for approval").tag("candidate")
                    Text("Off").tag("disabled")
                }
                .labelsHidden()
                .frame(width: 180)
                .accessibilityLabel("Lesson state")
            }
            Text("Lessons are short rules agents learned from mistakes. Only lessons that are on are shown to agents.")
                .font(.caption).foregroundStyle(.secondary)
            if let notice {
                Text(notice).font(.caption).foregroundStyle(.secondary)
            }
            content
        }
        .padding(20)
        .task(id: "\(query)|\(stateFilter)") {
            try? await Task.sleep(nanoseconds: query.isEmpty ? 0 : 300_000_000)
            await load()
        }
        .confirmationDialog("Delete this lesson?", isPresented: Binding(get: { deleting != nil },
                                                                        set: { if !$0 { deleting = nil } }),
                            presenting: deleting) { row in
            Button("Delete", role: .destructive) { Task { await delete(row) } }
        } message: { row in
            Text(row.action)
        }
    }

    @ViewBuilder private var content: some View {
        if !loaded {
            if let problem {
                LoadProblem(message: problem) { await load() }
            } else {
                ProgressView().controlSize(.small).frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        } else if rows.isEmpty {
            VStack(spacing: 6) {
                Image(systemName: "graduationcap").font(.system(size: 28)).foregroundStyle(.tertiary)
                Text(query.isEmpty && stateFilter.isEmpty ? "No lessons yet." : "No lesson matches.")
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            ScrollView {
                VStack(spacing: 8) {
                    ForEach(rows) { row in card(row) }
                }
            }
        }
    }

    private func card(_ row: LessonRow) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(row.action).font(.callout.weight(.semibold)).textSelection(.enabled)
            if !row.when.isEmpty {
                Text("When: \(row.when)").font(.caption).foregroundStyle(.secondary)
            }
            if !row.mistake.isEmpty {
                Text("Instead of: \(row.mistake)").font(.caption).foregroundStyle(.secondary)
            }
            HStack(spacing: 8) {
                Text(row.stateWords)
                    .font(.caption2.weight(.semibold))
                    .padding(.horizontal, 6).padding(.vertical, 2)
                    .background(Capsule().fill(row.state == "active" ? Color.green.opacity(0.18)
                                               : row.state == "disabled" ? Color.secondary.opacity(0.15)
                                               : Color.orange.opacity(0.18)))
                Text("\(row.role) · \(Int((row.confidence * 100).rounded()))% sure").font(.caption).foregroundStyle(.secondary)
                Spacer()
                switch row.state {
                case "candidate":
                    Button("Approve") { Task { await feedback(row, "approve") } }
                case "active":
                    Button("Turn Off") { Task { await feedback(row, "disable") } }
                default:
                    Button("Turn On") { Task { await feedback(row, "enable") } }
                }
                Button { deleting = row } label: { Image(systemName: "trash") }
                    .buttonStyle(.borderless).help("Delete").accessibilityLabel("Delete lesson")
            }
            .controlSize(.small)
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color.primary.opacity(0.04)))
    }

    private func load() async {
        var parts = ["limit=100"]
        if let encoded = query.trimmingCharacters(in: .whitespaces).addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed),
           !encoded.isEmpty { parts.append("query=\(encoded)") }
        if !stateFilter.isEmpty { parts.append("state=\(stateFilter)") }
        let result = await state.taskAPI("dashboard/api/lessons?" + parts.joined(separator: "&"))
        if result.ok {
            rows = (result.object["results"] as? [[String: Any]] ?? []).map(LessonRow.init)
            loaded = true
            problem = nil
        } else {
            problem = result.object["message"] as? String ?? "Lessons could not be read."
        }
    }

    private func feedback(_ row: LessonRow, _ outcome: String) async {
        let result = await state.taskAPI("dashboard/api/lessons/feedback", body: ["lesson_id": row.id, "outcome": outcome])
        notice = result.ok ? nil : (result.object["message"] as? String ?? "The lesson could not be changed.")
        await load()
    }

    private func delete(_ row: LessonRow) async {
        let result = await state.taskAPI("dashboard/api/lessons/delete", body: ["lesson_id": row.id])
        deleting = nil
        notice = result.ok ? "Deleted." : (result.object["message"] as? String ?? "The lesson could not be deleted.")
        await load()
    }
}
