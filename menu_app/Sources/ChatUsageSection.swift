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

/// One bar of the chart: a day (30 and 90 days) or a month (a year).
struct ChatUsageBucket: Identifiable {
    let id: Int
    let title: String
    let axis: String
    let agents: [String: Int]
    var tokens: Int { agents.values.reduce(0, +) }
}

/// Each agent's colour, the same in the chart and the list.
enum ChatUsageColors {
    static func color(_ agent: String) -> Color {
        switch agent {
        case "codex": return VisualTokens.adaptive(light: 0x1F9E86, dark: 0x3CC8A8)
        case "opencode": return VisualTokens.adaptive(light: 0x7B61E8, dark: 0xA08CFF)
        case "claude": return VisualTokens.adaptive(light: 0xD9774F, dark: 0xF0956A)
        case "copilot": return VisualTokens.adaptive(light: 0x2F6FDB, dark: 0x6EA2FF)
        case "antigravity": return VisualTokens.adaptive(light: 0xB8901C, dark: 0xE8C55A)
        case "goose": return VisualTokens.adaptive(light: 0x4A9D3F, dark: 0x7BCB6E)
        case "kilo": return VisualTokens.adaptive(light: 0xD4508F, dark: 0xF080B5)
        case "kimi": return VisualTokens.adaptive(light: 0x1E95B8, dark: 0x5CCBE8)
        default: return VisualTokens.adaptive(light: 0x7D828C, dark: 0xB4B9C2)
        }
    }
}

struct ChatUsageSection: View {
    @ObservedObject var state: AppState
    @MacMCPState private var period: ProviderUsagePeriod = .month
    @MacMCPState private var summary: ChatUsageSummary?
    @MacMCPState private var loading = false
    @MacMCPState private var issue: String?
    @MacMCPState private var expanded: Set<String> = []
    @MacMCPState private var hovered: Int?
    @MacMCPState private var detailsShown = false

    private static let chartHeight: CGFloat = 150
    private static let scaleGutter: CGFloat = 44

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
                chart(summary).padding(.top, 22)
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

    // MARK: Summary

    private var header: some View {
        HStack(alignment: .top, spacing: 16) {
            VStack(alignment: .leading, spacing: 4) {
                HStack(alignment: .firstTextBaseline, spacing: 5) {
                    Text(Self.compact(summary?.tokens ?? 0))
                        .font(.system(size: 34, weight: .semibold)).monospacedDigit()
                    Text("tokens").font(.system(size: 15, weight: .medium)).foregroundStyle(.secondary)
                }
                if let summary, summary.answers > 0 {
                    Text("\(Self.count(summary.answers, "answer")) in \(Self.count(summary.chats, "chat")), with \(ChatTime.duration(summary.seconds)) of agent work.")
                        .font(.system(size: 13)).foregroundStyle(.secondary)
                }
            }
            .accessibilityElement(children: .combine)
            Spacer(minLength: 12)
            Picker("Period", selection: $period) {
                ForEach(ProviderUsagePeriod.allCases) { Text(Self.periodName($0)).tag($0) }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .fixedSize()
        }
    }

    // MARK: Chart

    private func chart(_ summary: ChatUsageSummary) -> some View {
        let buckets = Self.buckets(summary, period: period)
        let scale = Self.scaleTop(buckets.map(\.tokens).max() ?? 0)
        let order = summary.agents.map(\.id) + [""]
        return VStack(alignment: .leading, spacing: 6) {
            ZStack(alignment: .topLeading) {
                ForEach([1.0, 0.5], id: \.self) { level in
                    gridLine(label: Self.compact(Int(Double(scale) * level)))
                        .offset(y: Self.chartHeight * (1 - level))
                }
                HStack(alignment: .bottom, spacing: period == .quarter ? 2 : period == .year ? 10 : 4) {
                    ForEach(buckets) { bucket in
                        bar(bucket, order: order, scale: scale, leading: bucket.id < buckets.count / 2)
                    }
                }
                .frame(height: Self.chartHeight, alignment: .bottom)
                .padding(.trailing, Self.scaleGutter)
            }
            .frame(height: Self.chartHeight, alignment: .top)
            HStack {
                ForEach(Array(Self.axisPicks(buckets.count).enumerated()), id: \.offset) { index, pick in
                    if index > 0 { Spacer(minLength: 4) }
                    Text(buckets[pick].axis)
                }
            }
            .font(.system(size: 11)).foregroundStyle(.tertiary).monospacedDigit()
            .padding(.trailing, Self.scaleGutter)
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Tokens per \(period == .year ? "month" : "day")")
    }

    private func gridLine(label: String) -> some View {
        ZStack(alignment: .topTrailing) {
            Line().stroke(style: StrokeStyle(lineWidth: 1, dash: [3, 3]))
                .foregroundStyle(Color.primary.opacity(0.1))
                .frame(height: 1)
            Text(label).font(.system(size: 10.5)).foregroundStyle(.tertiary).monospacedDigit()
                .padding(.top, 3)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .allowsHitTesting(false)
    }

    @ViewBuilder
    private func bar(_ bucket: ChatUsageBucket, order: [String], scale: Int, leading: Bool) -> some View {
        if bucket.tokens == 0 {
            Circle().fill(Color.primary.opacity(0.14)).frame(width: 4, height: 4)
                .frame(maxWidth: .infinity)
                .accessibilityHidden(true)
        } else {
            let height = max(3, Self.chartHeight * CGFloat(bucket.tokens) / CGFloat(max(scale, 1)))
            VStack(spacing: 0) {
                // The largest agent at the bottom, as in the list below.
                ForEach(order.reversed(), id: \.self) { agent in
                    if let value = bucket.agents[agent], value > 0 {
                        Rectangle().fill(ChatUsageColors.color(agent))
                            .frame(height: max(1, height * CGFloat(value) / CGFloat(bucket.tokens)))
                    }
                }
            }
            .frame(height: height)
            .clipShape(RoundedRectangle(cornerRadius: 3, style: .continuous))
            .brightness(hovered == bucket.id ? 0.06 : 0)
            .frame(maxWidth: .infinity)
            .overlay(alignment: leading ? .topLeading : .topTrailing) {
                if hovered == bucket.id { tooltip(bucket, order: order).alignmentGuide(.top) { $0[.bottom] + 8 } }
            }
            .zIndex(hovered == bucket.id ? 1 : 0)
            .contentShape(Rectangle())
            .onHover { inside in
                if inside { hovered = bucket.id } else if hovered == bucket.id { hovered = nil }
            }
            .accessibilityElement()
            .accessibilityLabel("\(bucket.title): \(Self.compact(bucket.tokens)) tokens")
        }
    }

    private func tooltip(_ bucket: ChatUsageBucket, order: [String]) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text("\(bucket.title), \(Self.compact(bucket.tokens)) tokens").font(.system(size: 12, weight: .semibold))
            ForEach(order, id: \.self) { agent in
                if let value = bucket.agents[agent], value > 0 {
                    HStack(spacing: 6) {
                        RoundedRectangle(cornerRadius: 2).fill(ChatUsageColors.color(agent)).frame(width: 7, height: 7)
                        Text("\(label(agent)) \(Self.compact(value))").font(.system(size: 12))
                    }
                }
            }
        }
        .padding(.horizontal, 9).padding(.vertical, 6)
        .background(RoundedRectangle(cornerRadius: 7, style: .continuous).fill(.regularMaterial))
        .overlay(RoundedRectangle(cornerRadius: 7, style: .continuous).strokeBorder(Color.primary.opacity(0.1)))
        .shadow(color: .black.opacity(0.18), radius: 8, y: 3)
        .fixedSize()
        .allowsHitTesting(false)
    }

    // MARK: Agents

    private func agentList(_ summary: ChatUsageSummary) -> some View {
        VStack(spacing: 0) {
            ForEach(Array(summary.agents.enumerated()), id: \.element.id) { index, agent in
                if index > 0 { Divider() }
                agentRow(agent, share: summary.tokens > 0 ? Double(agent.tokens) / Double(summary.tokens) : 0,
                         models: summary.models.filter { $0.agent == agent.id && $0.tokens > 0 })
            }
        }
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color.primary.opacity(0.035)))
        .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(Color.primary.opacity(0.09)))
    }

    private func agentRow(_ agent: ChatUsageAgent, share: Double, models: [ChatUsageModel]) -> some View {
        let open = expanded.contains(agent.id)
        return VStack(alignment: .leading, spacing: 0) {
            Button {
                withAnimation(.easeOut(duration: 0.2)) {
                    if open { expanded.remove(agent.id) } else { expanded.insert(agent.id) }
                }
            } label: {
                HStack(spacing: 12) {
                    AgentAvatar(agent: agent.id, size: 28)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(agent.label).font(.system(size: 13, weight: .medium))
                        Text("\(Self.count(agent.answers, "answer")) in \(Self.count(agent.chats, "chat"))")
                            .font(.system(size: 11.5)).foregroundStyle(.secondary)
                    }
                    .frame(minWidth: 120, maxWidth: 190, alignment: .leading)
                    ShareMeter(share: share, color: ChatUsageColors.color(agent.id))
                        .frame(height: 6)
                    VStack(alignment: .trailing, spacing: 1) {
                        Text(Self.compact(agent.tokens)).font(.system(size: 13, weight: .medium)).monospacedDigit()
                        Text("\(Int((share * 100).rounded()))%").font(.system(size: 11)).foregroundStyle(.secondary)
                            .monospacedDigit()
                    }
                    .frame(width: 72, alignment: .trailing)
                    Image(systemName: "chevron.right")
                        .font(.system(size: 9, weight: .bold)).foregroundStyle(.tertiary)
                        .rotationEffect(.degrees(open ? 90 : 0))
                        .opacity(models.isEmpty ? 0 : 1)
                }
                .padding(.horizontal, 14).padding(.vertical, 10)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .disabled(models.isEmpty)
            .accessibilityLabel("\(agent.label), \(Self.compact(agent.tokens)) tokens, \(Int((share * 100).rounded())) percent")
            .accessibilityHint(models.isEmpty ? "" : (open ? "Hides the models" : "Shows the models it used"))
            if open {
                VStack(alignment: .leading, spacing: 7) {
                    ForEach(models) { model in
                        let parts = Self.modelParts(model.model)
                        HStack(alignment: .firstTextBaseline, spacing: 6) {
                            Text(parts.name).font(.system(size: 12.5))
                            Text([parts.via, Self.count(model.answers, "answer")].compactMap { $0 }.joined(separator: ", "))
                                .font(.system(size: 11.5)).foregroundStyle(.secondary)
                            Spacer(minLength: 8)
                            Text(Self.compact(model.tokens)).font(.system(size: 12)).foregroundStyle(.secondary)
                                .monospacedDigit()
                                .padding(.trailing, 21)
                        }
                    }
                }
                .padding(.leading, 54).padding(.trailing, 14).padding(.bottom, 11)
                .transition(.opacity)
            }
        }
    }

    // MARK: Token details

    private func details(_ summary: ChatUsageSummary) -> some View {
        DisclosureGroup(isExpanded: $detailsShown) {
            HStack(alignment: .top, spacing: 0) {
                detail(Self.compact(summary.inputTokens), "Sent to the agents",
                       "Your messages, the files you added and the context the agents read.")
                Divider()
                detail(Self.compact(summary.outputTokens), "Written by the agents",
                       "Their answers, thinking and the commands they ran.")
                Divider()
                detail(Self.compact(summary.cachedTokens), "Read from cache",
                       "Context the model reused. Cheaper, and not part of the total above.")
            }
            .fixedSize(horizontal: false, vertical: true)
            .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color.primary.opacity(0.035)))
            .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(Color.primary.opacity(0.09)))
            .padding(.top, 8)
        } label: {
            Text("Token details").font(.system(size: 12.5)).foregroundStyle(.secondary)
        }
    }

    private func detail(_ value: String, _ title: String, _ explanation: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(value).font(.system(size: 15, weight: .semibold)).monospacedDigit()
            Text(title).font(.system(size: 12))
            Text(explanation).font(.system(size: 11.5)).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, 2)
        }
        .padding(.horizontal, 14).padding(.vertical, 12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
    }

    // MARK: Data

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

    private func label(_ agent: String) -> String {
        summary?.agents.first { $0.id == agent }?.label ?? agent
    }

    static func periodName(_ period: ProviderUsagePeriod) -> String {
        switch period {
        case .month: return "30 Days"
        case .quarter: return "90 Days"
        case .year: return "Year"
        }
    }

    /// The days of the period, today last; a year as its twelve months.
    static func buckets(_ summary: ChatUsageSummary, period: ProviderUsagePeriod,
                        today: Date = Date(), calendar: Calendar = .current) -> [ChatUsageBucket] {
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
                return ChatUsageBucket(id: index, title: month.formatted(.dateTime.month(.wide).year()),
                                       axis: month.formatted(.dateTime.month(.abbreviated)), agents: sum)
            }
        }
        let count = period.days
        return (0..<count).map { index in
            let day = calendar.date(byAdding: .day, value: index - (count - 1), to: start) ?? start
            let isToday = index == count - 1
            let name = isToday ? "Today" : day.formatted(.dateTime.month(.abbreviated).day())
            return ChatUsageBucket(id: index, title: name, axis: name, agents: byDate[formatter.string(from: day)] ?? [:])
        }
    }

    /// The top of the scale: a round number just above the tallest bar.
    static func scaleTop(_ value: Int) -> Int {
        let step = value > 100_000 ? 50_000 : value > 10_000 ? 10_000 : value > 1_000 ? 1_000 : 100
        return max(step, Int((Double(value) / Double(step)).rounded(.up)) * step)
    }

    static func axisPicks(_ count: Int) -> [Int] {
        guard count > 4 else { return Array(0..<count) }
        return [0, count / 4, count / 2, count * 3 / 4, count - 1]
    }

    /// "OpenRouter/Free Models Router" reads as the model, with where it ran beside it.
    static func modelParts(_ model: String) -> (name: String, via: String?) {
        let pieces = model.split(separator: "/", maxSplits: 1).map(String.init)
        return pieces.count == 2 ? (pieces[1], pieces[0]) : (model, nil)
    }

    static func count(_ value: Int, _ noun: String) -> String { "\(value) \(noun)\(value == 1 ? "" : "s")" }

    /// "820", "12.4K", "100K", "2.2M".
    static func compact(_ value: Int) -> String {
        let absolute = Double(abs(value))
        let text: String
        if absolute >= 1_000_000 { text = String(format: "%.1fM", Double(value) / 1_000_000) }
        else if absolute >= 1_000 { text = String(format: "%.1fK", Double(value) / 1_000) }
        else { return "\(value)" }
        return text.replacingOccurrences(of: ".0", with: "")
    }
}

/// A thin track with the agent's share of the tokens filled in its colour.
private struct ShareMeter: View {
    let share: Double
    let color: Color

    var body: some View {
        GeometryReader { proxy in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.primary.opacity(0.1))
                Capsule().fill(color).frame(width: max(6, proxy.size.width * share))
            }
        }
        .accessibilityHidden(true)
    }
}

/// A horizontal line across its frame, for the chart's dashed scale lines.
private struct Line: Shape {
    func path(in rect: CGRect) -> Path {
        var path = Path()
        path.move(to: CGPoint(x: rect.minX, y: rect.midY))
        path.addLine(to: CGPoint(x: rect.maxX, y: rect.midY))
        return path
    }
}
