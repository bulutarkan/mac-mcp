import SwiftUI

/// One headline figure under a small label, for the Usage overview's stat row.
struct UsageStat: View {
    let title: String
    let value: String
    var help: String? = nil
    var tint: Color? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(value)
                .font(.callout.weight(.semibold))
                .monospacedDigit()
                .foregroundStyle(tint ?? .primary)
                .lineLimit(1)
                .minimumScaleFactor(0.8)
            Text(title)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(1)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .help(help ?? "")
        .accessibilityElement(children: .combine)
    }
}

/// Two parts of one whole as a single bar, with each part named underneath.
struct UsageSplitBar: View {
    let first: (label: String, value: Int)
    let second: (label: String, value: Int)
    let format: (Int) -> String

    var body: some View {
        let total = max(1, first.value + second.value)
        VStack(alignment: .leading, spacing: 6) {
            GeometryReader { proxy in
                let gap: CGFloat = 2
                let usable = max(0, proxy.size.width - gap)
                let firstWidth = first.value + second.value == 0
                    ? usable / 2
                    : usable * CGFloat(first.value) / CGFloat(total)
                HStack(spacing: gap) {
                    Capsule(style: .continuous)
                        .fill(Color.accentColor.gradient)
                        .frame(width: max(first.value > 0 ? 4 : 0, firstWidth))
                    Capsule(style: .continuous)
                        .fill(Color.accentColor.opacity(0.45))
                }
            }
            .frame(height: 6)
            .accessibilityHidden(true)
            HStack(spacing: 14) {
                legend(first.label, first.value, Color.accentColor)
                legend(second.label, second.value, Color.accentColor.opacity(0.45))
                Spacer(minLength: 0)
            }
        }
    }

    private func legend(_ label: String, _ value: Int, _ color: Color) -> some View {
        HStack(spacing: 5) {
            Circle().fill(color).frame(width: 7, height: 7)
            Text(label).foregroundStyle(.secondary)
            Text(format(value)).monospacedDigit()
        }
        .font(.caption)
        .accessibilityElement(children: .combine)
    }
}

/// The tools that moved the most payload: the first few, the rest on request.
struct UsageTopToolsList: View {
    let tools: [UsageTool]
    let format: (Int) -> String

    @MacMCPState private var showAll = false

    private static let folded = 5
    private static let failureMark = 0.05

    init(tools: [UsageTool], format: @escaping (Int) -> String) {
        self.tools = tools
        self.format = format
    }

    var body: some View {
        let maxTokens = max(1, tools.map(\.totalTokens).max() ?? 1)
        let shown = showAll ? tools : Array(tools.prefix(Self.folded))
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(shown.enumerated()), id: \.element.id) { index, tool in
                HStack(spacing: 12) {
                    Text(tool.tool)
                        .font(.system(.caption, design: .monospaced).weight(.medium))
                        .lineLimit(1)
                        .truncationMode(.middle)
                        .frame(width: 150, alignment: .leading)
                    GeometryReader { proxy in
                        ZStack(alignment: .leading) {
                            Capsule().fill(Color.primary.opacity(0.07))
                            Capsule()
                                .fill(Color.accentColor.opacity(0.8))
                                .frame(width: max(3, proxy.size.width * CGFloat(tool.totalTokens) / CGFloat(maxTokens)))
                        }
                    }
                    .frame(height: 5)
                    .accessibilityHidden(true)
                    Text(format(tool.totalTokens))
                        .font(.caption.weight(.semibold).monospacedDigit())
                        .frame(width: 52, alignment: .trailing)
                    Text("\(format(tool.calls)) calls")
                        .font(.caption2.monospacedDigit())
                        .foregroundStyle(.secondary)
                        .frame(width: 70, alignment: .trailing)
                    Group {
                        // Most tools fail now and then; only a noticeable failure rate earns the mark.
                        if tool.calls > 0, Double(tool.errorCount) / Double(tool.calls) >= Self.failureMark {
                            Image(systemName: "exclamationmark.circle.fill")
                                .foregroundStyle(.orange)
                                .help("\(tool.errorCount) of \(tool.calls) calls failed")
                        } else {
                            Color.clear
                        }
                    }
                    .font(.caption)
                    .frame(width: 14)
                }
                .padding(.vertical, 7)
                .accessibilityElement(children: .combine)
                .accessibilityLabel(
                    "\(tool.tool), \(format(tool.totalTokens)) tokens, \(tool.calls) calls"
                        + (tool.errorCount > 0 ? ", \(tool.errorCount) failed" : "")
                )
                if index < shown.count - 1 { Divider() }
            }
            if tools.count > Self.folded {
                Button(showAll ? "Show fewer" : "Show all \(tools.count)") {
                    withAnimation(.easeInOut(duration: 0.2)) { showAll.toggle() }
                }
                .buttonStyle(.link)
                .font(.caption)
                .padding(.top, 6)
            }
        }
    }
}

/// How often AI apps and agents looked up and saved memories and used skills (#110). Counts only.
struct MemorySkillUsageView: View {
    let usage: MemorySkillUsage
    let format: (Int) -> String

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 12) {
                UsageStat(title: "Memory lookups", value: format(usage.count("memory_search")),
                          help: "\(format(usage.count("memory_search", "matched"))) found something")
                UsageStat(title: "Memories saved", value: format(usage.count("memory_add", "saved")),
                          help: "\(format(usage.count("memory_add", "already_saved"))) were already saved")
                UsageStat(title: "Memories updated", value: format(usage.count("memory_update", "updated")))
                UsageStat(title: "Skills used", value: format(usage.count("skill_get", "loaded")),
                          help: "\(format(usage.count("skill_search"))) skill searches")
            }
            Text(detailLine)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if !usage.skillsLoaded.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(usage.skillsLoaded, id: \.name) { skill in
                        HStack {
                            Image(systemName: "book.closed")
                                .foregroundStyle(.secondary)
                                .accessibilityHidden(true)
                            Text(skill.name).font(.caption)
                            Spacer()
                            Text("\(format(skill.count))×")
                                .font(.caption.weight(.semibold))
                                .monospacedDigit()
                        }
                        .accessibilityElement(children: .combine)
                    }
                }
            }
        }
    }

    private var detailLine: String {
        var parts = ["\(format(usage.count("memory_search", "matched"))) of \(format(usage.count("memory_search"))) lookups found something"]
        let already = usage.count("memory_add", "already_saved")
        if already > 0 { parts.append(already == 1 ? "1 save was already known" : "\(format(already)) saves were already known") }
        let refused = usage.count("memory_add", "rejected_secret") + usage.count("memory_update", "rejected_secret")
        if refused > 0 { parts.append("\(format(refused)) refused because \(refused == 1 ? "it" : "they") held a secret") }
        let missing = usage.count("skill_get", "not_found")
        if missing > 0 { parts.append(missing == 1 ? "1 skill not found" : "\(format(missing)) skills not found") }
        return parts.joined(separator: " · ") + "."
    }
}
