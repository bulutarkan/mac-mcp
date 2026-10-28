import AppKit
import SwiftUI

// MARK: - The button on a Team History card

/// Opens what a team's agents wrote to each other (#104). Faded and unclickable when they wrote nothing;
/// a count the server could not give (nil) still opens, so nothing is hidden by an older server.
struct TeamMessagesButton: View {
    let count: Int?
    let action: () -> Void

    @MacMCPState private var hovering = false

    private var empty: Bool { count == 0 }

    var body: some View {
        Button(action: action) {
            HStack(spacing: 4) {
                Image(systemName: empty ? "bubble.left.and.bubble.right" : "bubble.left.and.bubble.right.fill")
                    .font(.system(size: 10.5, weight: .semibold))
                if let count, count > 0 {
                    Text("\(count)")
                        .font(.system(size: 11, weight: .semibold, design: .rounded))
                        .monospacedDigit()
                }
            }
            .foregroundStyle(empty ? Color.secondary : VisualTokens.accent)
            .padding(.horizontal, 8)
            .frame(height: 22)
            .background(Capsule().fill(empty ? Color.primary.opacity(0.03) : VisualTokens.accent.opacity(hovering ? 0.22 : 0.12)))
            .overlay(Capsule().strokeBorder(empty ? Color.primary.opacity(0.06) : VisualTokens.accent.opacity(0.28), lineWidth: 1))
            .opacity(empty ? 0.45 : 1)
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .disabled(empty)
        .onHover { hovering = $0 && !empty }
        .animation(.easeOut(duration: 0.12), value: hovering)
        .help(empty ? "The agents did not write to each other" : "Team messages")
        .accessibilityLabel(accessibilityText)
    }

    private var accessibilityText: String {
        guard let count else { return "Team messages" }
        return count == 0 ? "No team messages" : "Team messages, \(TeamHistoryFormat.count(count, "message"))"
    }
}

// MARK: - Who wrote and who was written to

/// Someone in a team conversation, drawn like a member in the team builder.
struct TeamMessageParty: Hashable {
    enum Kind: Hashable {
        case lead
        /// `number` is the agent's place in the team, shown like the team builder's jersey numbers.
        case agent(provider: String, number: Int)
        case everyone
        case other
    }

    let key: String
    let name: String
    let kind: Kind

    static let lead = TeamMessageParty(key: "lead", name: "Lead", kind: .lead)
    static let everyone = TeamMessageParty(key: "everyone", name: "Everyone", kind: .everyone)
}

/// Turns the room's labels ("task sec-privacy", "lead", "everyone") into the team's agents.
struct TeamMessageCast {
    let agents: [TeamHistoryAgent]
    private let byTask: [String: TeamHistoryAgent]
    private let byLabel: [String: TeamHistoryAgent]
    private let numbers: [String: Int]

    init(team: TeamHistoryTeam) {
        agents = team.agents
        byTask = Dictionary(team.agents.compactMap { agent in agent.taskID.map { ($0, agent) } }, uniquingKeysWith: { first, _ in first })
        byLabel = Dictionary(team.agents.map { ($0.label, $0) }, uniquingKeysWith: { first, _ in first })
        numbers = Dictionary(team.agents.enumerated().map { ($1.agentID, $0 + 1) }, uniquingKeysWith: { first, _ in first })
    }

    func sender(_ message: TeamMessageItem) -> TeamMessageParty {
        message.fromLead == true ? .lead : party(message.from)
    }

    func recipient(_ message: TeamMessageItem) -> TeamMessageParty {
        party(message.toLabel ?? "everyone")
    }

    func party(_ raw: String) -> TeamMessageParty {
        switch raw.lowercased() {
        case "everyone": return .everyone
        case "lead": return .lead
        default: break
        }
        if raw.hasPrefix("task ") {
            let task = String(raw.dropFirst(5))
            if let agent = byTask[task] ?? byLabel[task] { return member(agent) }
            return TeamMessageParty(key: raw, name: task, kind: .other)
        }
        if let agent = byLabel[raw] { return member(agent) }
        return TeamMessageParty(key: raw, name: raw, kind: .other)
    }

    func member(_ agent: TeamHistoryAgent) -> TeamMessageParty {
        let provider = agent.provider ?? ""
        return TeamMessageParty(key: agent.agentID, name: agent.label,
                                kind: provider.isEmpty ? .other : .agent(provider: provider, number: numbers[agent.agentID] ?? 0))
    }
}

/// A party's badge: the provider token for an agent, a gold crown for the lead.
struct TeamMessageAvatar: View {
    let party: TeamMessageParty
    var size: CGFloat = 30

    var body: some View {
        switch party.kind {
        case .agent(let provider, let number):
            // The jersey number tells apart agents of one provider; too small to read on a tiny badge.
            ProviderToken(provider: provider, size: size, number: size >= 24 && number > 0 ? number : nil)
        case .lead:
            tile("crown.fill", fill: VisualTokens.brand, foreground: VisualTokens.onBrand)
        case .everyone:
            tile("person.3.fill", fill: Color.secondary.opacity(0.35), foreground: .white)
        case .other:
            tile("person.fill", fill: Color.secondary.opacity(0.45), foreground: .white)
        }
    }

    private func tile(_ symbol: String, fill: Color, foreground: Color) -> some View {
        let shape = RoundedRectangle(cornerRadius: size * 0.3, style: .continuous)
        return shape.fill(fill)
            .overlay(shape.fill(LinearGradient(colors: [.white.opacity(0.26), .clear], startPoint: .top, endPoint: .center)))
            .overlay(shape.strokeBorder(.white.opacity(0.22), lineWidth: 1))
            .overlay(
                Image(systemName: symbol)
                    .font(.system(size: size * (symbol == "person.3.fill" ? 0.32 : 0.42), weight: .bold))
                    .foregroundStyle(foreground)
            )
            .frame(width: size, height: size)
            .accessibilityHidden(true)
    }
}

// MARK: - The conversation

/// A team's conversation in its own sheet: who wrote to whom and when, as a timeline that settles in
/// message by message. Reading it marks nothing read; the server keeps it a record, not an inbox.
struct TeamMessagesSheet: View {
    let team: TeamHistoryTeam
    let load: (String, Int) async -> TeamMessagesEnvelope?

    @Environment(\.dismiss) private var dismiss
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    @MacMCPState private var messages: [TeamMessageItem] = []
    /// The first page: the team-wide counts and result reads.
    @MacMCPState private var summary: TeamMessagesEnvelope?
    @MacMCPState private var nextAfter: Int?
    @MacMCPState private var failed = false
    @MacMCPState private var loadingMore = false
    @MacMCPState private var revealed = false
    /// A party's key: only what they wrote, what was written to them, and what went to everyone.
    @MacMCPState private var focus: String?

    private let cast: TeamMessageCast

    init(team: TeamHistoryTeam, load: @escaping (String, Int) async -> TeamMessagesEnvelope?) {
        self.team = team
        self.load = load
        cast = TeamMessageCast(team: team)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
                .padding(.horizontal, 22).padding(.top, 20).padding(.bottom, 14)
            if summary != nil && !originals.isEmpty {
                participants
                    .padding(.bottom, 12)
            }
            Divider()
            ScrollView {
                content
                    .padding(.horizontal, 22).padding(.vertical, 18)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            Divider()
            HStack {
                Button { copyConversation() } label: {
                    Label("Copy Conversation", systemImage: "doc.on.doc")
                }
                .disabled(originals.isEmpty)
                Spacer()
                Button("Done") { dismiss() }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
            }
            .padding(.horizontal, 22).padding(.vertical, 12)
        }
        .frame(width: 700, height: 640)
        .onExitCommand { dismiss() }
        .task(id: team.teamID) { await loadFirst() }
    }

    // MARK: Header

    private var header: some View {
        HStack(alignment: .center, spacing: 14) {
            RoundedRectangle(cornerRadius: 11, style: .continuous)
                .fill(LinearGradient(colors: [VisualTokens.accent.opacity(0.95), VisualTokens.accent.opacity(0.65)],
                                     startPoint: .topLeading, endPoint: .bottomTrailing))
                .overlay(
                    Image(systemName: "bubble.left.and.bubble.right.fill")
                        .font(.system(size: 17, weight: .semibold))
                        .foregroundStyle(.white)
                )
                .frame(width: 40, height: 40)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 3) {
                Text("Team Messages")
                    .font(.system(size: 17, weight: .semibold))
                Text(subtitle)
                    .font(SettingsType.meta)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            Spacer(minLength: 12)
            // The card's count shows while the conversation loads, so the header does not jump.
            if let sent = summary?.messageCounts?.sent ?? team.messageCount, sent > 0 {
                stat("\(sent)", sent == 1 ? "message" : "messages")
            }
            if let forwarded = summary?.messageCounts?.forwarded, forwarded > 0 {
                stat("\(forwarded)", "passed on")
            }
            stat("\(cast.agents.count)", cast.agents.count == 1 ? "agent" : "agents")
        }
    }

    private var subtitle: String {
        let title = team.title.isEmpty ? "Untitled team" : team.title
        guard let created = team.createdAt else { return title }
        return "\(title) · \(Date(timeIntervalSince1970: created).formatted(.relative(presentation: .named)))"
    }

    private func stat(_ value: String, _ caption: String) -> some View {
        VStack(alignment: .trailing, spacing: 1) {
            Text(value)
                .font(.system(size: 15, weight: .semibold, design: .rounded))
                .monospacedDigit()
            Text(caption)
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
        .frame(minWidth: 44, alignment: .trailing)
    }

    // MARK: Who took part

    private struct Speaker: Identifiable {
        let party: TeamMessageParty
        let sent: Int
        var id: String { party.key }
    }

    /// The lead, then the team's agents in order, then anyone else who wrote; with how much each wrote.
    private var speakers: [Speaker] {
        var sent: [String: Int] = [:]
        var parties: [String: TeamMessageParty] = [:]
        for message in originals {
            let party = cast.sender(message)
            sent[party.key, default: 0] += 1
            parties[party.key] = party
        }
        var ordered: [TeamMessageParty] = []
        if parties["lead"] != nil { ordered.append(.lead) }
        ordered += cast.agents.map(cast.member)
        ordered += parties.values.filter { party in !ordered.contains { $0.key == party.key } }.sorted { $0.name < $1.name }
        return ordered.map { Speaker(party: $0, sent: sent[$0.key] ?? 0) }
    }

    private var participants: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                chip(selected: focus == nil, help: "Show every message") {
                    Image(systemName: "line.3.horizontal.decrease")
                        .font(.system(size: 10, weight: .semibold))
                    Text("All")
                }
                ForEach(speakers) { speaker in
                    chip(selected: focus == speaker.party.key,
                         help: "Only what \(speaker.party.name) wrote or was sent, and messages to everyone") {
                        TeamMessageAvatar(party: speaker.party, size: 16)
                        Text(speaker.party.name).lineLimit(1)
                        Text("\(speaker.sent)")
                            .foregroundStyle(.secondary)
                            .monospacedDigit()
                    } action: {
                        focus = focus == speaker.party.key ? nil : speaker.party.key
                    }
                    .opacity(speaker.sent == 0 ? 0.6 : 1)
                }
            }
            .padding(.horizontal, 22)
        }
        .mask(LinearGradient(stops: [.init(color: .clear, location: 0), .init(color: .black, location: 0.03),
                                     .init(color: .black, location: 0.95), .init(color: .clear, location: 1)],
                             startPoint: .leading, endPoint: .trailing))
    }

    private func chip<Content: View>(selected: Bool, help: String, @ViewBuilder label: () -> Content,
                                   action: (() -> Void)? = nil) -> some View {
        Button {
            withAnimation(reduceMotion ? nil : .spring(response: 0.35, dampingFraction: 0.86)) {
                if let action { action() } else { focus = nil }
            }
        } label: {
            HStack(spacing: 5) { label() }
                .font(.system(size: 11.5, weight: .medium))
                .padding(.leading, 6).padding(.trailing, 9)
                .frame(height: 24)
                .background(Capsule().fill(selected ? VisualTokens.accent.opacity(0.16) : Color.primary.opacity(0.05)))
                .overlay(Capsule().strokeBorder(selected ? VisualTokens.accent.opacity(0.5) : Color.primary.opacity(0.07), lineWidth: 1))
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .help(help)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    // MARK: Timeline

    @ViewBuilder
    private var content: some View {
        if summary == nil && !failed {
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text("Loading messages…").foregroundStyle(.secondary)
            }
            .font(SettingsType.note)
            .frame(maxWidth: .infinity, minHeight: 240)
        } else if failed && summary == nil {
            VStack(spacing: 10) {
                Label("Could not load this team's messages.", systemImage: "exclamationmark.triangle")
                    .foregroundStyle(VisualTokens.warning)
                Button("Try Again") { Task { await loadFirst() } }
            }
            .font(SettingsType.note)
            .frame(maxWidth: .infinity, minHeight: 240)
        } else if originals.isEmpty {
            VStack(spacing: 8) {
                Image(systemName: "bubble.left.and.bubble.right")
                    .font(.system(size: 26, weight: .light))
                    .foregroundStyle(.tertiary)
                Text("The agents did not write to each other.")
                    .font(SettingsType.note)
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, minHeight: 240)
        } else {
            VStack(alignment: .leading, spacing: 0) {
                ZStack(alignment: .topLeading) {
                    // The thread that ties the avatars together.
                    Rectangle()
                        .fill(Color.primary.opacity(0.07))
                        .frame(width: 2)
                        .padding(.leading, 15).padding(.vertical, 18)
                    VStack(alignment: .leading, spacing: 0) {
                        ForEach(Array(entries.enumerated()), id: \.element.id) { index, entry in
                            entryView(entry)
                                .opacity(revealed ? 1 : 0)
                                .offset(y: revealed ? 0 : 14)
                                .animation(reduceMotion ? nil : .spring(response: 0.5, dampingFraction: 0.84)
                                    .delay(Double(min(index, 14)) * 0.045), value: revealed)
                                .transition(.opacity.combined(with: .move(edge: .top)))
                        }
                    }
                }
                if nextAfter != nil {
                    Button { Task { await loadMore() } } label: {
                        Label(loadingMore ? "Loading…" : "Show Later Messages", systemImage: "arrow.down.circle")
                    }
                    .buttonStyle(.link)
                    .font(SettingsType.note)
                    .disabled(loadingMore)
                    .padding(.leading, 44).padding(.top, 6)
                }
                if let reads = summary?.dependencyReads, !reads.isEmpty {
                    resultReads(reads)
                        .padding(.top, 18)
                }
            }
        }
    }

    private enum Entry: Identifiable {
        case day(String)
        case pause(after: String, text: String)
        case message(TeamMessageItem, copy: TeamMessageItem?)

        var id: String {
            switch self {
            case .day(let text): return "day:\(text)"
            case .pause(let after, _): return "pause:\(after)"
            case .message(let message, _): return message.id
            }
        }
    }

    /// Messages passed on to the lead because their recipient had finished, by the message they copy.
    private var copies: [String: TeamMessageItem] {
        let ids = Set(messages.map(\.id))
        return Dictionary(messages.compactMap { message in
            message.forwardedFrom.flatMap { ids.contains($0) ? ($0, message) : nil }
        }, uniquingKeysWith: { first, _ in first })
    }

    /// Each message once: a copy whose original is shown is told on the original instead.
    private var originals: [TeamMessageItem] {
        let ids = Set(messages.map(\.id))
        return messages.filter { message in message.forwardedFrom.map { !ids.contains($0) } ?? true }
    }

    private var entries: [Entry] {
        let copies = copies
        let shown = originals.filter { message in
            guard let focus else { return true }
            let recipient = cast.recipient(message)
            return cast.sender(message).key == focus || recipient.key == focus || recipient.kind == .everyone
        }
        let days = Set(shown.compactMap { TeamMessageTime.date($0.sentAt).map { Calendar.current.startOfDay(for: $0) } })
        var out: [Entry] = []
        var previous: Date?
        for message in shown {
            let date = TeamMessageTime.date(message.sentAt)
            if let date {
                if days.count > 1, previous.map({ !Calendar.current.isDate($0, inSameDayAs: date) }) ?? true {
                    out.append(.day(TeamMessageTime.day(date)))
                } else if let previous, date.timeIntervalSince(previous) >= 300 {
                    out.append(.pause(after: message.id, text: TeamMessageTime.pause(date.timeIntervalSince(previous))))
                }
                previous = date
            }
            out.append(.message(message, copy: copies[message.id]))
        }
        return out
    }

    @ViewBuilder
    private func entryView(_ entry: Entry) -> some View {
        switch entry {
        case .day(let text):
            marker(text, weight: .semibold)
        case .pause(_, let text):
            marker(text, weight: .regular)
        case .message(let message, let copy):
            TeamMessageBubble(message: message, sender: cast.sender(message), recipient: cast.recipient(message),
                              passedOnTo: copy.map { cast.recipient($0) })
                .padding(.vertical, 7)
        }
    }

    private func marker(_ text: String, weight: Font.Weight) -> some View {
        HStack(spacing: 8) {
            Circle()
                .fill(Color.primary.opacity(0.18))
                .frame(width: 7, height: 7)
                .frame(width: 32)
            Text(text)
                .font(.system(size: 10.5, weight: weight))
                .foregroundStyle(.tertiary)
                .textCase(weight == .semibold ? .uppercase : nil)
        }
        .padding(.vertical, 8)
    }

    private func resultReads(_ reads: [TeamDependencyRead]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Results read")
                .font(SettingsType.title)
            ForEach(Array(reads.enumerated()), id: \.offset) { _, read in
                HStack(spacing: 8) {
                    TeamMessageAvatar(party: cast.party("task \(read.readerTask)"), size: 18)
                    Text(cast.party("task \(read.readerTask)").name).fontWeight(.semibold)
                    Image(systemName: "doc.text.magnifyingglass")
                        .foregroundStyle(.secondary)
                        .accessibilityHidden(true)
                    Text("read the full result of")
                        .foregroundStyle(.secondary)
                    TeamMessageAvatar(party: cast.party("task \(read.targetTask)"), size: 18)
                    Text(cast.party("task \(read.targetTask)").name).fontWeight(.semibold)
                    if read.count > 1 {
                        Text("\(read.count)×").foregroundStyle(.secondary).monospacedDigit()
                    }
                }
                .font(SettingsType.note)
                .accessibilityElement(children: .combine)
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Color.primary.opacity(0.035)))
    }

    // MARK: Loading

    private func loadFirst() async {
        failed = false
        guard let page = await load(team.teamID, 0) else {
            failed = true
            return
        }
        summary = page
        messages = page.messages
        nextAfter = page.hasMore ? page.nextAfter : nil
        reveal()
    }

    private func loadMore() async {
        guard let after = nextAfter else { return }
        loadingMore = true
        defer { loadingMore = false }
        guard let page = await load(team.teamID, after) else { return }
        withAnimation(reduceMotion ? nil : .easeOut(duration: 0.25)) {
            messages += page.messages.filter { message in !messages.contains { $0.id == message.id } }
            nextAfter = page.hasMore ? page.nextAfter : nil
        }
    }

    private func reveal() {
        if reduceMotion {
            revealed = true
        } else {
            // One frame later, so the rows start hidden and settle in one after another.
            DispatchQueue.main.async { revealed = true }
        }
    }

    private func copyConversation() {
        let text = originals.map { message in
            let time = TeamMessageTime.date(message.sentAt).map { $0.formatted(date: .abbreviated, time: .shortened) } ?? message.sentAt
            return "\(time)  \(cast.sender(message).name) → \(cast.recipient(message).name): \(message.body)"
        }.joined(separator: "\n\n")
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }
}

// MARK: - One message

private struct TeamMessageBubble: View {
    let message: TeamMessageItem
    let sender: TeamMessageParty
    let recipient: TeamMessageParty
    /// Who got a copy because the recipient had finished, when that copy is in the conversation.
    let passedOnTo: TeamMessageParty?

    @MacMCPState private var expanded = false

    private var isLead: Bool { sender.kind == .lead }
    /// About what eight lines of the sheet hold; shorter messages are shown whole without a Show More.
    private var long: Bool { message.body.count > 700 || message.body.filter { $0 == "\n" }.count > 7 }

    private var tint: Color {
        switch sender.kind {
        case .agent(let provider, _): return ProviderTokenStyle.tint(provider)
        case .lead: return VisualTokens.brand
        case .everyone, .other: return .secondary
        }
    }

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            TeamMessageAvatar(party: sender, size: 32)
                .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Color(nsColor: .windowBackgroundColor)).padding(-3))  // cuts the thread line
            VStack(alignment: .leading, spacing: 6) {
                heading
                bubble
                if let note = passedOnNote {
                    Label(note, systemImage: "arrow.uturn.right")
                        .font(SettingsType.meta)
                        .foregroundStyle(VisualTokens.warning)
                }
            }
        }
        .accessibilityElement(children: .combine)
    }

    private var heading: some View {
        HStack(spacing: 6) {
            Text(sender.name)
                .font(.system(size: 13, weight: .semibold, design: .rounded))
                .lineLimit(1)
            if isLead {
                Text("LEAD")
                    .font(.system(size: 8.5, weight: .heavy))
                    .tracking(0.6)
                    .foregroundStyle(VisualTokens.onBrand)
                    .padding(.horizontal, 5).padding(.vertical, 1.5)
                    .background(Capsule().fill(VisualTokens.brand))
            }
            Image(systemName: "arrow.right")
                .font(.system(size: 9, weight: .bold))
                .foregroundStyle(.tertiary)
                .accessibilityLabel("to")
            TeamMessageAvatar(party: recipient, size: 16)
            Text(recipient.name)
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(.secondary)
                .lineLimit(1)
            if let kind = kindStyle {
                Label(kind.text, systemImage: kind.symbol)
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(kind.tint)
                    .padding(.horizontal, 6).padding(.vertical, 2)
                    .background(Capsule().fill(kind.tint.opacity(0.12)))
            }
            if message.forwardedFrom != nil {
                Label("Passed on", systemImage: "arrow.uturn.right")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(VisualTokens.warning)
            }
            Spacer(minLength: 6)
            Image(systemName: delivery.symbol)
                .font(.system(size: 10.5, weight: .medium))
                .foregroundStyle(delivery.tint)
                .help(delivery.text)
                .accessibilityLabel(delivery.text)
            Text(TeamMessageTime.clock(message.sentAt))
                .font(SettingsType.meta)
                .foregroundStyle(.tertiary)
                .monospacedDigit()
                .help(TeamMessageTime.date(message.sentAt).map { $0.formatted(date: .complete, time: .standard) } ?? message.sentAt)
        }
    }

    private var bubble: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(styledBody)
                .font(SettingsType.body)
                .lineSpacing(2)
                .lineLimit(long && !expanded ? 8 : nil)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            if long {
                Button(expanded ? "Show Less" : "Show More") {
                    withAnimation(.easeInOut(duration: 0.2)) { expanded.toggle() }
                }
                .buttonStyle(.link)
                .font(SettingsType.meta)
            }
        }
        .padding(.horizontal, 13).padding(.vertical, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(tint.opacity(isLead ? 0.1 : 0.06))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .strokeBorder(tint.opacity(isLead ? 0.45 : 0.16), lineWidth: 1)
        )
    }

    /// Agents write `code` and **bold** in their messages; only inline marks are read, line breaks are kept.
    private var styledBody: AttributedString {
        (try? AttributedString(markdown: message.body,
                               options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)))
            ?? AttributedString(message.body)
    }

    private var passedOnNote: String? {
        guard message.deliveryNote == "recipient_finished" else { return nil }
        if let passedOnTo {
            return "\(recipient.name) had already finished, so it went to \(passedOnTo.kind == .lead ? "the lead" : passedOnTo.name)"
        }
        return "\(recipient.name) had already finished"
    }

    private var kindStyle: (text: String, symbol: String, tint: Color)? {
        switch message.kind {
        case "update": return nil
        case "reply": return ("Reply", "arrowshape.turn.up.left.fill", .secondary)
        case "question": return ("Question", "questionmark.circle.fill", VisualTokens.accent)
        case "blocker": return ("Blocker", "exclamationmark.octagon.fill", VisualTokens.error)
        case "correction": return ("Correction", "pencil.circle.fill", VisualTokens.warning)
        default: return (message.kind.capitalized, "tag.fill", .secondary)
        }
    }

    private var delivery: (text: String, symbol: String, tint: Color) {
        let opened = message.deliveries.filter { $0.readAt != nil }.map(\.label)
        if !opened.isEmpty { return ("Opened by \(opened.joined(separator: ", "))", "envelope.open.fill", VisualTokens.success) }
        let shown = message.deliveries.map(\.label)
        if !shown.isEmpty { return ("Shown to \(shown.joined(separator: ", ")), not opened", "eye.fill", .secondary) }
        return ("Nobody saw it", "eye.slash", Color.secondary.opacity(0.6))
    }
}

// MARK: - Time

enum TeamMessageTime {
    /// The server writes "+0300"; the parser wants "+03:00".
    static func date(_ stamp: String) -> Date? {
        let parser = ISO8601DateFormatter()
        parser.formatOptions = [.withInternetDateTime]
        let fixed = stamp.replacingOccurrences(of: #"([+-]\d{2})(\d{2})$"#, with: "$1:$2", options: .regularExpression)
        return parser.date(from: fixed)
    }

    static func clock(_ stamp: String) -> String {
        guard let date = date(stamp) else { return stamp }
        return date.formatted(date: .omitted, time: .shortened)
    }

    static func day(_ date: Date) -> String {
        if Calendar.current.isDateInToday(date) { return "Today" }
        if Calendar.current.isDateInYesterday(date) { return "Yesterday" }
        return date.formatted(date: .complete, time: .omitted)
    }

    static func pause(_ seconds: TimeInterval) -> String {
        let minutes = Int(seconds / 60)
        if minutes < 60 { return "\(minutes) min later" }
        return "\(minutes / 60) h \(minutes % 60) min later"
    }
}
