import SwiftUI

// MARK: Usage › Chat: the owner's own conversations in Mac MCP Chat, counted apart from the agents
// Mac MCP starts (Delegated Agents). Read from /dashboard/api/chats/usage.
// Laid out like Screen Time: one sentence, the days as bars split by agent, the agents in the same colours.

struct ChatUsageAgent: Identifiable {
    let id: String
    let label: String
    let answers: Int
    let chats: Int
    let inputTokens: Int
    let outputTokens: Int
    var tokens: Int { inputTokens + outputTokens }
}

struct ChatUsageModel: Identifiable {
    let agent: String
    let model: String
    let answers: Int
    let tokens: Int
    var id: String { agent + "/" + model }
}

struct ChatUsageDay: Identifiable {
    let date: String
    let tokens: Int
    let answers: Int
    let agents: [String: Int]
    var id: String { date }
}

struct ChatUsageSummary {
    let answers: Int
    let chats: Int
    let inputTokens: Int
    let outputTokens: Int
    let cachedTokens: Int
    let seconds: Double
    let agents: [ChatUsageAgent]
    let models: [ChatUsageModel]
    let daily: [ChatUsageDay]

    init(_ object: [String: Any]) {
        let totals = object["totals"] as? [String: Any] ?? [:]
        func int(_ value: Any?) -> Int { (value as? NSNumber)?.intValue ?? 0 }
        answers = int(totals["turns"])
        chats = int(totals["chats"])
        inputTokens = int(totals["input_tokens"])
        outputTokens = int(totals["output_tokens"])
        cachedTokens = int(totals["cached_tokens"])
        seconds = (totals["seconds"] as? NSNumber)?.doubleValue ?? 0
        agents = (object["agents"] as? [[String: Any]] ?? []).map {
            ChatUsageAgent(id: $0["agent"] as? String ?? "", label: $0["label"] as? String ?? "",
                           answers: int($0["turns"]), chats: int($0["chats"]),
                           inputTokens: int($0["input_tokens"]), outputTokens: int($0["output_tokens"]))
        }
        models = (object["models"] as? [[String: Any]] ?? []).map {
            ChatUsageModel(agent: $0["agent"] as? String ?? "", model: $0["model"] as? String ?? "",
                           answers: int($0["turns"]), tokens: int($0["tokens"]))
        }
        daily = (object["daily"] as? [[String: Any]] ?? []).map {
            let tokens = int($0["tokens"])
            var split = ($0["agents"] as? [String: Any] ?? [:]).mapValues { int($0) }
            if split.isEmpty && tokens > 0 { split = ["": tokens] }  // a server from before the split: one bar
            return ChatUsageDay(date: $0["date"] as? String ?? "", tokens: tokens, answers: int($0["turns"]), agents: split)
        }
    }

    var tokens: Int { inputTokens + outputTokens }
}

struct ChatUsageSection: View {
    @ObservedObject var state: AppState
    @MacMCPState private var period: ProviderUsagePeriod = .month
    @MacMCPState private var summary: ChatUsageSummary?
    @MacMCPState private var loading = false
    @MacMCPState private var issue: String?
    @MacMCPState private var expanded: Set<String> = []
    @MacMCPState private var detailsShown = false

    init(state: AppState) {
        self.state = state
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            if loading && summary == nil {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("Loading chat usage…").font(.callout).foregroundStyle(.secondary)
                }
                .padding(.top, 18)
            } else if let issue {
                Text(issue).font(.callout).foregroundStyle(.secondary).padding(.top, 18)
            } else if let summary, summary.answers > 0 {
                UsageBarChart(buckets: Self.buckets(summary, period: period), series: Self.series(summary),
                              spacing: period == .quarter ? 2 : period == .year ? 10 : 4,
                              label: "Tokens per \(period == .year ? "month" : "day")")
                    .padding(.top, 22)
                Text("Agents").font(.system(size: 13, weight: .semibold)).padding(.top, 28).padding(.bottom, 8)
                agentList(summary)
                details(summary).padding(.top, 14)
            } else {
                Text("No answers in Mac MCP Chat in this period. Open it from the menu: Chat… (⇧⌘N).")
                    .font(.callout).foregroundStyle(.secondary)
                    .padding(.top, 18)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .task(id: period) { await load() }
    }

    private var header: some View {
        HStack(alignment: .top, spacing: 16) {
            VStack(alignment: .leading, spacing: 4) {
                HStack(alignment: .firstTextBaseline, spacing: 5) {
                    Text(UsageFormat.compact(summary?.tokens ?? 0))
                        .font(.system(size: 34, weight: .semibold)).monospacedDigit()
                    Text("tokens").font(.system(size: 15, weight: .medium)).foregroundStyle(.secondary)
                }
                if let summary, summary.answers > 0 {
                    Text("\(UsageFormat.count(summary.answers, "answer")) in \(UsageFormat.count(summary.chats, "chat")), with \(ChatTime.duration(summary.seconds)) of agent work.")
                        .font(.system(size: 13)).foregroundStyle(.secondary)
                }
            }
            .accessibilityElement(children: .combine)
            Spacer(minLength: 12)
            Picker("Period", selection: $period) {
                ForEach(ProviderUsagePeriod.allCases) { Text($0.name).tag($0) }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .fixedSize()
        }
    }

    private func agentList(_ summary: ChatUsageSummary) -> some View {
        VStack(spacing: 0) {
            ForEach(Array(summary.agents.enumerated()), id: \.element.id) { index, agent in
                if index > 0 { Divider() }
                let models = summary.models.filter { $0.agent == agent.id && $0.tokens > 0 }
                let open = expanded.contains(agent.id)
                UsageRow(title: agent.label,
                         subtitle: Text("\(UsageFormat.count(agent.answers, "answer")) in \(UsageFormat.count(agent.chats, "chat"))"),
                         share: summary.tokens > 0 ? Double(agent.tokens) / Double(summary.tokens) : 0,
                         color: UsageColors.agent(agent.id), amount: UsageFormat.compact(agent.tokens),
                         percent: UsageFormat.share(agent.tokens, of: summary.tokens),
                         help: "\(UsageFormat.exact(agent.tokens)) tokens",
                         open: open,
                         toggle: models.isEmpty ? nil : {
                             withAnimation(.easeOut(duration: 0.2)) {
                                 if open { expanded.remove(agent.id) } else { expanded.insert(agent.id) }
                             }
                         }) {
                    UsageAgentLogo(agent: agent.id)
                } detail: {
                    VStack(alignment: .leading, spacing: 7) {
                        ForEach(models) { model in
                            let parts = Self.modelParts(model.model)
                            HStack(alignment: .firstTextBaseline, spacing: 6) {
                                Text(parts.name).font(.system(size: 12.5))
                                Text([parts.via, UsageFormat.count(model.answers, "answer")].compactMap { $0 }.joined(separator: ", "))
                                    .font(.system(size: 11.5)).foregroundStyle(.secondary)
                                Spacer(minLength: 8)
                                Text(UsageFormat.compact(model.tokens)).font(.system(size: 12)).foregroundStyle(.secondary)
                                    .monospacedDigit().padding(.trailing, 22)
                            }
                        }
                    }
                }
            }
        }
        .usageListBox()
    }

    private func details(_ summary: ChatUsageSummary) -> some View {
        DisclosureGroup(isExpanded: $detailsShown) {
            UsageFactsRow(facts: [
                UsageFact(value: UsageFormat.compact(summary.inputTokens), title: "Sent to the agents",
                          note: "Your messages, the files you added and the context the agents read."),
                UsageFact(value: UsageFormat.compact(summary.outputTokens), title: "Written by the agents",
                          note: "Their answers, thinking and the commands they ran."),
                UsageFact(value: UsageFormat.compact(summary.cachedTokens), title: "Read from cache",
                          note: "Context the model reused. Cheaper, and not part of the total above."),
            ])
            .padding(.top, 8)
        } label: {
            Text("Token details").font(.system(size: 12.5)).foregroundStyle(.secondary)
        }
    }

    private func load() async {
        loading = true
        let result = await state.taskAPI("dashboard/api/chats/usage?days=\(period.days)")
        loading = false
        if result.ok {
            summary = ChatUsageSummary(result.object)
            issue = nil
        } else {
            issue = result.object["message"] as? String ?? "Mac MCP is not answering."
        }
    }

    /// The agents in the order of the list, the largest first; "" is a day from a server before the split.
    static func series(_ summary: ChatUsageSummary) -> [UsageSeries] {
        summary.agents.map { UsageSeries(id: $0.id, name: $0.label, color: UsageColors.agent($0.id)) }
            + [UsageSeries(id: "", name: "Chat", color: UsageColors.agent(""))]
    }

    /// The days of the period, today last; a year as its twelve months.
    static func buckets(_ summary: ChatUsageSummary, period: ProviderUsagePeriod,
                        today: Date = Date(), calendar: Calendar = .current) -> [UsageBucket] {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.calendar = calendar
        formatter.dateFormat = "yyyy-MM-dd"
        let byDate = Dictionary(summary.daily.map { ($0.date, $0.agents) }, uniquingKeysWith: { first, _ in first })
        let start = calendar.startOfDay(for: today)
        if period == .year {
            return (0..<12).map { index in
                let month = calendar.date(byAdding: .month, value: index - 11, to: start) ?? start
                let parts = calendar.dateComponents([.year, .month], from: month)
                var sum: [String: Int] = [:]
                for day in summary.daily {
                    guard let date = formatter.date(from: day.date) else { continue }
                    let other = calendar.dateComponents([.year, .month], from: date)
                    if other.year == parts.year && other.month == parts.month {
                        sum.merge(day.agents, uniquingKeysWith: +)
                    }
                }
                return UsageBucket(id: index, title: month.formatted(.dateTime.month(.wide).year()),
                                   axis: month.formatted(.dateTime.month(.abbreviated)), parts: sum)
            }
        }
        let count = period.days
        return (0..<count).map { index in
            let day = calendar.date(byAdding: .day, value: index - (count - 1), to: start) ?? start
            let name = index == count - 1 ? "Today" : day.formatted(.dateTime.month(.abbreviated).day())
            return UsageBucket(id: index, title: name, axis: name, parts: byDate[formatter.string(from: day)] ?? [:])
        }
    }

    /// "OpenRouter/Free Models Router" reads as the model, with where it ran beside it.
    static func modelParts(_ model: String) -> (name: String, via: String?) {
        let pieces = model.split(separator: "/", maxSplits: 1).map(String.init)
        return pieces.count == 2 ? (pieces[1], pieces[0]) : (model, nil)
    }
}
