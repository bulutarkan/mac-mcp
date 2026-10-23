import SwiftUI

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
        if seconds < 60 { return count(seconds, "second") }
        if seconds < 3600 { return "\(count(seconds / 60, "minute")) \(count(seconds % 60, "second"))" }
        return "\(count(seconds / 3600, "hour")) \(count((seconds % 3600) / 60, "minute"))"
    }

    /// "1 tool call", "2 tool calls".
    static func count(_ value: Int, _ noun: String) -> String {
        "\(value) \(noun)\(value == 1 ? "" : "s")"
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
    let loadMessages: (String) async -> TeamMessagesEnvelope?
    let showMore: () -> Void

    @MacMCPState private var expanded: Set<String> = []

    private var teams: [TeamHistoryTeam] { history?.teams ?? [] }

    private var retentionText: String {
        guard let days = history?.retentionDays else { return "for as long as Agent History keeps agents" }
        return days == 0 ? "until you delete them" : "for \(days) days, like Agent History"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Agent teams that ran on this Mac, with each agent's tool calls, tokens and time, and what the agents wrote to each other. Prompts and results are never shown, and teams are kept \(retentionText).")
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
                    TeamHistoryCard(team: team, isExpanded: expanded.contains(team.id), loadMessages: loadMessages) {
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
    let loadMessages: (String) async -> TeamMessagesEnvelope?
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
                    statColumn("\(team.toolCalls)", team.toolCalls == 1 ? "tool call" : "tool calls")
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
            // On the button itself, so it keeps its press action for VoiceOver.
            .accessibilityLabel(teamSpokenSummary)
            .accessibilityValue(isExpanded ? "Expanded" : "Collapsed")
            .accessibilityHint("Shows or hides this team's agents")

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
                    TeamMessagesPanel(teamID: team.teamID, load: loadMessages)
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
        let agents = team.agentCount == 1 ? "\(succeeded) of 1 agent succeeded" : "\(succeeded) of \(team.agentCount) agents succeeded"
        return "\(team.title.isEmpty ? "Untitled team" : team.title), \(started), \(outcome.text), \(agents), \(TeamHistoryFormat.count(team.toolCalls, "tool call")), \(tokens), \(TeamHistoryFormat.spokenDuration(team.durationMS))"
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

/// What a team's agents wrote to each other (#104), who saw it, and whose results they read (#106).
private struct TeamMessagesPanel: View {
    let teamID: String
    let load: (String) async -> TeamMessagesEnvelope?

    @MacMCPState private var envelope: TeamMessagesEnvelope?
    @MacMCPState private var loaded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Divider()
            HStack(spacing: 6) {
                Image(systemName: "bubble.left.and.bubble.right")
                    .foregroundStyle(.secondary)
                    .accessibilityHidden(true)
                Text("Messages").font(.caption.weight(.semibold))
                if let counts = envelope?.messageCounts, counts.sent > 0 {
                    Text(Self.countsText(counts))
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                Spacer(minLength: 0)
            }
            if !loaded {
                ProgressView().controlSize(.small)
            } else if let envelope {
                if envelope.messages.isEmpty {
                    Text("The agents did not write to each other.")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(envelope.messages) { message in
                        TeamMessageRow(message: message)
                    }
                    if envelope.hasMore {
                        Text("Older messages are not shown here.")
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                    }
                }
                if let reads = envelope.dependencyReads, !reads.isEmpty {
                    ForEach(Array(reads.enumerated()), id: \.offset) { _, read in
                        Label("\(read.readerTask) read the full result of \(read.targetTask)\(read.count > 1 ? " (\(read.count)×)" : "")",
                              systemImage: "doc.text.magnifyingglass")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                }
            } else {
                Text("Could not load this team's messages.")
                    .font(.caption2)
                    .foregroundStyle(.orange)
            }
        }
        .task(id: teamID) {
            envelope = await load(teamID)
            loaded = true
        }
    }

    static func countsText(_ counts: TeamMessageCounts) -> String {
        var parts = [TeamHistoryFormat.count(counts.sent, "message")]
        if counts.toFinishedRecipients > 0 {
            parts.append("\(counts.toFinishedRecipients) reached a finished agent")
        }
        if counts.forwarded > 0 {
            parts.append("\(counts.forwarded) passed on")
        }
        return parts.joined(separator: " · ")
    }
}

/// One message: who to whom, when, the text, and whether it was seen, opened or passed on.
private struct TeamMessageRow: View {
    let message: TeamMessageItem

    private var time: String {
        let parser = ISO8601DateFormatter()
        parser.formatOptions = [.withInternetDateTime]
        let fixed = message.sentAt.replacingOccurrences(of: #"([+-]\d{2})(\d{2})$"#, with: "$1:$2", options: .regularExpression)
        guard let date = parser.date(from: fixed) else { return message.sentAt }
        return date.formatted(date: .omitted, time: .shortened)
    }

    private var delivery: (text: String, symbol: String, tint: Color) {
        if message.deliveryNote == "recipient_finished" {
            return ("Recipient had finished", "arrow.uturn.right.circle", .orange)
        }
        let opened = message.deliveries.filter { $0.readAt != nil }.map(\.label)
        if !opened.isEmpty { return ("Opened by \(opened.joined(separator: ", "))", "envelope.open", .green) }
        let shown = message.deliveries.map(\.label)
        if !shown.isEmpty { return ("Seen by \(shown.joined(separator: ", "))", "eye", .secondary) }
        return ("Not seen yet", "eye.slash", .secondary)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 6) {
                Text("\(message.fromLead == true ? "Lead" : message.from) → \(message.toLabel ?? "everyone")")
                    .font(.caption2.weight(.semibold))
                    .lineLimit(1)
                if message.kind != "update" {
                    Text(message.kind.capitalized)
                        .font(.system(size: 9, weight: .semibold))
                        .padding(.horizontal, 5).padding(.vertical, 1)
                        .background(Capsule().fill(Color.primary.opacity(0.08)))
                }
                if message.forwardedFrom != nil {
                    Text("Passed on")
                        .font(.system(size: 9, weight: .semibold))
                        .foregroundStyle(.orange)
                }
                Spacer(minLength: 4)
                Text(time).font(.caption2).foregroundStyle(.tertiary).monospacedDigit()
            }
            Text(message.body)
                .font(.caption)
                .lineLimit(4)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            Label(delivery.text, systemImage: delivery.symbol)
                .font(.caption2)
                .foregroundStyle(delivery.tint)
                .lineLimit(1)
        }
        .padding(8)
        .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(Color.primary.opacity(0.03)))
        .accessibilityElement(children: .combine)
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
                Text("\(TeamHistoryFormat.count(agent.toolCalls, "call")) · \(TeamHistoryFormat.duration(agent.durationMS))")
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
        .help(TeamHistoryFormat.count(agent.toolCalls, "tool call"))
    }

    private var tokenHelp: String {
        guard let parts else { return agent.tokens.total == nil ? "This agent's tool did not report token use" : "No tokens" }
        return "Cached input \(ProviderUsageFormat.compact(parts.cached)) · New input \(ProviderUsageFormat.compact(parts.input)) · Output \(ProviderUsageFormat.compact(parts.output))"
    }

    private var spokenSummary: String {
        let tokens = agent.tokens.total.map { "\($0) tokens" } ?? "tokens not reported"
        let model = agent.model.map { ", \($0)" } ?? ""
        return "\(agent.label)\(model), \(outcome.text), \(TeamHistoryFormat.count(agent.toolCalls, "tool call")), \(tokens), \(TeamHistoryFormat.spokenDuration(agent.durationMS))"
    }
}
