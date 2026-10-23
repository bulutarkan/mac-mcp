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

/// Delegated-agent tokens as one list: every agent that used tokens, the busiest first, folded by default.
struct ProviderUsageSection: View {
    let summary: ProviderUsageSummaryEnvelope?
    let loading: Bool
    let issue: String?
    @Binding var period: ProviderUsagePeriod

    @MacMCPState private var expanded: Set<String> = []

    init(
        summary: ProviderUsageSummaryEnvelope?,
        loading: Bool,
        issue: String?,
        period: Binding<ProviderUsagePeriod>
    ) {
        self.summary = summary
        self.loading = loading
        self.issue = issue
        _period = period
    }

    var body: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 12) {
                header
                if loading && summary == nil {
                    HStack(spacing: 8) {
                        ProgressView().controlSize(.small)
                        Text("Loading provider usage…")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    .padding(.vertical, 6)
                } else if active.isEmpty {
                    emptyState
                } else {
                    shareBar
                    rows
                }
                footer
            }
            .padding(.vertical, 2)
        } label: {
            Label("Delegated Agents", systemImage: "cpu")
        }
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

    // MARK: Header

    private var header: some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 5) {
                    Text("Provider Tokens")
                        .font(.subheadline.weight(.semibold))
                    Image(systemName: "info.circle")
                        .font(.caption)
                        .foregroundStyle(.tertiary)
                        .help(
                            (summary?.metricScope ?? "Provider-native delegated-agent usage only.")
                                + " Never added to Tool data processed."
                        )
                        .accessibilityLabel("About Provider Tokens")
                }
                if !active.isEmpty {
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text(ProviderUsageFormat.compact(grandTotal))
                            .font(.system(size: 26, weight: .semibold, design: .rounded))
                            .monospacedDigit()
                            .help(ProviderUsageFormat.exact(grandTotal))
                        Text(active.count == 1 ? "tokens · 1 agent" : "tokens · \(active.count) agents")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            Spacer(minLength: 12)
            Picker("Period", selection: $period) {
                ForEach(ProviderUsagePeriod.allCases) { item in
                    Text(item.rawValue).tag(item)
                }
            }
            .labelsHidden()
            .pickerStyle(.segmented)
            .frame(width: 150)
        }
    }

    /// One bar split by each agent's share, in the same tint as its row.
    private var shareBar: some View {
        GeometryReader { proxy in
            let entries = active.filter { $0.total > 0 }
            let gap: CGFloat = 2
            let minimum: CGFloat = 4
            // Every agent keeps a visible sliver; the rest of the width is shared by tokens, so the bar never overflows.
            let spare = max(0, proxy.size.width - (gap + minimum) * CGFloat(entries.count) + gap)
            HStack(spacing: gap) {
                ForEach(entries) { entry in
                    Capsule(style: .continuous)
                        .fill(ProviderUsageStyle.of(entry.id).tint.gradient)
                        .frame(width: minimum + spare * CGFloat(entry.total) / CGFloat(max(1, grandTotal)))
                        .help("\(entry.title) · \(ProviderUsageFormat.share(entry.total, of: grandTotal))")
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(height: 8)
        .accessibilityHidden(true)
    }

    private var rows: some View {
        VStack(spacing: 0) {
            ForEach(Array(active.enumerated()), id: \.element.id) { index, entry in
                ProviderUsageRow(
                    entry: entry,
                    grandTotal: grandTotal,
                    isExpanded: expanded.contains(entry.id)
                ) {
                    withAnimation(.easeInOut(duration: 0.2)) {
                        if expanded.contains(entry.id) {
                            expanded.remove(entry.id)
                        } else {
                            expanded.insert(entry.id)
                        }
                    }
                }
                if index < active.count - 1 {
                    Divider().padding(.leading, 42)
                }
            }
        }
    }

    private var emptyState: some View {
        HStack(spacing: 10) {
            Image(systemName: "moon.zzz")
                .font(.title3)
                .foregroundStyle(.tertiary)
            Text("No delegated-agent activity in this period.")
                .font(.callout)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, 10)
    }

    @ViewBuilder
    private var footer: some View {
        if let issue {
            Label(issue, systemImage: "exclamationmark.triangle")
                .font(.caption2)
                .foregroundStyle(.orange)
        } else if summary != nil {
            HStack(alignment: .firstTextBaseline, spacing: 12) {
                VStack(alignment: .leading, spacing: 3) {
                    if !active.isEmpty, !idleTitles.isEmpty {
                        Text("No activity: \(idleTitles.joined(separator: ", "))")
                    }
                }
                .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 8)
                if let since = ProviderUsageFormat.since(summary?.availableSince) {
                    Text("Since \(since)")
                }
            }
            .font(.caption2)
            .foregroundStyle(.tertiary)
            .padding(.top, 2)
        }
    }
}

private struct ProviderUsageRow: View {
    let entry: ProviderUsageEntry
    let grandTotal: Int
    let isExpanded: Bool
    let toggle: () -> Void

    @MacMCPState private var hovering = false

    init(entry: ProviderUsageEntry, grandTotal: Int, isExpanded: Bool, toggle: @escaping () -> Void) {
        self.entry = entry
        self.grandTotal = grandTotal
        self.isExpanded = isExpanded
        self.toggle = toggle
    }

    private var style: ProviderUsageStyle { ProviderUsageStyle.of(entry.id) }
    private var usage: ProviderUsageProvider { entry.usage }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button(action: toggle) {
                HStack(spacing: 10) {
                    ProviderUsageTile(symbol: style.symbol, tint: style.tint)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(entry.title)
                            .font(.callout.weight(.medium))
                            .foregroundStyle(.primary)
                        Text(usage.agents == 1 ? "1 run" : "\(usage.agents) runs")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                    Spacer(minLength: 12)
                    Text(ProviderUsageFormat.compact(usage.totalTokens))
                        .font(.callout.weight(.semibold))
                        .monospacedDigit()
                        .foregroundStyle(.primary)
                        .help(ProviderUsageFormat.exact(usage.totalTokens))
                    Text(ProviderUsageFormat.share(entry.total, of: grandTotal))
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)
                        .frame(width: 38, alignment: .trailing)
                    Image(systemName: "chevron.right")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.tertiary)
                        .rotationEffect(.degrees(isExpanded ? 90 : 0))
                }
                .padding(.vertical, 8)
                .padding(.horizontal, 6)
                .contentShape(Rectangle())
                .background(
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .fill(Color.primary.opacity(hovering ? 0.05 : 0))
                )
            }
            .buttonStyle(.plain)
            .onHover { hovering = $0 }
            .accessibilityLabel(
                "\(entry.title), \(ProviderUsageFormat.compact(usage.totalTokens)) tokens, "
                    + "\(ProviderUsageFormat.share(entry.total, of: grandTotal)) of delegated-agent tokens"
            )
            .accessibilityHint(isExpanded ? "Hide details" : "Show details")

            if isExpanded {
                ProviderUsageDetail(usage: usage, tint: style.tint)
                    .padding(.leading, 48)
                    .padding(.trailing, 6)
                    .padding(.bottom, 12)
                    .transition(.opacity.combined(with: .move(edge: .top)))
            }
        }
    }
}

private struct ProviderUsageTile: View {
    let symbol: String
    let tint: Color

    var body: some View {
        Image(systemName: symbol)
            .font(.system(size: 12, weight: .semibold))
            .foregroundStyle(tint)
            .frame(width: 28, height: 28)
            .background(
                RoundedRectangle(cornerRadius: 7, style: .continuous)
                    .fill(tint.opacity(0.16))
            )
            .accessibilityHidden(true)
    }
}

/// What an open row adds: where the tokens went, and which models used them.
private struct ProviderUsageDetail: View {
    let usage: ProviderUsageProvider
    let tint: Color

    private static let modelLimit = 4

    private var cached: Int? {
        guard let read = usage.cacheReadTokens, let write = usage.cacheWriteTokens else { return nil }
        return read + write
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            // Each meter is its own share of the total: providers count cache and reasoning differently, so they never stack.
            HStack(alignment: .top, spacing: 14) {
                meter("Input", usage.inputTokens, help: nil)
                meter(
                    "Cached",
                    cached,
                    help: "Cache read \(ProviderUsageFormat.compact(usage.cacheReadTokens)) · "
                        + "cache write \(ProviderUsageFormat.compact(usage.cacheWriteTokens))"
                )
                meter("Output", usage.outputTokens, help: nil)
                meter("Reasoning", usage.reasoningTokens, help: nil)
            }

            if !usage.models.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(usage.models.prefix(Self.modelLimit)) { model in
                        HStack(spacing: 8) {
                            Text(model.model)
                                .font(.system(.caption, design: .monospaced))
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                                .truncationMode(.middle)
                            Spacer(minLength: 8)
                            Text(ProviderUsageFormat.compact(model.totalTokens))
                                .font(.caption.weight(.medium).monospacedDigit())
                                .help(ProviderUsageFormat.exact(model.totalTokens))
                        }
                    }
                    if usage.models.count > Self.modelLimit {
                        Text("+\(usage.models.count - Self.modelLimit) more models")
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                    }
                }
            }

            Label(sourceText, systemImage: sourceSymbol)
                .font(.caption2)
                .foregroundStyle(.tertiary)
        }
    }

    private var sourceText: String {
        let turns = usage.turns == 1 ? "1 turn" : "\(usage.turns) turns"
        switch usage.source {
        case "report": return "\(turns) · reported by the provider"
        case "native_tokenizer": return "\(turns) · counted with the provider's tokenizer"
        case "estimate": return "\(turns) · estimated"
        case "mixed": return "\(turns) · reported and estimated"
        default: return turns
        }
    }

    private var sourceSymbol: String {
        usage.source == "report" ? "checkmark.seal" : "info.circle"
    }

    private func meter(_ title: String, _ value: Int?, help: String?) -> some View {
        let total = max(1, usage.totalTokens ?? 0)
        let fraction = value.map { min(1, max(0, Double($0) / Double(total))) } ?? 0
        return VStack(alignment: .leading, spacing: 4) {
            Text(title)
                .font(.caption2)
                .foregroundStyle(.secondary)
            Text(ProviderUsageFormat.compact(value))
                .font(.callout.weight(.semibold))
                .monospacedDigit()
                .lineLimit(1)
                .minimumScaleFactor(0.8)
            GeometryReader { proxy in
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.primary.opacity(0.08))
                    Capsule()
                        .fill(tint.opacity(0.85))
                        .frame(width: value == nil ? 0 : max(3, proxy.size.width * fraction))
                }
            }
            .frame(height: 4)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .help(help ?? ProviderUsageFormat.exact(value))
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
