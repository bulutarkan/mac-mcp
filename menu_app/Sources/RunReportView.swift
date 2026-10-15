import AppKit
import SwiftUI

/// One Scheduled Tasks run, the way a person reads it: the result as formatted text, then the steps
/// the agent took (its thinking, the tools it used, commands, files it changed) with details on demand.
/// Steps come from scheduled_task_runner / agent_timeline.py; nothing here is shown as raw JSON.

struct RunReportSheet: View {
    @ObservedObject var state: AppState
    let taskName: String
    let run: TaskRun
    @Environment(\.dismiss) private var dismiss
    @MacMCPState private var full: TaskRun?
    @MacMCPState private var fullProblem: String?
    @MacMCPState private var tab = "result"

    private var shown: TaskRun { full ?? run }
    private var steps: [[String: Any]] { shown.detail?["steps"] as? [[String: Any]] ?? [] }
    private var isAgent: Bool { shown.detail?["agent_id"] != nil }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
                .padding(.horizontal, 22).padding(.top, 20).padding(.bottom, 12)
            if isAgent && !steps.isEmpty {
                Picker("", selection: $tab) {
                    Text("Result").tag("result")
                    Text("Steps (\(steps.count))").tag("steps")
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(width: 260)
                .padding(.horizontal, 22).padding(.bottom, 10)
            }
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    if let problem = fullProblem {
                        LoadProblem(message: problem) { await loadFull() }
                    }
                    if tab == "steps" && isAgent {
                        StepsTimeline(steps: steps)
                    } else {
                        resultBody
                    }
                }
                .padding(22)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            Divider()
            HStack {
                if let text = copyText, !text.isEmpty {
                    Button {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(text, forType: .string)
                    } label: { Label("Copy", systemImage: "doc.on.doc") }
                }
                Spacer()
                Button("Done") { dismiss() }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
            }
            .padding(.horizontal, 22).padding(.vertical, 12)
        }
        .frame(width: 720, height: 640)
        .task { await loadFull() }
    }

    private func loadFull() async {
        let result = await state.taskAPI("dashboard/api/scheduled-tasks/run?run_id=\(run.id)")
        if result.ok, let raw = result.object["run"] as? [String: Any] {
            full = TaskRun(raw)
            fullProblem = nil
        } else {
            fullProblem = "Only the summary of this run could be loaded; its full report and steps are missing."
        }
    }

    private var copyText: String? {
        if let output = shown.output, !output.isEmpty { return output }
        return shown.summary
    }

    // MARK: Header

    private var header: some View {
        HStack(alignment: .top, spacing: 12) {
            ZStack {
                Circle().fill(shown.color.opacity(0.16)).frame(width: 40, height: 40)
                Image(systemName: shown.symbol).font(.system(size: 20, weight: .semibold)).foregroundStyle(shown.color)
            }
            VStack(alignment: .leading, spacing: 6) {
                Text(taskName).font(.title3.weight(.semibold))
                HStack(spacing: 6) {
                    chip(TaskTime.stamp(shown.startedAt), symbol: "calendar")
                    if let duration = shown.duration { chip(TaskTime.duration(duration), symbol: "timer") }
                    if let agent = agentLabel { chip(agent, symbol: "sparkles") }
                    if let tokens = tokenLabel { chip(tokens, symbol: "number") }
                    if !isAgent, let code = shown.exitCode { chip("Exit code \(code)", symbol: "terminal") }
                }
            }
            Spacer()
        }
    }

    private var agentLabel: String? {
        guard isAgent else { return nil }
        let provider = (shown.detail?["provider"] as? String).map {
            ["codex": "Codex", "claude": "Claude Code", "opencode": "OpenCode", "kimi": "Kimi Code", "grok": "Grok Build"][$0] ?? $0.capitalized
        }
        let model = shown.detail?["model"] as? String
        return [provider, model].compactMap { $0 }.joined(separator: " · ").nilIfEmpty
    }

    private var tokenLabel: String? {
        guard let usage = shown.detail?["usage"] as? [String: Any] else { return nil }
        let total = (usage["input_tokens"] as? Int ?? 0) + (usage["output_tokens"] as? Int ?? 0)
        guard total > 0 else { return nil }
        let text = total >= 1000 ? String(format: "%.0fk tokens", Double(total) / 1000) : "\(total) tokens"
        if let cost = usage["cost_usd"] as? Double, cost > 0 { return text + String(format: " · $%.2f", cost) }
        return text
    }

    private func chip(_ text: String, symbol: String) -> some View {
        Label(text, systemImage: symbol)
            .font(.caption)
            .foregroundStyle(.secondary)
            .padding(.horizontal, 8).padding(.vertical, 3)
            .background(Capsule().fill(Color.primary.opacity(0.06)))
    }

    // MARK: Result

    @ViewBuilder
    private var resultBody: some View {
        if let reason = shown.reason, shown.status != "done" {
            HStack(alignment: .top, spacing: 8) {
                Image(systemName: shown.status == "skipped" ? "forward.end.circle" : "exclamationmark.triangle.fill")
                    .foregroundStyle(shown.color)
                Text(reason).font(.callout).fixedSize(horizontal: false, vertical: true)
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(shown.color.opacity(0.10)))
        }
        if (shown.detail?["private_mode"] as? Bool) == true {
            Label("Private mode was on, so the result was not kept.", systemImage: "eye.slash")
                .font(.callout).foregroundStyle(.secondary)
        } else if isAgent {
            if let report = shown.output?.nilIfEmpty ?? shown.summary {
                MarkdownText(source: report)
                    .textSelection(.enabled)
            }
            artifacts
        } else if let output = shown.output?.nilIfEmpty {
            VStack(alignment: .leading, spacing: 6) {
                Label("Output", systemImage: "terminal").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                ConsoleText(text: output, maxHeight: 420)
            }
        } else if let summary = shown.summary {
            Text(summary).font(.callout)
        }
    }

    @ViewBuilder
    private var artifacts: some View {
        if let items = shown.detail?["artifacts"] as? [[String: Any]], !items.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                Text("Files").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                    FileRow(path: item["ref"] as? String ?? "", change: nil, note: item["description"] as? String)
                }
            }
        }
    }
}

// MARK: - Steps

private struct StepsTimeline: View {
    let steps: [[String: Any]]

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(steps.enumerated()), id: \.offset) { index, step in
                StepRow(step: step, last: index == steps.count - 1)
            }
        }
    }
}

private struct StepRow: View {
    let step: [String: Any]
    let last: Bool
    @MacMCPState private var open = false

    private var kind: String { step["kind"] as? String ?? "tool" }
    private var status: String { step["status"] as? String ?? "done" }
    private var title: String { step["title"] as? String ?? "" }
    private var detail: String? { (step["detail"] as? String)?.nilIfEmpty }
    private var text: String? { (step["text"] as? String)?.nilIfEmpty }
    private var output: String? { (step["output"] as? String)?.nilIfEmpty }
    private var inputs: [String: Any] { step["input"] as? [String: Any] ?? [:] }
    private var hasDetails: Bool { output != nil || !inputs.isEmpty }

    private var style: (symbol: String, color: Color) {
        switch kind {
        case "thinking": return ("brain", .purple)
        case "message": return ("text.bubble", .accentColor)
        case "command": return ("terminal", .teal)
        case "data": return ("tablecells", .green)
        case "files": return ("doc.badge.ellipsis", .orange)
        case "search": return ("globe", .blue)
        case "todo": return ("checklist", .indigo)
        case "error": return ("exclamationmark.triangle", .red)
        default: return ("wrench.and.screwdriver", .blue)
        }
    }

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(spacing: 0) {
                ZStack {
                    Circle().fill(style.color.opacity(0.15)).frame(width: 28, height: 28)
                    Image(systemName: style.symbol).font(.system(size: 12, weight: .semibold)).foregroundStyle(style.color)
                }
                if !last {
                    Rectangle().fill(Color.primary.opacity(0.10)).frame(width: 2).frame(maxHeight: .infinity)
                }
            }
            VStack(alignment: .leading, spacing: 5) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(title)
                        .font(kind == "thinking" ? .callout.weight(.medium) : .callout.weight(.semibold))
                        .foregroundStyle(kind == "thinking" ? Color.secondary : Color.primary)
                    if status == "failed" {
                        Image(systemName: "xmark.circle.fill").foregroundStyle(.red).font(.caption)
                    }
                    Spacer(minLength: 0)
                    if hasDetails {
                        Button {
                            withAnimation(.easeInOut(duration: 0.15)) { open.toggle() }
                        } label: {
                            Label(open ? "Hide" : "Details", systemImage: open ? "chevron.up" : "chevron.down")
                                .font(.caption)
                        }
                        .buttonStyle(.borderless)
                    }
                }
                content
                if open { details }
            }
            .padding(.bottom, last ? 0 : 16)
        }
    }

    @ViewBuilder
    private var content: some View {
        switch kind {
        case "thinking":
            if let text {
                Text(text)
                    .font(.callout).italic()
                    .foregroundStyle(.secondary)
                    .lineLimit(open ? nil : 3)
                    .fixedSize(horizontal: false, vertical: true)
                    .activatable(open ? "Show less" : "Show all of this thinking") { withAnimation { open.toggle() } }
            }
        case "message":
            if let text { MarkdownText(source: text, compact: true) }
        case "command", "data":
            if let detail {
                Text(detail)
                    .font(.system(.caption, design: .monospaced))
                    .padding(.horizontal, 8).padding(.vertical, 5)
                    .background(RoundedRectangle(cornerRadius: 6).fill(Color.primary.opacity(0.06)))
                    .textSelection(.enabled)
            }
        case "files":
            ForEach(Array((step["files"] as? [[String: Any]] ?? []).enumerated()), id: \.offset) { _, file in
                FileRow(path: file["path"] as? String ?? "", change: file["change"] as? String, note: nil)
            }
        case "todo":
            ForEach(Array((step["items"] as? [[String: Any]] ?? []).enumerated()), id: \.offset) { _, item in
                Label(item["text"] as? String ?? "", systemImage: (item["done"] as? Bool) == true ? "checkmark.circle.fill" : "circle")
                    .font(.callout)
                    .foregroundStyle((item["done"] as? Bool) == true ? Color.secondary : Color.primary)
            }
        case "error":
            if let text { Text(text).font(.callout).foregroundStyle(.red).textSelection(.enabled) }
        default:
            if let detail { Text(detail).font(.caption).foregroundStyle(.secondary) }
        }
    }

    @ViewBuilder
    private var details: some View {
        VStack(alignment: .leading, spacing: 8) {
            if !inputs.isEmpty {
                Text("Asked with").font(.caption2.weight(.semibold)).foregroundStyle(.secondary)
                KeyValueList(object: inputs)
            }
            if let output {
                Text(kind == "command" || kind == "data" ? "Printed" : "Got back").font(.caption2.weight(.semibold)).foregroundStyle(.secondary)
                if let object = ReadableJSON.object(output) {
                    KeyValueList(object: object)
                } else if kind == "command" || kind == "data" {
                    ConsoleText(text: output, maxHeight: 220)
                } else {
                    Text(output).font(.callout).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 9, style: .continuous).fill(Color.primary.opacity(0.04)))
        .overlay(RoundedRectangle(cornerRadius: 9, style: .continuous).strokeBorder(Color.primary.opacity(0.07)))
    }
}

private struct FileRow: View {
    let path: String
    let change: String?
    let note: String?

    var body: some View {
        Button {
            let url = URL(fileURLWithPath: (path as NSString).expandingTildeInPath)
            NSWorkspace.shared.activateFileViewerSelecting([url])
        } label: {
            HStack(spacing: 6) {
                Image(systemName: change == "add" ? "plus.circle.fill" : change == "delete" ? "minus.circle.fill" : "pencil.circle.fill")
                    .foregroundStyle(change == "add" ? Color.green : change == "delete" ? Color.red : Color.orange)
                Text(URL(fileURLWithPath: path).lastPathComponent).font(.callout.weight(.medium))
                Text(DetailPaths.tilde(URL(fileURLWithPath: path).deletingLastPathComponent().path))
                    .font(.caption).foregroundStyle(.secondary).lineLimit(1)
                if let note { Text("· \(note)").font(.caption).foregroundStyle(.secondary).lineLimit(1) }
            }
        }
        .buttonStyle(.plain)
        .help("Show in Finder")
    }
}

enum DetailPaths {
    static func tilde(_ path: String) -> String {
        let home = NSHomeDirectory()
        return path.hasPrefix(home) ? "~" + path.dropFirst(home.count) : path
    }
}

// MARK: - Values without JSON

enum ReadableJSON {
    static func object(_ text: String) -> [String: Any]? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.hasPrefix("{"), let data = trimmed.data(using: .utf8) else { return nil }
        return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
    }

    /// A value in words: lists become "3 items: a, b, c", nested objects their field names.
    static func describe(_ value: Any) -> String {
        switch value {
        case let text as String: return text
        case let number as NSNumber:
            if CFGetTypeID(number) == CFBooleanGetTypeID() { return number.boolValue ? "Yes" : "No" }
            return number.stringValue
        case let list as [Any]:
            if list.isEmpty { return "None" }
            let shown = list.prefix(3).map { item -> String in
                if let object = item as? [String: Any] {
                    for key in ["name", "title", "tool", "id", "path", "summary"] {
                        if let text = object[key] as? String { return text }
                    }
                    return "\(object.count) fields"
                }
                return describe(item)
            }
            return "\(list.count) item\(list.count == 1 ? "" : "s"): " + shown.joined(separator: ", ") + (list.count > 3 ? ", …" : "")
        case let object as [String: Any]:
            return object.keys.sorted().prefix(6).joined(separator: ", ") + (object.count > 6 ? ", …" : "")
        default:
            return "—"
        }
    }

    static func label(_ key: String) -> String {
        let spaced = key.replacingOccurrences(of: "_", with: " ").replacingOccurrences(of: "-", with: " ")
        return spaced.prefix(1).uppercased() + spaced.dropFirst()
    }
}

private struct KeyValueList: View {
    let object: [String: Any]

    private var rows: [(String, String)] {
        var out: [(String, String)] = []
        for key in object.keys.sorted() {
            guard let value = object[key] else { continue }
            if let nested = value as? [String: Any], nested.count <= 8, out.count < 24 {
                for inner in nested.keys.sorted() {
                    out.append(("\(ReadableJSON.label(key)) › \(ReadableJSON.label(inner))", ReadableJSON.describe(nested[inner] as Any)))
                }
            } else {
                out.append((ReadableJSON.label(key), ReadableJSON.describe(value)))
            }
        }
        return Array(out.prefix(30))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    Text(row.0).font(.caption).foregroundStyle(.secondary).frame(width: 150, alignment: .leading)
                    Text(row.1).font(.callout).textSelection(.enabled).lineLimit(4)
                    Spacer(minLength: 0)
                }
            }
        }
    }
}

struct ConsoleText: View {
    let text: String
    var maxHeight: CGFloat = 300

    var body: some View {
        ScrollView {
            Text(text)
                .font(.system(.caption, design: .monospaced))
                .foregroundStyle(Color(white: 0.88))
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(12)
        }
        .frame(maxHeight: maxHeight)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color(white: 0.09)))
        .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(Color.white.opacity(0.08)))
    }
}

// MARK: - Markdown

/// Headings, lists, quotes, code blocks, tables and inline styles (bold, italic, code, links),
/// which is what agents write. SwiftUI's own Markdown support covers inline styles only.
struct MarkdownText: View {
    let source: String
    var compact = false

    private enum Block {
        case heading(Int, String)
        case paragraph(String)
        case bullets([(Int, String)])
        case numbered([String])
        case quote(String)
        case code(String)
        case table([[String]])
        case rule
    }

    private var blocks: [Block] {
        var out: [Block] = []
        var paragraph: [String] = []
        var bullets: [(Int, String)] = []
        var numbered: [String] = []
        var table: [[String]] = []
        var code: [String]?
        func flush() {
            if !paragraph.isEmpty { out.append(.paragraph(paragraph.joined(separator: " "))); paragraph = [] }
            if !bullets.isEmpty { out.append(.bullets(bullets)); bullets = [] }
            if !numbered.isEmpty { out.append(.numbered(numbered)); numbered = [] }
            if !table.isEmpty { out.append(.table(table)); table = [] }
        }
        for raw in source.components(separatedBy: "\n") {
            let line = raw.trimmingCharacters(in: .whitespaces)
            if line.hasPrefix("```") {
                if let lines = code { out.append(.code(lines.joined(separator: "\n"))); code = nil } else { flush(); code = [] }
                continue
            }
            if code != nil { code?.append(raw); continue }
            if line.isEmpty { flush(); continue }
            if line.hasPrefix("|") && line.hasSuffix("|") {
                let cells = line.dropFirst().dropLast().components(separatedBy: "|").map { $0.trimmingCharacters(in: .whitespaces) }
                if !cells.allSatisfy({ $0.allSatisfy { "-: ".contains($0) } }) { table.append(cells) }
                continue
            }
            if let match = line.range(of: #"^#{1,6} "#, options: .regularExpression) {
                flush()
                out.append(.heading(line[match].count - 1, String(line[match.upperBound...])))
                continue
            }
            if line == "---" || line == "***" || line == "___" { flush(); out.append(.rule); continue }
            if let match = line.range(of: #"^[-*•+] "#, options: .regularExpression) {
                if !paragraph.isEmpty || !numbered.isEmpty { let keep = bullets; flush(); bullets = keep }
                let indent = raw.prefix(while: { $0 == " " }).count / 2
                bullets.append((min(indent, 3), String(line[match.upperBound...])))
                continue
            }
            if let match = line.range(of: #"^\d+[.)] "#, options: .regularExpression) {
                if !paragraph.isEmpty || !bullets.isEmpty { let keep = numbered; flush(); numbered = keep }
                numbered.append(String(line[match.upperBound...]))
                continue
            }
            if line.hasPrefix(">") {
                flush()
                out.append(.quote(line.drop(while: { $0 == ">" || $0 == " " }).description))
                continue
            }
            if !bullets.isEmpty || !numbered.isEmpty { flush() }
            paragraph.append(line)
        }
        if let lines = code { out.append(.code(lines.joined(separator: "\n"))) }
        flush()
        return out
    }

    var body: some View {
        VStack(alignment: .leading, spacing: compact ? 6 : 10) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                view(for: block)
            }
        }
    }

    private func inline(_ text: String) -> Text {
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        if let attributed = try? AttributedString(markdown: text, options: options) { return Text(attributed) }
        return Text(text)
    }

    @ViewBuilder
    private func view(for block: Block) -> some View {
        switch block {
        case .heading(let level, let text):
            inline(text)
                .font(level <= 1 ? .title3.weight(.bold) : level == 2 ? .headline : .subheadline.weight(.semibold))
                .padding(.top, compact ? 0 : 4)
        case .paragraph(let text):
            inline(text).font(.callout).fixedSize(horizontal: false, vertical: true)
        case .bullets(let items):
            VStack(alignment: .leading, spacing: 5) {
                ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Circle().fill(Color.secondary.opacity(item.0 == 0 ? 0.8 : 0.5)).frame(width: 5, height: 5)
                            .alignmentGuide(.firstTextBaseline) { $0[.bottom] - 1 }
                        inline(item.1).font(.callout).fixedSize(horizontal: false, vertical: true)
                    }
                    .padding(.leading, CGFloat(item.0) * 16)
                }
            }
        case .numbered(let items):
            VStack(alignment: .leading, spacing: 5) {
                ForEach(Array(items.enumerated()), id: \.offset) { index, item in
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text("\(index + 1).").font(.callout.monospacedDigit()).foregroundStyle(.secondary)
                        inline(item).font(.callout).fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        case .quote(let text):
            HStack(spacing: 10) {
                RoundedRectangle(cornerRadius: 2).fill(Color.accentColor.opacity(0.6)).frame(width: 3)
                inline(text).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
        case .code(let text):
            ConsoleText(text: text, maxHeight: 260)
        case .table(let rows):
            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array(rows.enumerated()), id: \.offset) { index, row in
                    HStack(alignment: .top, spacing: 12) {
                        ForEach(Array(row.enumerated()), id: \.offset) { _, cell in
                            inline(cell)
                                .font(index == 0 ? .caption.weight(.semibold) : .callout)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                    .padding(.vertical, 6).padding(.horizontal, 10)
                    .background(index == 0 ? Color.primary.opacity(0.06) : Color.clear)
                    if index < rows.count - 1 { Divider() }
                }
            }
            .background(RoundedRectangle(cornerRadius: 8).strokeBorder(Color.primary.opacity(0.10)))
        case .rule:
            Divider()
        }
    }
}

private extension String {
    var nilIfEmpty: String? { trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? nil : self }
}

// MARK: - Rows that open

/// A row that opens on a click, with the same action for accessibility (VoiceOver and Mac MCP's
/// own mac_act press it) and, on macOS 14 and later, for the keyboard (Return or Space when focused).
struct ActivatableRow: ViewModifier {
    let hint: String?
    let action: () -> Void

    @ViewBuilder
    func body(content: Content) -> some View {
        let row = content
            .contentShape(Rectangle())
            .onTapGesture(perform: action)
            .accessibilityAddTraits(.isButton)
            .accessibilityAction(.default, action)
            .accessibilityHint(hint.map { Text($0) } ?? Text(""))
        if #available(macOS 14.0, *) {
            row
                .focusable()
                .onKeyPress(.return) { action(); return .handled }
                .onKeyPress(.space) { action(); return .handled }
        } else {
            row
        }
    }
}

extension View {
    func activatable(_ hint: String? = nil, perform action: @escaping () -> Void) -> some View {
        modifier(ActivatableRow(hint: hint, action: action))
    }
}

/// What a pane shows when its first load failed: what happened and a way to try again,
/// instead of a spinner or an empty list that looks like "nothing here".
struct LoadProblem: View {
    let message: String
    let retry: () async -> Void
    @MacMCPState private var retrying = false

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "exclamationmark.triangle").foregroundStyle(.secondary)
                .accessibilityHidden(true)
            Text(message).font(.callout).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 8)
            if retrying {
                ProgressView().controlSize(.small)
            } else {
                Button("Try Again") {
                    retrying = true
                    Task {
                        await retry()
                        retrying = false
                    }
                }
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color.primary.opacity(0.05)))
    }
}
