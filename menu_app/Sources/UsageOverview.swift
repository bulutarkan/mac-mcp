import SwiftUI

/// The tools that moved the most data, busiest first: calls, a share meter and the tokens, with a mark when a
/// noticeable part of the calls failed. The first five show; the rest open on request.
struct UsageTopToolsList: View {
    let tools: [UsageTool]
    let total: Int

    @MacMCPState private var showAll = false

    private static let folded = 5
    private static let failureMark = 0.05

    init(tools: [UsageTool], total: Int) {
        self.tools = tools
        self.total = total
    }

    var body: some View {
        let sorted = tools.sorted { $0.totalTokens > $1.totalTokens }
        let top = max(1, sorted.first?.totalTokens ?? 1)
        let shown = showAll ? sorted : Array(sorted.prefix(Self.folded))
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(shown.enumerated()), id: \.element.id) { index, tool in
                if index > 0 { Divider() }
                UsageRow(title: tool.tool, subtitle: subtitle(tool),
                         share: Double(tool.totalTokens) / Double(top), color: UsageColors.results,
                         amount: UsageFormat.compact(tool.totalTokens),
                         percent: UsageFormat.share(tool.totalTokens, of: total),
                         help: "\(UsageFormat.exact(tool.totalTokens)) tokens") {
                    UsageSymbolTile(symbol: Self.symbol(tool.tool))
                } detail: {
                    EmptyView()
                }
                .accessibilityLabel(
                    "\(tool.tool), \(UsageFormat.compact(tool.totalTokens)) tokens, \(tool.calls) calls"
                        + (tool.errorCount > 0 ? ", \(tool.errorCount) failed" : "")
                )
            }
            if sorted.count > Self.folded {
                Divider()
                Button(showAll ? "Show fewer" : "Show all \(sorted.count) tools") {
                    withAnimation(.easeInOut(duration: 0.2)) { showAll.toggle() }
                }
                .buttonStyle(.link)
                .font(.system(size: 12.5))
                .padding(.horizontal, 14).padding(.vertical, 9)
            }
        }
        .usageListBox()
    }

    /// "2,205 calls, 144 failed": most tools fail now and then, so only a noticeable failure rate is named.
    private func subtitle(_ tool: UsageTool) -> Text {
        let calls = Text(UsageFormat.count(tool.calls, "call"))
        guard tool.calls > 0, Double(tool.errorCount) / Double(tool.calls) >= Self.failureMark else { return calls }
        return calls + Text(", ") + Text("\(UsageFormat.exact(tool.errorCount)) failed").foregroundColor(UsageColors.warning)
    }

    static func symbol(_ tool: String) -> String {
        if tool.hasPrefix("browser") || tool == "open_url" { return "safari" }
        if tool.hasPrefix("run_") || tool.contains("job") { return "terminal" }
        if tool.contains("file") || tool.contains("edit") { return "doc.text" }
        if tool.hasPrefix("memory") { return "brain" }
        if tool.hasPrefix("skill") { return "book.closed" }
        if tool.hasPrefix("mac_") || tool.hasPrefix("computer") { return "macwindow" }
        if tool.contains("agent") || tool.hasPrefix("team") { return "person.2" }
        if tool.contains("search") || tool.contains("discover") || tool.contains("find") { return "magnifyingglass" }
        return "wrench.and.screwdriver"
    }
}

/// How often AI apps and agents looked up and saved memories and used skills (#110). Counts only.
struct MemorySkillUsageView: View {
    let usage: MemorySkillUsage

    init(usage: MemorySkillUsage) {
        self.usage = usage
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            UsageFactsRow(facts: [
                UsageFact(value: UsageFormat.exact(usage.count("memory_search")), title: "Memory lookups",
                          note: "\(UsageFormat.exact(usage.count("memory_search", "matched"))) found something."),
                UsageFact(value: UsageFormat.exact(usage.count("memory_add", "saved")), title: "Memories saved",
                          note: "\(UsageFormat.exact(usage.count("memory_add", "already_saved"))) were already known."),
                UsageFact(value: UsageFormat.exact(usage.count("memory_update", "updated")), title: "Memories updated",
                          note: "Changed when something new came up."),
                UsageFact(value: UsageFormat.exact(usage.count("skill_get", "loaded")), title: "Skills used",
                          note: "After \(UsageFormat.count(usage.count("skill_search"), "skill search", plural: "skill searches")).")
            ], boxed: false)
            if !problems.isEmpty {
                Text(problems.joined(separator: "; ") + ".")
                    .font(.system(size: 11.5)).foregroundStyle(.secondary)
                    .padding(.horizontal, 14).padding(.bottom, 10)
            }
            if !usage.skillsLoaded.isEmpty {
                Divider()
                VStack(spacing: 0) {
                    ForEach(Array(usage.skillsLoaded.enumerated()), id: \.element.name) { index, skill in
                        if index > 0 { Divider() }
                        HStack(spacing: 8) {
                            Image(systemName: "book.closed").font(.system(size: 12)).foregroundStyle(.secondary)
                                .accessibilityHidden(true)
                            Text(skill.name).font(.system(size: 13))
                            Spacer()
                            Text(skill.count == 1 ? "once" : "\(UsageFormat.exact(skill.count)) times")
                                .font(.system(size: 12.5)).foregroundStyle(.secondary).monospacedDigit()
                        }
                        .padding(.vertical, 8)
                        .accessibilityElement(children: .combine)
                    }
                }
                .padding(.horizontal, 14)
            }
        }
        .usageListBox()
    }

    /// What went wrong, if anything: saves refused for holding a secret, skills that were asked for but missing.
    private var problems: [String] {
        var parts: [String] = []
        let refused = usage.count("memory_add", "rejected_secret") + usage.count("memory_update", "rejected_secret")
        if refused > 0 { parts.append("\(UsageFormat.count(refused, "save")) refused because \(refused == 1 ? "it" : "they") held a secret") }
        let missing = usage.count("skill_get", "not_found")
        if missing > 0 { parts.append("\(UsageFormat.count(missing, "skill")) not found") }
        return parts
    }
}
