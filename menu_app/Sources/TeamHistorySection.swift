import SwiftUI

/// A titled part of the Agents pane that folds open and closed, so Providers and Team History
/// share one screen without crowding it (#263).
struct AgentsFoldingSection<Content: View>: View {
    let title: String
    @ViewBuilder let content: () -> Content

    @MacMCPState private var expanded: Bool

    init(title: String, initiallyExpanded: Bool = true, @ViewBuilder content: @escaping () -> Content) {
        self.title = title
        self.content = content
        _expanded = MacMCPState(initialValue: initiallyExpanded)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Button {
                withAnimation(.easeInOut(duration: 0.2)) { expanded.toggle() }
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "chevron.right")
                        .font(.system(size: 9, weight: .bold))
                        .rotationEffect(.degrees(expanded ? 90 : 0))
                    Text(title.uppercased())
                        .font(.system(size: 10, weight: .semibold))
                        .tracking(0.5)
                    Spacer(minLength: 0)
                }
                .foregroundStyle(.tertiary)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(title)
            .accessibilityValue(expanded ? "Expanded" : "Collapsed")
            .accessibilityHint("Shows or hides this section")

            if expanded {
                content()
                    .transition(.opacity)
            }
        }
    }
}

enum TeamHistoryFormat {
    static func duration(_ milliseconds: Int?) -> String {
        guard let milliseconds else { return "—" }
        let seconds = max(0, milliseconds / 1000)
        if seconds < 60 { return "\(seconds)s" }
        if seconds < 3600 { return "\(seconds / 60)m \(seconds % 60)s" }
        return "\(seconds / 3600)h \((seconds % 3600) / 60)m"
    }

    static func spokenDuration(_ milliseconds: Int?) -> String {
        guard let milliseconds else { return "duration unknown" }
        let seconds = max(0, milliseconds / 1000)
        if seconds < 60 { return "\(seconds) seconds" }
        if seconds < 3600 { return "\(seconds / 60) minutes \(seconds % 60) seconds" }
        return "\(seconds / 3600) hours \((seconds % 3600) / 60) minutes"
    }

    static func tokens(_ value: Int?) -> String {
        value == nil ? "Unknown" : ProviderUsageFormat.compact(value)
    }

    static func outcome(_ status: String?, _ outcome: String?) -> (text: String, symbol: String, tint: Color) {
        let state = (status ?? "").lowercased()
        switch state {
        case "running", "starting", "queued", "pending":
            return ("Running", "clock.fill", .blue)
        case "cancelled":
            return ("Cancelled", "minus.circle.fill", .secondary)
        case "failed", "timed_out", "killed", "interrupted", "budget_exhausted", "quality_failed":
            return ("Failed", "xmark.circle.fill", .red)
        case "completed_with_failures":
            return ("Finished with failures", "exclamationmark.circle.fill", .orange)
        default:
            if let outcome, outcome.contains("fail") {
                return ("Finished with problems", "exclamationmark.circle.fill", .orange)
            }
            return ("Succeeded", "checkmark.circle.fill", .green)
        }
    }
}

/// Past agent teams, newest first: each team's agents with their tool calls, tokens and time.
struct TeamHistorySection: View {
    let history: TeamHistoryEnvelope?
    let loading: Bool
    let issue: String?
    let showMore: () -> Void

    @MacMCPState private var expanded: Set<String> = []

    private var teams: [TeamHistoryTeam] { history?.teams ?? [] }

    private var retentionText: String {
        guard let days = history?.retentionDays else { return "for as long as Agent History keeps agents" }
        return days == 0 ? "until you delete them" : "for \(days) days, like Agent History"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Agent teams that ran on this Mac, with each agent's tool calls, tokens and time. Only counts are shown, never prompts or results, and teams are kept \(retentionText).")
                .font(.caption2)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            if let issue {
                Label(issue, systemImage: "exclamationmark.triangle")
                    .font(.caption)
                    .foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if teams.isEmpty {
                HStack(spacing: 8) {
                    if loading {
                        ProgressView().controlSize(.small)
                        Text("Loading teams…")
                    } else {
                        Image(systemName: "person.3")
                        Text("No agent team has run yet.")
                    }
                }
                .font(.caption)
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(12)
                .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color.primary.opacity(0.04)))
            } else {
                ForEach(teams) { team in
                    TeamHistoryCard(team: team, isExpanded: expanded.contains(team.id)) {
                        withAnimation(.easeInOut(duration: 0.2)) {
                            if expanded.contains(team.id) {
                                expanded.remove(team.id)
                            } else {
                                expanded.insert(team.id)
                            }
                        }
                    }
                }
                if let history, history.total > teams.count {
                    Button(loading ? "Loading…" : "Show more teams (\(history.total - teams.count) older)") {
                        showMore()
                    }
                    .buttonStyle(.link)
                    .font(.caption)
                    .disabled(loading)
                }
            }
        }
    }
}

/// How many of a team's agents succeeded, as a ring that fills on first appearance.
private struct TeamOutcomeRing: View {
    let succeeded: Int
    let total: Int
    let tint: Color
    let progress: CGFloat

    var body: some View {
        let fraction = total > 0 ? CGFloat(succeeded) / CGFloat(total) : 0
        ZStack {
            Circle()
                .stroke(Color.primary.opacity(0.08), lineWidth: 4)
            Circle()
                .trim(from: 0, to: fraction * progress)
                .stroke(tint.gradient, style: StrokeStyle(lineWidth: 4, lineCap: .round))
                .rotationEffect(.degrees(-90))
            Text("\(succeeded)/\(total)")
                .font(.system(size: 9, weight: .semibold))
                .monospacedDigit()
        }
        .frame(width: 36, height: 36)
        .accessibilityHidden(true)
    }
}

private struct TeamHistoryCard: View {
    let team: TeamHistoryTeam
    let isExpanded: Bool
    let toggle: () -> Void

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @MacMCPState private var progress: CGFloat = 0

    private var outcome: (text: String, symbol: String, tint: Color) {
        TeamHistoryFormat.outcome(team.status, team.outcome)
    }

    private var succeeded: Int {
        team.successfulCount ?? team.agents.filter { TeamHistoryFormat.outcome($0.status, $0.outcome).text == "Succeeded" }.count
    }

    private var started: String {
        guard let created = team.createdAt else { return "Unknown time" }
        return Date(timeIntervalSince1970: created).formatted(.relative(presentation: .named))
    }

    private var modelText: String {
        [team.provider.map { (AgentProviders.name($0) ?? $0) }, team.model].compactMap { $0 }.joined(separator: " · ")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button(action: toggle) {
                HStack(spacing: 12) {
                    TeamOutcomeRing(succeeded: succeeded, total: team.agentCount, tint: outcome.tint, progress: progress)
                    VStack(alignment: .leading, spacing: 3) {
                        Text(team.title.isEmpty ? "Untitled team" : team.title)
                            .font(.callout.weight(.semibold))
                            .lineLimit(1)
                        Text([started, "\(team.agentCount) \(team.agentCount == 1 ? "agent" : "agents")", modelText]
                            .filter { !$0.isEmpty }.joined(separator: " · "))
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                    Spacer(minLength: 8)
                    statColumn(TeamHistoryFormat.tokens(team.tokens.total), "tokens")
                    statColumn("\(team.toolCalls)", "tool calls")
                    statColumn(TeamHistoryFormat.duration(team.durationMS), "time")
                    Image(systemName: "chevron.right")
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(.tertiary)
                        .rotationEffect(.degrees(isExpanded ? 90 : 0))
                }
                .padding(12)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(teamSpokenSummary)
            .accessibilityValue(isExpanded ? "Expanded" : "Collapsed")
            .accessibilityHint("Shows or hides this team's agents")
            .accessibilityAddTraits(.isButton)

            if isExpanded {
                Divider().padding(.horizontal, 12)
                VStack(alignment: .leading, spacing: 10) {
                    TeamHistoryLegend()
                    ForEach(team.agents) { agent in
                        TeamHistoryAgentRow(
                            agent: agent,
                            maxTokens: team.agents.compactMap { $0.tokens.total }.max() ?? 0,
                            maxToolCalls: team.agents.map(\.toolCalls).max() ?? 0
                        )
                    }
                    if team.tokensUnknownAgents > 0 {
                        Text("\(team.tokensUnknownAgents) \(team.tokensUnknownAgents == 1 ? "agent's tool" : "agents' tools") did not report token use, so the team total leaves \(team.tokensUnknownAgents == 1 ? "it" : "them") out.")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(12)
                .transition(.opacity)
            }
        }
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(Color.primary.opacity(0.035))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .strokeBorder(outcome.tint.opacity(0.18), lineWidth: 1)
        )
        .onAppear {
            if reduceMotion {
                progress = 1
            } else {
                withAnimation(.easeOut(duration: 0.9)) { progress = 1 }
            }
        }
    }

    private var teamSpokenSummary: String {
        let tokens = team.tokens.total.map { "\($0) tokens" } ?? "tokens unknown"
        return "\(team.title.isEmpty ? "Untitled team" : team.title), \(started), \(outcome.text), \(succeeded) of \(team.agentCount) agents succeeded, \(team.toolCalls) tool calls, \(tokens), \(TeamHistoryFormat.spokenDuration(team.durationMS))"
    }

    private func statColumn(_ value: String, _ caption: String) -> some View {
        VStack(alignment: .trailing, spacing: 1) {
            Text(value)
                .font(.callout.weight(.semibold))
                .monospacedDigit()
                .lineLimit(1)
            Text(caption)
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
        .frame(minWidth: 56, alignment: .trailing)
    }
}

/// What the three parts of an agent's token bar and its thin bar mean.
private struct TeamHistoryLegend: View {
    var body: some View {
        HStack(spacing: 14) {
            swatch(Color.primary.opacity(0.28), "Cached input")
            swatch(Color.primary.opacity(0.55), "New input")
            swatch(Color.primary.opacity(0.9), "Output")
            swatch(Color.teal, "Tool calls")
            Spacer(minLength: 0)
        }
        .font(.caption2)
        .foregroundStyle(.secondary)
        .accessibilityHidden(true)
    }

    private func swatch(_ color: Color, _ title: String) -> some View {
        HStack(spacing: 4) {
            Capsule().fill(color).frame(width: 12, height: 6)
            Text(title)
        }
    }
}

private struct TeamHistoryAgentRow: View {
    let agent: TeamHistoryAgent
    let maxTokens: Int
    let maxToolCalls: Int

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @MacMCPState private var progress: CGFloat = 0

    private var style: ProviderUsageStyle { ProviderUsageStyle.of(agent.provider ?? "") }
    private var outcome: (text: String, symbol: String, tint: Color) {
        TeamHistoryFormat.outcome(agent.status, agent.outcome)
    }

    /// Cached input, new input and output as parts of the total. Providers count cached
    /// input differently (inside input, or beside it), so the parts are taken from the total.
    private var parts: (cached: Int, input: Int, output: Int)? {
        guard let total = agent.tokens.total, total > 0 else { return nil }
        let output = min(total, agent.tokens.output ?? 0)
        let cached = min(total - output, agent.tokens.cacheRead ?? 0)
        return (cached, max(0, total - output - cached), output)
    }

    var body: some View {
        HStack(spacing: 10) {
            ZStack {
                Circle().fill(style.tint.opacity(0.16))
                Image(systemName: style.symbol)
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(style.tint)
            }
            .frame(width: 26, height: 26)

            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 4) {
                    Text(agent.label)
                        .font(.caption.weight(.semibold))
                        .lineLimit(1)
                    Image(systemName: outcome.symbol)
                        .font(.system(size: 10))
                        .foregroundStyle(outcome.tint)
                        .help(outcome.text)
                }
                Text(agent.model ?? (AgentProviders.name(agent.provider ?? "") ?? (agent.provider ?? "Agent")))
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            .frame(width: 150, alignment: .leading)

            VStack(alignment: .leading, spacing: 5) {
                tokenBar
                toolBar
            }
            .frame(maxWidth: .infinity)

            VStack(alignment: .trailing, spacing: 1) {
                Text(TeamHistoryFormat.tokens(agent.tokens.total))
                    .font(.caption.weight(.semibold))
                    .monospacedDigit()
                Text("\(agent.toolCalls) calls · \(TeamHistoryFormat.duration(agent.durationMS))")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
            }
            .frame(width: 112, alignment: .trailing)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(spokenSummary)
        .onAppear {
            if reduceMotion {
                progress = 1
            } else {
                withAnimation(.easeOut(duration: 0.8).delay(0.05)) { progress = 1 }
            }
        }
    }

    private var tokenBar: some View {
        GeometryReader { proxy in
            let width = proxy.size.width
            ZStack(alignment: .leading) {
                Capsule().fill(Color.primary.opacity(0.06))
                if let parts, maxTokens > 0 {
                    let scale = width * progress / CGFloat(maxTokens)
                    HStack(spacing: 0) {
                        Rectangle().fill(style.tint.opacity(0.3)).frame(width: CGFloat(parts.cached) * scale)
                        Rectangle().fill(style.tint.opacity(0.6)).frame(width: CGFloat(parts.input) * scale)
                        Rectangle().fill(style.tint).frame(width: max(CGFloat(parts.output) * scale, parts.output > 0 ? 2 * progress : 0))
                    }
                    .clipShape(Capsule())
                } else if agent.tokens.total == nil {
                    Text("Tokens not reported")
                        .font(.system(size: 8))
                        .foregroundStyle(.tertiary)
                        .padding(.leading, 6)
                }
            }
        }
        .frame(height: 9)
        .help(tokenHelp)
    }

    private var toolBar: some View {
        GeometryReader { proxy in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.primary.opacity(0.06))
                Capsule()
                    .fill(Color.teal.gradient)
                    .frame(width: maxToolCalls > 0 ? proxy.size.width * progress * CGFloat(agent.toolCalls) / CGFloat(maxToolCalls) : 0)
            }
        }
        .frame(height: 4)
        .help("\(agent.toolCalls) tool calls")
    }

    private var tokenHelp: String {
        guard let parts else { return agent.tokens.total == nil ? "This agent's tool did not report token use" : "No tokens" }
        return "Cached input \(ProviderUsageFormat.compact(parts.cached)) · New input \(ProviderUsageFormat.compact(parts.input)) · Output \(ProviderUsageFormat.compact(parts.output))"
    }

    private var spokenSummary: String {
        let tokens = agent.tokens.total.map { "\($0) tokens" } ?? "tokens not reported"
        let model = agent.model.map { ", \($0)" } ?? ""
        return "\(agent.label)\(model), \(outcome.text), \(agent.toolCalls) tool calls, \(tokens), \(TeamHistoryFormat.spokenDuration(agent.durationMS))"
    }
}
