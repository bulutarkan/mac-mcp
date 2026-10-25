import SwiftUI

// MARK: Usage › Chat: the owner's own conversations in Mac MCP Chat, counted apart from the agents
// Mac MCP starts (Delegated Agents). Read from /dashboard/api/chats/usage.

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
            ChatUsageDay(date: $0["date"] as? String ?? "", tokens: int($0["tokens"]), answers: int($0["turns"]))
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

    init(state: AppState) {
        self.state = state
    }

    var body: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 12) {
                header
                if loading && summary == nil {
                    HStack(spacing: 8) {
                        ProgressView().controlSize(.small)
                        Text("Loading chat usage…").font(.caption).foregroundStyle(.secondary)
                    }
                    .padding(.vertical, 6)
                } else if let issue {
                    Text(issue).font(.caption).foregroundStyle(.secondary)
                } else if let summary, summary.answers > 0 {
                    stats(summary)
                    if summary.daily.count > 1 { dailyBars(summary) }
                    agentRows(summary)
                    if !summary.models.isEmpty { modelRows(summary) }
                } else {
                    Text("No answers in Mac MCP Chat in this period. Open it from the menu: Chat… (⇧⌘N).")
                        .font(.caption).foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.vertical, 4)
                }
            }
            .padding(.vertical, 2)
        } label: {
            Label("Chat", systemImage: "bubble.left.and.text.bubble.right")
        }
        .task(id: period) { await load() }
    }

    private var header: some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 5) {
                    Text("Chat Tokens").font(.subheadline.weight(.semibold))
                    Image(systemName: "info.circle").font(.caption).foregroundStyle(.tertiary)
                        .help("What the agents reported for your own conversations in Mac MCP Chat, on your own accounts. Counted apart from Delegated Agents and never added to Tool data processed.")
                        .accessibilityLabel("About Chat Tokens")
                }
                if let summary, summary.answers > 0 {
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text(Self.compact(summary.tokens))
                            .font(.system(size: 26, weight: .semibold, design: .rounded)).monospacedDigit()
                        Text("tokens in \(Self.count(summary.answers, "answer"))").font(.callout).foregroundStyle(.secondary)
                    }
                }
            }
            Spacer(minLength: 12)
            Picker("Period", selection: $period) {
                ForEach(ProviderUsagePeriod.allCases) { Text($0.rawValue).tag($0) }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .fixedSize()
        }
    }

    private func stats(_ summary: ChatUsageSummary) -> some View {
        HStack(spacing: 12) {
            UsageStat(title: "Chats", value: "\(summary.chats)")
            UsageStat(title: "Sent", value: Self.compact(summary.inputTokens), help: "Tokens the agents read: your messages, files and their own context")
            UsageStat(title: "Written", value: Self.compact(summary.outputTokens), help: "Tokens the agents wrote")
            UsageStat(title: "From cache", value: Self.compact(summary.cachedTokens), help: "Tokens the model read from its cache (cheaper)")
            UsageStat(title: "Time worked", value: ChatTime.duration(summary.seconds))
        }
    }

    /// Tokens per day as quiet columns, today on the right.
    private func dailyBars(_ summary: ChatUsageSummary) -> some View {
        let peak = max(1, summary.daily.map(\.tokens).max() ?? 1)
        return HStack(alignment: .bottom, spacing: 3) {
            ForEach(summary.daily) { day in
                RoundedRectangle(cornerRadius: 2, style: .continuous)
                    .fill(ChatStyle.accent.opacity(0.75))
                    .frame(height: max(3, 44 * CGFloat(day.tokens) / CGFloat(peak)))
                    .frame(maxWidth: 18)
                    .help("\(day.date): \(Self.compact(day.tokens)) tokens, \(day.answers) answers")
            }
        }
        .frame(height: 44, alignment: .bottom)
        .accessibilityLabel("Chat tokens per day")
    }

    private func agentRows(_ summary: ChatUsageSummary) -> some View {
        VStack(spacing: 6) {
            ForEach(summary.agents) { agent in
                HStack(spacing: 10) {
                    AgentAvatar(agent: agent.id, size: 22)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(agent.label).font(.callout.weight(.medium))
                        Text("\(Self.count(agent.answers, "answer")) in \(Self.count(agent.chats, "chat"))")
                            .font(.caption2).foregroundStyle(.secondary)
                    }
                    Spacer()
                    VStack(alignment: .trailing, spacing: 1) {
                        Text(Self.compact(agent.tokens)).font(.callout.weight(.semibold)).monospacedDigit()
                        Text("\(Self.compact(agent.inputTokens)) in · \(Self.compact(agent.outputTokens)) out")
                            .font(.caption2).foregroundStyle(.secondary).monospacedDigit()
                    }
                }
                .padding(.vertical, 2)
            }
        }
    }

    private func modelRows(_ summary: ChatUsageSummary) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Models").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            ForEach(summary.models) { model in
                HStack {
                    Text(model.model).font(.caption).lineLimit(1)
                    Text(model.agent).font(.caption2).foregroundStyle(.tertiary)
                    Spacer()
                    Text("\(model.answers) · \(Self.compact(model.tokens))").font(.caption).foregroundStyle(.secondary)
                        .monospacedDigit()
                }
            }
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

    static func count(_ value: Int, _ noun: String) -> String { "\(value) \(noun)\(value == 1 ? "" : "s")" }

    static func compact(_ value: Int) -> String {
        let absolute = Double(abs(value))
        if absolute >= 1_000_000 { return String(format: "%.1fM", Double(value) / 1_000_000) }
        if absolute >= 1_000 { return String(format: "%.1fK", Double(value) / 1_000) }
        return "\(value)"
    }
}
