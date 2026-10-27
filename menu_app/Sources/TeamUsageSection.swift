import SwiftUI

// MARK: Usage › Teams: what each saved team (Settings › Agents › Teams) used, in the Delegated Agents tab's
// language: one sentence, a row per team that opens to its facts, its days by member, its members and runs.
// The data and its formatting are in TeamUsage.swift.

struct TeamUsageSection: View {
    @ObservedObject var state: AppState
    @MacMCPState private var period: ProviderUsagePeriod = .year
    @MacMCPState private var summary: TeamUsageSummary?
    @MacMCPState private var loading = false
    @MacMCPState private var issue: String?
    @MacMCPState private var expanded: Set<String> = []
    @MacMCPState private var deletedShown = false
    /// A summary given up front (the native render test) is shown as it is and never loaded.
    private let preloaded: Bool

    init(state: AppState, summary: TeamUsageSummary? = nil, expanded: Set<String> = []) {
        self.state = state
        preloaded = summary != nil
        _summary = MacMCPState(initialValue: summary)
        _expanded = MacMCPState(initialValue: expanded)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            if loading && summary == nil {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("Loading team usage…").font(.callout).foregroundStyle(.secondary)
                }
                .padding(.top, 18)
            } else if let issue {
                Text(issue).font(.callout).foregroundStyle(.secondary).padding(.top, 18)
            } else if let summary, !summary.teams.isEmpty {
                if !summary.saved.isEmpty {
                    Text("Teams").font(.system(size: 13, weight: .semibold)).padding(.top, 28).padding(.bottom, 8)
                    teamList(summary.saved, total: summary.totalTokens)
                }
                if !summary.deleted.isEmpty {
                    DisclosureGroup(isExpanded: $deletedShown) {
                        VStack(alignment: .leading, spacing: 8) {
                            teamList(summary.deleted, total: summary.totalTokens)
                            Text("A deleted team keeps its history here; its tokens also stay with their providers in Delegated Agents.")
                                .font(.system(size: 12)).foregroundStyle(.tertiary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        .padding(.top, 8)
                    } label: {
                        Text("Deleted teams (\(summary.deleted.count))").font(.system(size: 12.5)).foregroundStyle(.secondary)
                    }
                    .padding(.top, summary.saved.isEmpty ? 28 : 16)
                }
            } else {
                emptyState.padding(.top, 18)
            }
            footer.padding(.top, 10)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .task(id: period) { await load() }
    }

    // MARK: Summary

    private var header: some View {
        HStack(alignment: .top, spacing: 16) {
            VStack(alignment: .leading, spacing: 4) {
                HStack(alignment: .firstTextBaseline, spacing: 5) {
                    Text(UsageFormat.compact(summary?.totalTokens ?? 0))
                        .font(.system(size: 34, weight: .semibold)).monospacedDigit()
                        .help(ProviderUsageFormat.exact(summary?.totalTokens ?? 0))
                    Text("tokens").font(.system(size: 15, weight: .medium)).foregroundStyle(.secondary)
                }
                if let summary, !summary.teams.isEmpty {
                    let tasks = summary.teams.flatMap(\.runList).reduce(0) { $0 + $1.members }
                    Text("\(UsageFormat.count(summary.teams.count, "team")), \(UsageFormat.count(summary.runs, "run")), \(UsageFormat.count(tasks, "member task")).")
                        .font(.system(size: 13)).foregroundStyle(.secondary)
                }
                HStack(spacing: 5) {
                    Text("The same tokens as Delegated Agents, grouped by the saved team that used them.")
                    Image(systemName: "info.circle")
                        .help("Each member's tokens count under its provider in Delegated Agents too; this tab only groups them by team, member and run. Teams started without a saved team are not listed.")
                        .accessibilityLabel("About team usage")
                }
                .font(.system(size: 12)).foregroundStyle(.tertiary)
                .fixedSize(horizontal: false, vertical: true)
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

    private var emptyState: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("No saved team ran in this period.").font(.callout).foregroundStyle(.secondary)
            Text("Build a team in Settings › Agents › Teams and start it by name; what each member used shows here.")
                .font(.system(size: 12)).foregroundStyle(.tertiary)
                .fixedSize(horizontal: false, vertical: true)
            Button("Open Teams") { MenuAppDelegate.openSettings?("teams") }
                .controlSize(.small)
                .padding(.top, 2)
        }
    }

    private func teamList(_ teams: [TeamUsageTeam], total: Int) -> some View {
        VStack(spacing: 0) {
            ForEach(Array(teams.enumerated()), id: \.element.id) { index, team in
                if index > 0 { Divider() }
                let open = expanded.contains(team.id)
                TeamUsageRow(team: team, total: total, period: period, open: open) {
                    withAnimation(.easeOut(duration: 0.2)) {
                        if open { expanded.remove(team.id) } else { expanded.insert(team.id) }
                    }
                }
            }
        }
        .usageListBox()
    }

    @ViewBuilder
    private var footer: some View {
        if let summary {
            HStack(alignment: .firstTextBaseline, spacing: 16) {
                VStack(alignment: .leading, spacing: 2) {
                    if summary.otherRuns > 0 {
                        Text("Teams started without a saved team: \(UsageFormat.compact(summary.otherTokens)) tokens in \(UsageFormat.count(summary.otherRuns, "run")).")
                    }
                    if summary.unreportedTurns > 0 {
                        Text("\(UsageFormat.count(summary.unreportedTurns, "turn")) reported no token count and \(summary.unreportedTurns == 1 ? "is" : "are") left out.")
                    }
                }
                .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 8)
                if let since = ProviderUsageFormat.since(summary.availableSince) {
                    Text("Since \(since)")
                }
            }
            .font(.system(size: 12))
            .foregroundStyle(.tertiary)
        }
    }

    private func load() async {
        guard !preloaded else { return }
        loading = true
        let result = await state.taskAPI("dashboard/api/team-usage?days=\(period.days)")
        loading = false
        if result.ok {
            summary = TeamUsageSummary(result.object)
            issue = nil
        } else {
            issue = result.object["message"] as? String ?? result.object["error"] as? String ?? "Mac MCP is not answering."
        }
    }
}

/// One saved team: its members' tiles, name and last run, its days as a line, its tokens and share, and a chevron.
private struct TeamUsageRow: View {
    let team: TeamUsageTeam
    let total: Int
    let period: ProviderUsagePeriod
    let open: Bool
    let toggle: () -> Void

    var body: some View {
        let colors = TeamUsageColors.members(team.members)
        VStack(alignment: .leading, spacing: 0) {
            Button(action: toggle) {
                HStack(spacing: 12) {
                    TeamUsageAvatars(members: team.members, colors: colors)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(team.name).font(.system(size: 13, weight: .medium)).lineLimit(1).truncationMode(.middle)
                        Text(meta).font(.system(size: 11.5)).foregroundStyle(.secondary).lineLimit(1)
                    }
                    .frame(minWidth: 130, maxWidth: 230, alignment: .leading)
                    TeamUsageSparkline(values: period.buckets(team.daily).map(\.total), color: .accentColor)
                        .frame(maxWidth: .infinity)
                    VStack(alignment: .trailing, spacing: 1) {
                        Text(UsageFormat.compact(team.tokens)).font(.system(size: 13, weight: .medium)).monospacedDigit()
                        Text(UsageFormat.share(team.tokens, of: total)).font(.system(size: 11)).foregroundStyle(.secondary).monospacedDigit()
                    }
                    .frame(width: 76, alignment: .trailing)
                    .help(ProviderUsageFormat.exact(team.tokens))
                    Image(systemName: "chevron.right")
                        .font(.system(size: 9, weight: .bold)).foregroundStyle(.tertiary)
                        .rotationEffect(.degrees(open ? 90 : 0))
                        .frame(width: 10)
                }
                .padding(.horizontal, 14).padding(.vertical, 10)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("\(team.name), \(UsageFormat.compact(team.tokens)) tokens, \(UsageFormat.share(team.tokens, of: total)), \(meta)")
            .accessibilityHint(open ? "Hides the details" : "Shows the details")
            .accessibilityAddTraits(.isButton)
            // children: .ignore drops the button's own press, so VoiceOver and AXPress get it back here.
            .accessibilityAction { toggle() }
            if open {
                TeamUsageDetail(team: team, period: period, colors: colors)
                    .padding(.horizontal, 14).padding(.bottom, 14)
                    .transition(.opacity)
            }
        }
    }

    private var meta: String {
        var parts = [UsageFormat.count(team.members.count, "member"), UsageFormat.count(team.runs, "run")]
        if let last = team.lastRunAt { parts.append("last \(TeamUsageFormat.when(last))") }
        return parts.joined(separator: " · ")
    }
}

/// Members' initials in their colours, overlapping like a stack of tiles.
private struct TeamUsageAvatars: View {
    let members: [TeamUsageMember]
    let colors: [String: Color]

    private static let shown = 5

    var body: some View {
        HStack(spacing: -5) {
            ForEach(members.prefix(Self.shown)) { member in
                tile(Text(TeamUsageFormat.initials(member.name)), fill: colors[member.key] ?? UsageColors.agent(""))
            }
            if members.count > Self.shown {
                tile(Text("+\(members.count - Self.shown)"), fill: Color.primary.opacity(0.15))
            }
        }
        .accessibilityHidden(true)
    }

    private func tile(_ text: Text, fill: Color) -> some View {
        text.font(.system(size: 9.5, weight: .heavy))
            .foregroundStyle(Color.black.opacity(0.78))
            .frame(width: 26, height: 26)
            .background(RoundedRectangle(cornerRadius: 7, style: .continuous).fill(fill))
            .background(RoundedRectangle(cornerRadius: 7, style: .continuous).fill(Color(nsColor: .windowBackgroundColor)))
            .overlay(RoundedRectangle(cornerRadius: 7, style: .continuous)
                .strokeBorder(Color(nsColor: .windowBackgroundColor), lineWidth: 2))
    }
}

/// A team's days (a year's months) as a thin line, to see at a glance when it ran.
private struct TeamUsageSparkline: View {
    let values: [Int]
    let color: Color

    var body: some View {
        GeometryReader { proxy in
            let top = CGFloat(max(1, values.max() ?? 1))
            let step = values.count > 1 ? proxy.size.width / CGFloat(values.count - 1) : 0
            Path { path in
                for (index, value) in values.enumerated() {
                    let point = CGPoint(x: CGFloat(index) * step,
                                        y: proxy.size.height - 2 - (proxy.size.height - 4) * CGFloat(value) / top)
                    if index == 0 { path.move(to: point) } else { path.addLine(to: point) }
                }
            }
            .stroke(color, style: StrokeStyle(lineWidth: 1.5, lineCap: .round, lineJoin: .round))
        }
        .frame(minWidth: 60, maxHeight: 26)
        .frame(height: 26)
        .accessibilityHidden(true)
    }
}

/// An open team: four facts, its days split by member, its members and its runs.
private struct TeamUsageDetail: View {
    let team: TeamUsageTeam
    let period: ProviderUsagePeriod
    let colors: [String: Color]

    @MacMCPState private var openRuns: Set<String> = []

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            UsageFactsRow(facts: [
                UsageFact(value: UsageFormat.compact(team.tokens), title: "Tokens", help: ProviderUsageFormat.exact(team.tokens)),
                UsageFact(value: UsageFormat.exact(team.runs), title: team.runs == 1 ? "Run" : "Runs"),
                UsageFact(value: TeamUsageFormat.duration(team.averageDurationSeconds), title: "Average run"),
                UsageFact(value: team.gates > 0 ? "\(team.gatesPassed)/\(team.gates)" : "—", title: "Review gates passed",
                          help: team.gates > 0 ? "Reviewer members that passed the work they checked" : "No reviewer gate in these runs"),
            ])
            chart.padding(.top, 14)
            Text("Members").font(.system(size: 13, weight: .semibold)).padding(.top, 18).padding(.bottom, 6)
            members
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text("Runs").font(.system(size: 13, weight: .semibold))
                Text("Open a run to see each member.").font(.system(size: 11.5)).foregroundStyle(.tertiary)
            }
            .padding(.top, 18).padding(.bottom, 6)
            runs
        }
    }

    private var chart: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline, spacing: 12) {
                Text(period == .year ? "Tokens per month, by member" : "Tokens per day, by member")
                    .font(.system(size: 12.5, weight: .medium))
                Spacer(minLength: 8)
                ForEach(team.members) { member in
                    HStack(spacing: 5) {
                        RoundedRectangle(cornerRadius: 2).fill(colors[member.key] ?? .gray).frame(width: 8, height: 8)
                        Text(member.name).foregroundStyle(.secondary)
                    }
                    .font(.system(size: 12))
                    .accessibilityElement(children: .combine)
                }
            }
            UsageBarChart(buckets: period.buckets(team.daily),
                          series: team.members.map { UsageSeries(id: $0.key, name: $0.name, color: colors[$0.key] ?? .gray) },
                          spacing: period == .quarter ? 2 : period == .year ? 10 : 4, height: 120,
                          label: "\(team.name): tokens per \(period == .year ? "month" : "day") by member")
        }
        .padding(14)
        .usageListBox()
    }

    private var members: some View {
        VStack(spacing: 0) {
            columns(["Member", "Provider · model · thinking", "Role", "Share", "Tokens", "Runs"])
            ForEach(team.members) { member in
                Divider()
                HStack(spacing: 10) {
                    Text(member.name).font(.system(size: 12.5, weight: .semibold)).lineLimit(1)
                        .frame(width: Width.name, alignment: .leading)
                    Text(selection(member.provider, member.model, member.reasoning))
                        .font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(1).truncationMode(.tail)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    roleBadge(member.role).frame(width: Width.role, alignment: .leading)
                    UsageShareMeter(share: member.share, color: colors[member.key] ?? .gray).frame(width: Width.share)
                    HStack(spacing: 4) {
                        Text(UsageFormat.compact(member.tokens)).font(.system(size: 12.5, weight: .medium))
                        Text(UsageFormat.share(member.tokens, of: team.tokens)).font(.system(size: 11)).foregroundStyle(.secondary)
                    }
                    .monospacedDigit()
                    .frame(width: Width.tokens, alignment: .trailing)
                    .help(ProviderUsageFormat.exact(member.tokens))
                    Text(UsageFormat.exact(member.runs)).font(.system(size: 12)).monospacedDigit()
                        .frame(width: Width.runs, alignment: .trailing)
                }
                .padding(.horizontal, 12).padding(.vertical, 8)
                .accessibilityElement(children: .ignore)
                .accessibilityLabel("\(member.name), \(selection(member.provider, member.model, member.reasoning)), \(member.role ?? "member"), \(UsageFormat.compact(member.tokens)) tokens, \(UsageFormat.share(member.tokens, of: team.tokens)), \(UsageFormat.count(member.runs, "run"))")
            }
        }
        .usageListBox()
    }

    private var runs: some View {
        VStack(spacing: 0) {
            columns(["Date", "Title", "Outcome", "Time", "Tokens"], runs: true)
            ForEach(team.runList) { run in
                Divider()
                runRow(run)
                if openRuns.contains(run.id) {
                    ForEach(run.rows) { member in
                        HStack(spacing: 10) {
                            Color.clear.frame(width: Width.date, height: 1)
                            HStack(spacing: 5) {
                                Text("↳ \(member.name)").font(.system(size: 12))
                                Text(selection(member.provider, member.model, member.reasoning))
                                    .font(.system(size: 11.5)).foregroundStyle(.tertiary).lineLimit(1)
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            Text(TeamUsageFormat.status(member)).font(.system(size: 11.5)).foregroundStyle(.secondary)
                                .frame(width: Width.outcome, alignment: .leading)
                            Text(TeamUsageFormat.duration(member.durationSeconds)).font(.system(size: 11.5)).foregroundStyle(.secondary)
                                .frame(width: Width.time, alignment: .trailing)
                            Text(member.tokens > 0 ? UsageFormat.compact(member.tokens) : "—").font(.system(size: 11.5))
                                .foregroundStyle(.secondary)
                                .frame(width: Width.tokens, alignment: .trailing)
                            Color.clear.frame(width: 10, height: 1)
                        }
                        .monospacedDigit()
                        .padding(.horizontal, 12).padding(.vertical, 5)
                        .background(Color.primary.opacity(0.025))
                        .accessibilityElement(children: .combine)
                    }
                }
            }
            if team.runs > team.runList.count {
                Divider()
                Text("The \(team.runList.count) most recent of \(UsageFormat.count(team.runs, "run")).")
                    .font(.system(size: 11.5)).foregroundStyle(.tertiary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 12).padding(.vertical, 7)
            }
        }
        .usageListBox()
    }

    private func runRow(_ run: TeamUsageRun) -> some View {
        let open = openRuns.contains(run.id)
        let outcome = TeamUsageFormat.outcome(run)
        let title = TeamUsageFormat.runTitle(run.title, team: team.name)
        return Button {
            withAnimation(.easeOut(duration: 0.15)) {
                if open { openRuns.remove(run.id) } else { openRuns.insert(run.id) }
            }
        } label: {
            HStack(spacing: 10) {
                Text(run.startedAt.formatted(.dateTime.month(.abbreviated).day().hour().minute()))
                    .font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(1)
                    .frame(width: Width.date, alignment: .leading)
                Text(title).font(.system(size: 12.5)).lineLimit(1).truncationMode(.tail)
                    .frame(maxWidth: .infinity, alignment: .leading)
                Text(outcome.text).font(.system(size: 12)).lineLimit(1)
                    .foregroundStyle(outcome.good == true ? AnyShapeStyle(Color.green)
                                     : outcome.good == false ? AnyShapeStyle(UsageColors.warning) : AnyShapeStyle(.secondary))
                    .frame(width: Width.outcome, alignment: .leading)
                Text(TeamUsageFormat.duration(run.durationSeconds)).font(.system(size: 12))
                    .frame(width: Width.time, alignment: .trailing)
                Text(UsageFormat.compact(run.tokens)).font(.system(size: 12.5, weight: .medium))
                    .frame(width: Width.tokens, alignment: .trailing)
                    .help(ProviderUsageFormat.exact(run.tokens))
                Image(systemName: "chevron.right")
                    .font(.system(size: 8, weight: .bold)).foregroundStyle(.tertiary)
                    .rotationEffect(.degrees(open ? 90 : 0))
                    .frame(width: 10)
            }
            .monospacedDigit()
            .padding(.horizontal, 12).padding(.vertical, 8)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(title), \(run.startedAt.formatted(date: .abbreviated, time: .shortened)), \(outcome.text), \(TeamUsageFormat.duration(run.durationSeconds)), \(UsageFormat.compact(run.tokens)) tokens")
        .accessibilityHint(open ? "Hides each member" : "Shows each member")
        .accessibilityAddTraits(.isButton)
        .accessibilityAction { if open { openRuns.remove(run.id) } else { openRuns.insert(run.id) } }
    }

    /// Column titles over the members (six) or the runs (five and the chevron).
    private func columns(_ titles: [String], runs: Bool = false) -> some View {
        HStack(spacing: 10) {
            if runs {
                Text(titles[0]).frame(width: Width.date, alignment: .leading)
                Text(titles[1]).frame(maxWidth: .infinity, alignment: .leading)
                Text(titles[2]).frame(width: Width.outcome, alignment: .leading)
                Text(titles[3]).frame(width: Width.time, alignment: .trailing)
                Text(titles[4]).frame(width: Width.tokens, alignment: .trailing)
                Color.clear.frame(width: 10, height: 1)
            } else {
                Text(titles[0]).frame(width: Width.name, alignment: .leading)
                Text(titles[1]).frame(maxWidth: .infinity, alignment: .leading)
                Text(titles[2]).frame(width: Width.role, alignment: .leading)
                Text(titles[3]).frame(width: Width.share, alignment: .leading)
                Text(titles[4]).frame(width: Width.tokens, alignment: .trailing)
                Text(titles[5]).frame(width: Width.runs, alignment: .trailing)
            }
        }
        .font(.system(size: 11, weight: .semibold)).foregroundStyle(.tertiary)
        .padding(.horizontal, 12).padding(.vertical, 6)
        .accessibilityHidden(true)
    }

    @ViewBuilder
    private func roleBadge(_ role: String?) -> some View {
        if let role { SettingsBadge(text: role) } else { Text("—").font(.system(size: 12)).foregroundStyle(.tertiary) }
    }

    /// "Codex · gpt-6.1-sol · High"; an empty model or thinking was the provider's own default.
    private func selection(_ provider: String, _ model: String?, _ reasoning: String?) -> String {
        [AgentChoice.providerName(provider), model ?? "default model", reasoning.map(AgentChoice.reasoningDisplayName)]
            .compactMap { $0 }.joined(separator: " · ")
    }

    private enum Width {
        static let name: CGFloat = 84
        static let role: CGFloat = 70
        static let share: CGFloat = 80
        static let tokens: CGFloat = 80
        static let runs: CGFloat = 34
        static let date: CGFloat = 104
        static let outcome: CGFloat = 190
        static let time: CGFloat = 52
    }
}
