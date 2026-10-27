import SwiftUI

enum ProviderUsagePeriod: String, CaseIterable, Identifiable {
    case month = "30D"
    case quarter = "90D"
    case year = "1Y"

    var id: String { rawValue }

    var days: Int {
        switch self {
        case .month: return 30
        case .quarter: return 90
        case .year: return 365
        }
    }

    var name: String {
        switch self {
        case .month: return "30 Days"
        case .quarter: return "90 Days"
        case .year: return "Year"
        }
    }

    /// The days of the period as bars, today last; a year as its twelve months. `byDate` is each
    /// "yyyy-MM-dd" day's tokens by series.
    func buckets(_ byDate: [String: [String: Int]], today: Date = Date(), calendar: Calendar = .current) -> [UsageBucket] {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.calendar = calendar
        formatter.dateFormat = "yyyy-MM-dd"
        let start = calendar.startOfDay(for: today)
        if self == .year {
            return (0..<12).map { index in
                let month = calendar.date(byAdding: .month, value: index - 11, to: start) ?? start
                let parts = calendar.dateComponents([.year, .month], from: month)
                var sum: [String: Int] = [:]
                for (day, split) in byDate {
                    guard let date = formatter.date(from: day) else { continue }
                    let other = calendar.dateComponents([.year, .month], from: date)
                    if other.year == parts.year && other.month == parts.month {
                        sum.merge(split, uniquingKeysWith: +)
                    }
                }
                return UsageBucket(id: index, title: month.formatted(.dateTime.month(.wide).year()),
                                   axis: month.formatted(.dateTime.month(.abbreviated)), parts: sum)
            }
        }
        let count = days
        return (0..<count).map { index in
            let day = calendar.date(byAdding: .day, value: index - (count - 1), to: start) ?? start
            let name = index == count - 1 ? "Today" : day.formatted(.dateTime.month(.abbreviated).day())
            return UsageBucket(id: index, title: name, axis: name, parts: byDate[formatter.string(from: day)] ?? [:])
        }
    }
}

/// How one delegated agent looks in Usage: its symbol and the tint its share and meters use.
struct ProviderUsageStyle {
    let symbol: String
    let tint: Color

    static func of(_ id: String) -> ProviderUsageStyle {
        switch id {
        case "codex": return ProviderUsageStyle(symbol: "terminal", tint: .blue)
        case "opencode": return ProviderUsageStyle(symbol: "chevron.left.forwardslash.chevron.right", tint: .teal)
        case "claude": return ProviderUsageStyle(symbol: "sparkles", tint: .orange)
        case "kimi": return ProviderUsageStyle(symbol: "moon.stars", tint: .purple)
        case "grok": return ProviderUsageStyle(symbol: "hammer", tint: .indigo)
        case "antigravity": return ProviderUsageStyle(symbol: "arrow.up.circle", tint: .pink)
        case "pi": return ProviderUsageStyle(symbol: "function", tint: .mint)
        case "kilo": return ProviderUsageStyle(symbol: "k.circle", tint: .yellow)
        case "cline": return ProviderUsageStyle(symbol: "c.circle", tint: .cyan)
        case "goose": return ProviderUsageStyle(symbol: "bird", tint: .green)
        case "copilot": return ProviderUsageStyle(symbol: "airplane", tint: .gray)
        default: return ProviderUsageStyle(symbol: "cpu", tint: .gray)
        }
    }
}

private struct ProviderUsageEntry: Identifiable {
    let id: String
    let title: String
    let usage: ProviderUsageProvider

    var total: Int { usage.totalTokens ?? 0 }
}

/// Delegated-agent tokens: the total in a sentence, each agent's share in one bar, and a row per agent that
/// opens to where its tokens went, the models it used and who reported them.
struct ProviderUsageSection: View {
    let summary: ProviderUsageSummaryEnvelope?
    let loading: Bool
    let issue: String?
    @Binding var period: ProviderUsagePeriod
    /// Opens Usage › Teams, from the line that says how much of this saved teams used.
    var showTeams: (() -> Void)?

    @MacMCPState private var expanded: Set<String> = []

    init(
        summary: ProviderUsageSummaryEnvelope?,
        loading: Bool,
        issue: String?,
        period: Binding<ProviderUsagePeriod>,
        showTeams: (() -> Void)? = nil
    ) {
        self.summary = summary
        self.loading = loading
        self.issue = issue
        _period = period
        self.showTeams = showTeams
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            if loading && summary == nil {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("Loading provider usage…").font(.callout).foregroundStyle(.secondary)
                }
                .padding(.top, 18)
            } else if active.isEmpty {
                Text("No delegated-agent activity in this period.")
                    .font(.callout).foregroundStyle(.secondary)
                    .padding(.top, 18)
            } else {
                shareBar.padding(.top, 22)
                Text("Agents").font(.system(size: 13, weight: .semibold)).padding(.top, 28).padding(.bottom, 8)
                rows
                teamsLine.padding(.top, 8)
            }
            footer.padding(.top, 10)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    // MARK: Data

    private var active: [ProviderUsageEntry] {
        (summary?.providers ?? [:])
            .compactMap { id, usage -> ProviderUsageEntry? in
                guard usage.available != false, usage.turns > 0 else { return nil }
                return ProviderUsageEntry(id: id, title: AgentProviders.name(id) ?? id, usage: usage)
            }
            .sorted { lhs, rhs in
                if lhs.total != rhs.total { return lhs.total > rhs.total }
                if lhs.usage.turns != rhs.usage.turns { return lhs.usage.turns > rhs.usage.turns }
                return lhs.title.localizedCaseInsensitiveCompare(rhs.title) == .orderedAscending
            }
    }

    private var grandTotal: Int { active.reduce(0) { $0 + $1.total } }

    /// Every other known agent, so the list always accounts for all of them.
    private var idleTitles: [String] {
        let activeIDs = Set(active.map(\.id))
        return AgentProviders.names
            .filter { !activeIDs.contains($0.key) }
            .map(\.value)
            .sorted { $0.localizedCaseInsensitiveCompare($1) == .orderedAscending }
    }

    // MARK: Summary

    private var header: some View {
        HStack(alignment: .top, spacing: 16) {
            VStack(alignment: .leading, spacing: 4) {
                HStack(alignment: .firstTextBaseline, spacing: 5) {
                    Text(UsageFormat.compact(grandTotal))
                        .font(.system(size: 34, weight: .semibold)).monospacedDigit()
                        .help(ProviderUsageFormat.exact(grandTotal))
                    Text("tokens").font(.system(size: 15, weight: .medium)).foregroundStyle(.secondary)
                }
                if !active.isEmpty {
                    let runs = active.reduce(0) { $0 + $1.usage.agents }
                    let turns = active.reduce(0) { $0 + $1.usage.turns }
                    Text("\(UsageFormat.count(active.count, "agent")) worked in \(UsageFormat.count(runs, "run")), \(UsageFormat.count(turns, "turn")) in all.")
                        .font(.system(size: 13)).foregroundStyle(.secondary)
                }
                HStack(spacing: 5) {
                    Text("What the agents’ own providers reported, counted apart from tool data.")
                    Image(systemName: "info.circle")
                        .help((summary?.metricScope ?? "Provider-native delegated-agent usage only.") + " Never added to tool data.")
                        .accessibilityLabel("About Provider Tokens")
                }
                .font(.system(size: 12)).foregroundStyle(.tertiary)
            }
            .accessibilityElement(children: .combine)
            Spacer(minLength: 12)
            Picker("Period", selection: $period) {
                ForEach(ProviderUsagePeriod.allCases) { Text($0.name).tag($0) }
            }
            .labelsHidden()
            .pickerStyle(.segmented)
            .fixedSize()
        }
    }

    /// One bar split by each agent's share, in the colour of its row, with the shares named under it.
    private var shareBar: some View {
        VStack(alignment: .leading, spacing: 9) {
            GeometryReader { proxy in
                let entries = active.filter { $0.total > 0 }
                let gap: CGFloat = 2
                let minimum: CGFloat = 4
                // Every agent keeps a visible sliver; the rest of the width is shared by tokens, so the bar never overflows.
                let spare = max(0, proxy.size.width - (gap + minimum) * CGFloat(entries.count) + gap)
                HStack(spacing: gap) {
                    ForEach(entries) { entry in
                        RoundedRectangle(cornerRadius: 3, style: .continuous)
                            .fill(UsageColors.agent(entry.id))
                            .frame(width: minimum + spare * CGFloat(entry.total) / CGFloat(max(1, grandTotal)))
                            .help("\(entry.title), \(ProviderUsageFormat.share(entry.total, of: grandTotal))")
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(height: 10)
            .accessibilityHidden(true)
            HStack(spacing: 16) {
                ForEach(active) { entry in
                    HStack(spacing: 5) {
                        RoundedRectangle(cornerRadius: 2).fill(UsageColors.agent(entry.id)).frame(width: 8, height: 8)
                        Text(entry.title).foregroundStyle(.secondary)
                        Text(ProviderUsageFormat.share(entry.total, of: grandTotal)).monospacedDigit()
                    }
                    .accessibilityElement(children: .combine)
                }
            }
            .font(.system(size: 12))
        }
    }

    private var rows: some View {
        VStack(spacing: 0) {
            ForEach(Array(active.enumerated()), id: \.element.id) { index, entry in
                if index > 0 { Divider() }
                let open = expanded.contains(entry.id)
                UsageRow(title: entry.title,
                         subtitle: Text(UsageFormat.count(entry.usage.agents, "run")),
                         share: Double(entry.total) / Double(max(1, active.first?.total ?? 1)),
                         color: UsageColors.agent(entry.id),
                         amount: UsageFormat.compact(entry.total),
                         percent: ProviderUsageFormat.share(entry.total, of: grandTotal),
                         help: ProviderUsageFormat.exact(entry.usage.totalTokens),
                         open: open,
                         toggle: {
                             withAnimation(.easeOut(duration: 0.2)) {
                                 if open { expanded.remove(entry.id) } else { expanded.insert(entry.id) }
                             }
                         }) {
                    UsageAgentLogo(agent: entry.id)
                } detail: {
                    ProviderUsageDetail(id: entry.id, usage: entry.usage)
                }
            }
        }
        .usageListBox()
    }

    /// Tokens saved teams used, counted inside the agents above; nothing when no team ran.
    private var teamTokens: Int { active.reduce(0) { $0 + ($1.usage.teamTokens ?? 0) } }

    @ViewBuilder
    private var teamsLine: some View {
        if teamTokens > 0 {
            HStack(spacing: 4) {
                Text("\(UsageFormat.compact(teamTokens)) of this came from teams")
                    .help(ProviderUsageFormat.exact(teamTokens))
                if let showTeams {
                    Text("·").accessibilityHidden(true)
                    Button("Show in Teams ›", action: showTeams)
                        .buttonStyle(.link)
                        .accessibilityLabel("Show team usage")
                }
            }
            .font(.system(size: 12))
            .foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private var footer: some View {
        if let issue {
            Label(issue, systemImage: "exclamationmark.triangle")
                .font(.caption2)
                .foregroundStyle(.orange)
        } else if summary != nil {
            HStack(alignment: .firstTextBaseline, spacing: 16) {
                if !active.isEmpty, !idleTitles.isEmpty {
                    Text("No activity: \(idleTitles.joined(separator: ", ")).")
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 8)
                if let since = ProviderUsageFormat.since(summary?.availableSince) {
                    Text("Since \(since)")
                }
            }
            .font(.system(size: 12))
            .foregroundStyle(.tertiary)
        }
    }
}

/// What an open row adds: where the tokens went, which models used them, and who reported them.
private struct ProviderUsageDetail: View {
    let id: String
    let usage: ProviderUsageProvider

    private static let modelLimit = 4

    private var cached: Int? {
        guard let read = usage.cacheReadTokens, let write = usage.cacheWriteTokens else { return nil }
        return read + write
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            // Each meter is its own share of the total: providers count cache and reasoning differently, so they never stack.
            HStack(alignment: .top, spacing: 16) {
                part("Input", usage.inputTokens, note: nil)
                part("Cached", cached, note: cached == nil ? nil
                     : "\(UsageFormat.compact(usage.cacheReadTokens ?? 0)) read, \(UsageFormat.compact(usage.cacheWriteTokens ?? 0)) written")
                part("Output", usage.outputTokens, note: nil)
                part("Reasoning", usage.reasoningTokens, note: nil)
            }
            .padding(.trailing, 22)
            if !usage.models.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(usage.models.prefix(Self.modelLimit)) { model in
                        HStack(spacing: 8) {
                            Text(model.model).font(.system(size: 12.5)).lineLimit(1).truncationMode(.middle)
                            Spacer(minLength: 8)
                            Text(ProviderUsageFormat.compact(model.totalTokens).replacingOccurrences(of: ".0", with: ""))
                                .font(.system(size: 12)).foregroundStyle(.secondary).monospacedDigit()
                                .help(ProviderUsageFormat.exact(model.totalTokens))
                                .padding(.trailing, 22)
                        }
                    }
                    if usage.models.count > Self.modelLimit {
                        Text("and \(usage.models.count - Self.modelLimit) more models")
                            .font(.system(size: 11.5)).foregroundStyle(.secondary)
                    }
                }
            }
            Label(sourceText, systemImage: usage.source == "report" ? "checkmark.seal" : "info.circle")
                .font(.system(size: 11.5))
                .foregroundStyle(.tertiary)
        }
    }

    private var sourceText: String {
        let turns = UsageFormat.count(usage.turns, "turn")
        switch usage.source {
        case "report": return "\(turns), reported by \(reporter)."
        case "native_tokenizer": return "\(turns), counted with the provider's tokenizer."
        case "estimate": return "\(turns), estimated."
        case "mixed": return "\(turns), reported and estimated."
        default: return turns + "."
        }
    }

    private var reporter: String {
        switch id {
        case "codex": return "OpenAI"
        case "claude": return "Anthropic"
        case "copilot": return "GitHub"
        case "antigravity": return "Google"
        case "grok": return "xAI"
        default: return "the provider"
        }
    }

    private func part(_ title: String, _ value: Int?, note: String?) -> some View {
        let total = max(1, usage.totalTokens ?? 0)
        let fraction = value.map { min(1, max(0, Double($0) / Double(total))) } ?? 0
        return VStack(alignment: .leading, spacing: 3) {
            Text(title).font(.system(size: 11.5)).foregroundStyle(.secondary)
            Text(value.map(UsageFormat.compact) ?? "—").font(.system(size: 13.5, weight: .semibold)).monospacedDigit()
            UsageShareMeter(share: value == nil ? 0 : fraction, color: UsageColors.agent(id), height: 4)
                .padding(.top, 2)
            if let note {
                Text(note).font(.system(size: 11)).foregroundStyle(.tertiary).lineLimit(2)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .help(ProviderUsageFormat.exact(value))
        .accessibilityElement(children: .combine)
    }
}

enum ProviderUsageFormat {
    static func compact(_ value: Int?) -> String {
        guard let value else { return "—" }
        let absolute = Double(abs(value))
        if absolute >= 1_000_000_000 { return String(format: "%.1fB", Double(value) / 1_000_000_000) }
        if absolute >= 1_000_000 { return String(format: "%.1fM", Double(value) / 1_000_000) }
        if absolute >= 1_000 { return String(format: "%.1fK", Double(value) / 1_000) }
        return "\(value)"
    }

    static func exact(_ value: Int?) -> String {
        guard let value else { return "Unavailable for part of this period" }
        return NumberFormatter.localizedString(from: NSNumber(value: value), number: .decimal) + " tokens"
    }

    static func share(_ value: Int, of total: Int) -> String {
        guard total > 0 else { return "—" }
        let percent = Double(value) / Double(total) * 100
        if value > 0 && percent < 1 { return "<1%" }
        return "\(Int(percent.rounded()))%"
    }

    /// "2026-10-05" as the owner's medium date; anything unparseable stays as given.
    static func since(_ day: String?) -> String? {
        guard let day, !day.isEmpty else { return nil }
        let parser = DateFormatter()
        parser.locale = Locale(identifier: "en_US_POSIX")
        parser.dateFormat = "yyyy-MM-dd"
        guard let date = parser.date(from: day) else { return day }
        return date.formatted(date: .abbreviated, time: .omitted)
    }
}
