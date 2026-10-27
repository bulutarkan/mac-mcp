import AppKit
import SwiftUI

/// Settings › Agents › Teams: named agent teams the owner builds once and reuses. Providers wait on
/// a bench and are dragged into numbered slots; each member gets a name, model, thinking and role.
/// Teams are saved to settings.json under `subagents.team_presets`, one team at a time.
struct TeamPresetsSection: View {
    @ObservedObject var settings: SettingsStore
    let providers: [ProviderInfo]
    let catalogs: [String: ProviderCatalogInfo]
    let phase: AgentChoice.ProvidersPhase
    let openProviders: () -> Void

    /// The team being built or edited; nil shows the saved teams.
    @MacMCPState private var draft: TeamPreset?
    /// The saved team the draft started from; nil for a new team.
    @MacMCPState private var original: TeamPreset?
    @MacMCPState private var notice: String?
    @MacMCPState private var noticeIsError = false
    @MacMCPState private var pendingDelete: TeamPreset?

    var body: some View {
        Group {
            if let preset = Binding($draft) {
                TeamPresetBuilder(
                    preset: preset,
                    original: original,
                    others: settings.teamPresets,
                    providers: providers,
                    catalogs: catalogs,
                    phase: phase,
                    locked: settings.providerSettingsLocked,
                    problem: noticeIsError ? notice : nil,
                    openProviders: openProviders,
                    close: closeBuilder,
                    save: save
                )
                .transition(AppMotion.arrive)
            } else {
                TeamPresetInventory(
                    presets: settings.teamPresets,
                    issue: settings.teamPresetsIssue,
                    notice: notice,
                    noticeIsError: noticeIsError,
                    providers: providers,
                    catalogs: catalogs,
                    phase: phase,
                    locked: settings.providerSettingsLocked,
                    openProviders: openProviders,
                    create: create,
                    edit: edit,
                    duplicate: duplicate,
                    delete: { pendingDelete = $0 }
                )
                .transition(AppMotion.arrive)
            }
        }
        .animation(AppMotion.content, value: draft == nil)
        .onAppear { settings.reloadTeamPresets() }
        .confirmationDialog(
            "Delete “\(pendingDelete?.name ?? "")”?",
            isPresented: Binding(get: { pendingDelete != nil }, set: { if !$0 { pendingDelete = nil } }),
            presenting: pendingDelete
        ) { preset in
            Button("Delete Team", role: .destructive) { delete(preset) }
            Button("Cancel", role: .cancel) {}
        } message: { _ in
            Text("The team is removed from Mac MCP. Agents that are already running keep going.")
        }
    }

    private func create() {
        notice = nil
        original = nil
        draft = TeamPreset(id: TeamPreset.newPresetID(existing: settings.teamPresets), name: "", members: [])
    }

    private func edit(_ preset: TeamPreset) {
        notice = nil
        original = preset
        draft = preset
    }

    private func duplicate(_ preset: TeamPreset) {
        notice = nil
        original = nil
        draft = preset.duplicate(among: settings.teamPresets)
    }

    private func closeBuilder() {
        draft = nil
        original = nil
        if noticeIsError { notice = nil }
    }

    private func save() {
        guard let draft else { return }
        do {
            let stored = try settings.saveTeamPreset(draft)
            notice = "Saved “\(stored.name)”. Agents can start it by name."
            noticeIsError = false
            self.draft = nil
            original = nil
        } catch {
            notice = "Could not save the team: \(error.localizedDescription)"
            noticeIsError = true
        }
    }

    private func delete(_ preset: TeamPreset) {
        do {
            try settings.deleteTeamPreset(id: preset.id)
            notice = "Deleted “\(preset.name)”."
            noticeIsError = false
        } catch {
            notice = "Could not delete the team: \(error.localizedDescription)"
            noticeIsError = true
        }
        pendingDelete = nil
    }
}

// MARK: - Provider tokens

/// Each provider's colour and monogram, so a team reads at a glance like a squad of players.
enum ProviderTokenStyle {
    /// The badge colour (deep enough for white letters) and a lighter one for text on dark backgrounds.
    private static func hues(_ provider: String) -> (badge: UInt32, light: UInt32) {
        switch provider.lowercased() {
        case "codex": return (0x0E8A6B, 0x2BC79A)
        case "claude": return (0xC2603F, 0xE38B6B)
        case "opencode": return (0x4A55D6, 0x8D96FF)
        case "kimi": return (0xC23B6E, 0xF2729F)
        case "grok": return (0x3B4250, 0xA4ADBB)
        case "antigravity": return (0x1F6FD1, 0x6AAEFF)
        case "pi": return (0x2E8B4E, 0x5CCB82)
        case "cline": return (0x00808F, 0x3CC6D6)
        case "kilo": return (0xA77B00, 0xF0C23B)
        case "goose": return (0x9A4FC2, 0xC792EA)
        case "copilot": return (0x5B4BB5, 0xA79BFF)
        default: return (0x6B7280, 0x9CA3AF)
        }
    }

    /// The badge itself: the same in light and dark, like a printed token.
    static func badge(_ provider: String) -> Color {
        VisualTokens.color(hues(provider).badge)
    }

    /// The provider's colour for text, numbers and outlines, readable in light and dark.
    static func tint(_ provider: String) -> Color {
        let hue = hues(provider)
        return VisualTokens.adaptive(light: hue.badge, dark: hue.light)
    }

    static func monogram(_ provider: String) -> String {
        switch provider.lowercased() {
        case "codex": return "Cx"
        case "claude": return "Cl"
        case "opencode": return "Oc"
        case "kimi": return "Ki"
        case "grok": return "Gk"
        case "antigravity": return "Ag"
        case "pi": return "π"
        case "cline": return "Cn"
        case "kilo": return "Kl"
        case "goose": return "Gs"
        case "copilot": return "Co"
        default: return String(provider.prefix(2)).capitalized
        }
    }
}

/// A provider's token: its colour and monogram, with the slot number like a jersey number.
struct ProviderToken: View {
    let provider: String
    var size: CGFloat = 32
    var number: Int?
    var available = true

    var body: some View {
        let tint = ProviderTokenStyle.tint(provider)
        RoundedRectangle(cornerRadius: size * 0.3, style: .continuous)
            .fill(ProviderTokenStyle.badge(provider))
            .overlay(
                RoundedRectangle(cornerRadius: size * 0.3, style: .continuous)
                    .fill(LinearGradient(colors: [.white.opacity(0.26), .clear], startPoint: .top, endPoint: .center))
            )
            .overlay(
                RoundedRectangle(cornerRadius: size * 0.3, style: .continuous)
                    .strokeBorder(.white.opacity(0.22), lineWidth: 1)
            )
            .overlay(
                Text(ProviderTokenStyle.monogram(provider))
                    .font(.system(size: size * 0.4, weight: .heavy, design: .rounded))
                    .foregroundStyle(.white)
                    .shadow(color: .black.opacity(0.18), radius: 0.5, y: 0.5)
            )
            .frame(width: size, height: size)
            .saturation(available ? 1 : 0)
            .opacity(available ? 1 : 0.55)
            .overlay(alignment: .bottomTrailing) {
                if let number {
                    Text("\(number)")
                        .font(.system(size: max(9, size * 0.3), weight: .bold, design: .rounded).monospacedDigit())
                        .foregroundStyle(tint)
                        .frame(minWidth: size * 0.44, minHeight: size * 0.44)
                        .background(Circle().fill(Color(nsColor: .windowBackgroundColor)))
                        .overlay(Circle().strokeBorder(tint.opacity(0.7), lineWidth: 1.2))
                        .offset(x: size * 0.16, y: size * 0.16)
                }
            }
            .accessibilityHidden(true)
    }
}

// MARK: - Inventory

/// The saved teams as cards, with New Team; an invitation to build one when there are none.
struct TeamPresetInventory: View {
    let presets: [TeamPreset]
    let issue: String
    let notice: String?
    let noticeIsError: Bool
    let providers: [ProviderInfo]
    let catalogs: [String: ProviderCatalogInfo]
    let phase: AgentChoice.ProvidersPhase
    let locked: Bool
    let openProviders: () -> Void
    let create: () -> Void
    let edit: (TeamPreset) -> Void
    let duplicate: (TeamPreset) -> Void
    let delete: (TeamPreset) -> Void

    private var usable: [String] { AgentChoice.usableProviders(providers) }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline) {
                Text("Teams are named groups of agents you set up once. Ask for a team by name and Mac MCP starts every member with its own provider, model, thinking and role.")
                    .font(SettingsType.meta).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 12)
                if !presets.isEmpty {
                    Button(action: create) { Label("New Team", systemImage: "plus") }
                        .controlSize(.small)
                        .disabled(locked || presets.count >= 50)
                }
            }
            if let notice {
                Label(notice, systemImage: noticeIsError ? "exclamationmark.triangle.fill" : "checkmark.circle.fill")
                    .font(SettingsType.note)
                    .foregroundStyle(noticeIsError ? Color.orange : Color.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !issue.isEmpty {
                Label(issue, systemImage: "exclamationmark.triangle")
                    .font(SettingsType.note).foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if presets.isEmpty {
                emptyState
            } else {
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 220), spacing: 12)], spacing: 12) {
                    ForEach(presets) { preset in
                        TeamPresetCard(
                            preset: preset,
                            readiness: readiness(preset),
                            locked: locked,
                            edit: { edit(preset) },
                            duplicate: { duplicate(preset) },
                            delete: { delete(preset) }
                        )
                    }
                }
            }
        }
    }

    private var emptyState: some View {
        VStack(spacing: 14) {
            HStack(spacing: 10) {
                ForEach(0..<3, id: \.self) { index in
                    let filled = index < usable.count
                    ZStack {
                        if filled {
                            ProviderToken(provider: usable[index], size: 40, number: index + 1)
                        } else {
                            RoundedRectangle(cornerRadius: 12, style: .continuous)
                                .strokeBorder(style: StrokeStyle(lineWidth: 1.2, dash: [4, 3]))
                                .foregroundStyle(.tertiary)
                                .frame(width: 40, height: 40)
                                .overlay(
                                    Text("\(index + 1)")
                                        .font(.system(size: 15, weight: .semibold, design: .rounded))
                                        .foregroundStyle(.tertiary)
                                )
                        }
                    }
                    .rotationEffect(.degrees(Double(index - 1) * 6))
                    .offset(y: index == 1 ? -4 : 0)
                }
            }
            .padding(.bottom, 2)
            .accessibilityHidden(true)
            VStack(spacing: 4) {
                Text("Build your first team")
                    .font(.system(size: 15, weight: .semibold, design: .rounded))
                Text(usable.isEmpty
                     ? "Turn on at least one provider, then drag it onto a team and give each member a name, model and role."
                     : "Drag providers onto a team, then give each member a name, model, thinking and role.")
                    .font(SettingsType.note).foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: 360)
            }
            if usable.isEmpty {
                Button("Open Providers", action: openProviders)
            } else {
                Button(action: create) { Label("New Team", systemImage: "plus") }
                    .buttonStyle(.borderedProminent)
                    .disabled(locked)
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 28)
        .padding(.horizontal, 20)
        .background(
            RoundedRectangle(cornerRadius: VisualTokens.Radius.card, style: .continuous)
                .fill(Color.primary.opacity(0.025))
        )
        .overlay(
            RoundedRectangle(cornerRadius: VisualTokens.Radius.card, style: .continuous)
                .strokeBorder(style: StrokeStyle(lineWidth: 1, dash: [5, 4]))
                .foregroundStyle(Color.primary.opacity(0.18))
        )
    }

    private func readiness(_ preset: TeamPreset) -> TeamPresetReadiness {
        TeamPresetReadiness(preset: preset, providers: providers, catalogs: catalogs, phase: phase)
    }
}

/// How many members of a team can start now.
struct TeamPresetReadiness {
    let total: Int
    let ready: Int
    let loading: Bool

    init(preset: TeamPreset, providers: [ProviderInfo], catalogs: [String: ProviderCatalogInfo], phase: AgentChoice.ProvidersPhase) {
        let statuses = preset.members.map {
            AgentChoice.status(provider: $0.provider, model: $0.model, reasoning: $0.reasoning, providers: providers,
                               catalogs: catalogs, phase: phase, subject: "this member")
        }
        total = statuses.count
        ready = statuses.filter(\.isReady).count
        loading = statuses.contains { $0.label == "Loading" }
    }

    var label: String {
        if loading { return "Checking providers" }
        if ready == total { return "Ready" }
        let waiting = total - ready
        return "\(waiting) of \(total) need\(waiting == 1 ? "s" : "") attention"
    }

    var symbol: String {
        loading ? "arrow.clockwise" : (ready == total ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
    }

    var color: Color {
        loading ? .secondary : (ready == total ? VisualTokens.success : VisualTokens.warning)
    }
}

private struct TeamPresetCard: View {
    let preset: TeamPreset
    let readiness: TeamPresetReadiness
    let locked: Bool
    let edit: () -> Void
    let duplicate: () -> Void
    let delete: () -> Void

    @MacMCPState private var hovering = false

    var body: some View {
        Button(action: edit) {
            VStack(alignment: .leading, spacing: 12) {
                tokens
                VStack(alignment: .leading, spacing: 3) {
                    Text(preset.name)
                        .font(.system(size: 15, weight: .semibold, design: .rounded))
                        .lineLimit(1)
                        .foregroundStyle(.primary)
                    Text(subtitle)
                        .font(SettingsType.meta).foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                Label(readiness.label, systemImage: readiness.symbol)
                    .font(SettingsType.meta.weight(.semibold))
                    .foregroundStyle(readiness.color)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(14)
            .background(
                RoundedRectangle(cornerRadius: VisualTokens.Radius.card, style: .continuous)
                    .fill(Color.primary.opacity(hovering ? 0.06 : 0.035))
            )
            .overlay(
                RoundedRectangle(cornerRadius: VisualTokens.Radius.card, style: .continuous)
                    .strokeBorder(Color.primary.opacity(hovering ? 0.16 : 0.09))
            )
            .contentShape(RoundedRectangle(cornerRadius: VisualTokens.Radius.card, style: .continuous))
        }
        .buttonStyle(.plain)
        .disabled(locked)
        .overlay(alignment: .topTrailing) {
            Menu {
                Button("Edit", action: edit)
                Button("Duplicate", action: duplicate)
                Divider()
                Button("Delete…", role: .destructive, action: delete)
            } label: {
                Image(systemName: "ellipsis.circle")
                    .font(.system(size: 14))
                    .foregroundStyle(.secondary)
            }
            .menuStyle(.borderlessButton)
            .menuIndicator(.hidden)
            .fixedSize()
            .padding(10)
            .disabled(locked)
            .accessibilityLabel("More for \(preset.name)")
        }
        .onHover { inside in withAnimation(AppMotion.micro) { hovering = inside } }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(preset.name), \(subtitle), \(readiness.label)")
        .accessibilityHint("Opens the team to edit")
    }

    private var tokens: some View {
        HStack(spacing: 4) {
            ForEach(preset.members.prefix(6)) { member in
                ProviderToken(provider: member.provider, size: 26)
            }
            if preset.members.count > 6 {
                Text("+\(preset.members.count - 6)")
                    .font(.system(size: 11, weight: .bold, design: .rounded))
                    .foregroundStyle(.secondary)
                    .frame(width: 26, height: 26)
                    .background(RoundedRectangle(cornerRadius: 7.8, style: .continuous).fill(Color.primary.opacity(0.08)))
            }
        }
        .accessibilityHidden(true)
    }

    private var subtitle: String {
        let count = preset.members.count
        var text = "\(count) member\(count == 1 ? "" : "s")"
        if let updated = preset.updatedAt {
            text += " · Edited " + updated.formatted(date: .abbreviated, time: .omitted)
        }
        return text
    }
}

// MARK: - Builder

/// Building or editing one team: the name, the provider bench and ten numbered slots.
struct TeamPresetBuilder: View {
    @Binding var preset: TeamPreset
    let original: TeamPreset?
    let others: [TeamPreset]
    let providers: [ProviderInfo]
    let catalogs: [String: ProviderCatalogInfo]
    let phase: AgentChoice.ProvidersPhase
    let locked: Bool
    let problem: String?
    let openProviders: () -> Void
    let close: () -> Void
    let save: () -> Void

    @MacMCPState private var targetedSlot: Int?
    @MacMCPState private var benchTargeted = false
    @MacMCPState private var confirmDiscard = false

    static let providerPayload = "mac-mcp-provider:"
    static let memberPayload = "mac-mcp-member:"

    private var issues: TeamPreset.Issues { preset.issues(among: others) }
    private var usable: [String] { AgentChoice.usableProviders(providers) }
    private var changed: Bool { original.map { $0 != preset } ?? true }
    private var canSave: Bool { !locked && issues.isEmpty && changed }

    var body: some View {
        let issues = issues
        VStack(alignment: .leading, spacing: 18) {
            header
            nameField(issues)
            bench
            board(issues)
        }
        .confirmationDialog("Discard your changes to this team?", isPresented: $confirmDiscard) {
            Button("Discard Changes", role: .destructive, action: close)
            Button("Keep Editing", role: .cancel) {}
        }
    }

    // MARK: Header and name

    private var header: some View {
        HStack(spacing: 10) {
            Button(action: leave) {
                Label("Teams", systemImage: "chevron.left")
            }
            .buttonStyle(.borderless)
            .help("Back to your saved teams")
            Spacer()
            if let problem {
                Label(problem, systemImage: "exclamationmark.triangle.fill")
                    .font(SettingsType.meta).foregroundStyle(.orange)
                    .lineLimit(2)
            }
            Button("Cancel", action: leave)
                .keyboardShortcut(.cancelAction)
            Button(original == nil ? "Save Team" : "Save Changes", action: save)
                .keyboardShortcut(.defaultAction)
                .disabled(!canSave)
                .help(canSave ? "Save this team" : saveHint)
        }
    }

    private var saveHint: String {
        if locked { return "settings.json must be repaired before teams can be saved." }
        if !issues.isEmpty { return "Fix the marked fields to save." }
        return "No changes to save."
    }

    private func nameField(_ issues: TeamPreset.Issues) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(original == nil ? "New team" : "Edit team")
                .font(SettingsType.meta).foregroundStyle(.secondary)
            HStack(alignment: .firstTextBaseline, spacing: 12) {
                TextField("Team name", text: $preset.name, prompt: Text("Name your team"))
                    .textFieldStyle(.plain)
                    .font(.system(size: 22, weight: .semibold, design: .rounded))
                    .accessibilityLabel("Team name")
                Text("\(preset.members.count) of \(TeamPreset.maxMembers)")
                    .font(.system(size: 11, weight: .semibold, design: .rounded).monospacedDigit())
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 8).padding(.vertical, 3)
                    .background(Capsule().fill(Color.primary.opacity(0.07)))
                    .accessibilityLabel("\(preset.members.count) of \(TeamPreset.maxMembers) members")
            }
            Rectangle()
                .fill(issues.name == nil ? Color.primary.opacity(0.14) : VisualTokens.error)
                .frame(height: 1)
            if let message = issues.name {
                Text(message).font(SettingsType.meta).foregroundStyle(VisualTokens.error)
            }
        }
    }

    // MARK: Bench

    private var bench: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline) {
                Text("Bench").font(SettingsType.title)
                Text(usable.isEmpty ? "" : "Drag a provider into a slot, or click it to fill the next open one.")
                    .font(SettingsType.meta).foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            Group {
                if usable.isEmpty {
                    HStack(spacing: 10) {
                        Image(systemName: "person.crop.circle.badge.questionmark")
                            .foregroundStyle(.secondary)
                        Text(phase == .loading
                             ? "Checking which providers are on…"
                             : "No provider is on and installed. Turn one on in Providers to add it to a team.")
                            .font(SettingsType.note).foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                        Spacer(minLength: 8)
                        if phase != .loading {
                            Button("Open Providers", action: openProviders).controlSize(.small)
                        }
                    }
                } else {
                    PromptFlow(spacing: 8) {
                        ForEach(usable, id: \.self) { provider in
                            BenchChip(provider: provider, full: preset.members.count >= TeamPreset.maxMembers) {
                                add(provider, at: nil)
                            }
                        }
                    }
                }
            }
            .padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .fill(benchTargeted ? VisualTokens.error.opacity(0.08) : Color.primary.opacity(0.035))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .strokeBorder(benchTargeted ? VisualTokens.error.opacity(0.6) : Color.primary.opacity(0.09))
            )
            .overlay(alignment: .trailing) {
                if benchTargeted {
                    Label("Release to take off the team", systemImage: "arrow.uturn.backward")
                        .font(SettingsType.meta.weight(.semibold))
                        .foregroundStyle(VisualTokens.error)
                        .padding(.trailing, 12)
                }
            }
            // A member dragged back onto the bench leaves the team.
            .dropDestination(for: String.self) { items, _ in
                guard let id = memberID(items.first) else { return false }
                withAnimation(AppMotion.spatial) { preset.removeMember(id: id) }
                return true
            } isTargeted: { benchTargeted = $0 && !preset.members.isEmpty }
        }
    }

    // MARK: Board

    private func board(_ issues: TeamPreset.Issues) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline) {
                Text("Line-up").font(SettingsType.title)
                Text("Slot order is the order tasks are handed out. Drag a member's badge to move it.")
                    .font(SettingsType.meta).foregroundStyle(.secondary)
                    .lineLimit(1)
                Spacer(minLength: 8)
                Menu {
                    ForEach(usable, id: \.self) { provider in
                        Button(AgentChoice.providerName(provider)) { add(provider, at: nil) }
                    }
                } label: {
                    Label("Add Member", systemImage: "plus")
                }
                .menuStyle(.borderlessButton)
                .fixedSize()
                .disabled(usable.isEmpty || preset.members.count >= TeamPreset.maxMembers)
            }
            if let message = issues.members {
                // An empty board is where every team starts: guidance, not an error.
                Label(message, systemImage: preset.members.isEmpty ? "arrow.down.circle" : "exclamationmark.circle")
                    .font(SettingsType.meta)
                    .foregroundStyle(preset.members.isEmpty ? Color.secondary : VisualTokens.warning)
            }
            Grid(horizontalSpacing: 12, verticalSpacing: 12) {
                ForEach(0..<(TeamPreset.maxMembers / 2), id: \.self) { row in
                    GridRow {
                        slot(row * 2, issues)
                        slot(row * 2 + 1, issues)
                    }
                }
            }
        }
    }

    @ViewBuilder
    private func slot(_ index: Int, _ issues: TeamPreset.Issues) -> some View {
        let targeted = targetedSlot == index
        Group {
            if index < preset.members.count {
                let member = preset.members[index]
                TeamMemberCard(
                    member: memberBinding(member.id),
                    number: index + 1,
                    isFirst: index == 0,
                    isLast: index == preset.members.count - 1,
                    nameIssue: issues.memberNames[member.id],
                    providers: providers,
                    catalogs: catalogs,
                    phase: phase,
                    targeted: targeted,
                    move: { offset in
                        withAnimation(AppMotion.spatial) { _ = preset.moveMember(id: member.id, to: index + offset) }
                    },
                    remove: { withAnimation(AppMotion.spatial) { preset.removeMember(id: member.id) } }
                )
            } else {
                EmptySlot(number: index + 1, isNext: index == preset.members.count, targeted: targeted)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .dropDestination(for: String.self) { items, _ in
            drop(items.first, at: index)
        } isTargeted: { inside in
            if inside { targetedSlot = index } else if targetedSlot == index { targetedSlot = nil }
        }
    }

    private func memberBinding(_ id: String) -> Binding<TeamPresetMember> {
        Binding(
            get: { preset.members.first { $0.id == id } ?? TeamPresetMember(id: id, name: "", provider: "") },
            set: { value in
                guard let index = preset.members.firstIndex(where: { $0.id == id }) else { return }
                preset.members[index] = value
            }
        )
    }

    // MARK: Actions

    private func add(_ provider: String, at index: Int?) {
        guard AgentChoice.isUsable(provider, in: providers) else { return }
        withAnimation(AppMotion.spatial) { _ = preset.addMember(provider: provider, at: index) }
    }

    private func drop(_ payload: String?, at index: Int) -> Bool {
        targetedSlot = nil
        guard let payload else { return false }
        if payload.hasPrefix(Self.providerPayload) {
            let provider = String(payload.dropFirst(Self.providerPayload.count))
            // Fail closed: only a provider that is on and installed joins a team.
            guard AgentChoice.isUsable(provider, in: providers),
                  preset.members.count < TeamPreset.maxMembers else { return false }
            withAnimation(AppMotion.spatial) { _ = preset.addMember(provider: provider, at: index) }
            return true
        }
        if let id = memberID(payload) {
            withAnimation(AppMotion.spatial) { _ = preset.moveMember(id: id, to: index) }
            return true
        }
        return false
    }

    private func memberID(_ payload: String?) -> String? {
        guard let payload, payload.hasPrefix(Self.memberPayload) else { return nil }
        let id = String(payload.dropFirst(Self.memberPayload.count))
        return preset.members.contains { $0.id == id } ? id : nil
    }

    private func leave() {
        if changed && !(original == nil && preset.members.isEmpty && TeamPreset.trimmed(preset.name).isEmpty) {
            confirmDiscard = true
        } else {
            close()
        }
    }
}

/// A provider waiting on the bench: drag it into a slot, or click it to fill the next open one.
private struct BenchChip: View {
    let provider: String
    let full: Bool
    let add: () -> Void

    @MacMCPState private var hovering = false

    var body: some View {
        HStack(spacing: 7) {
            ProviderToken(provider: provider, size: 22)
            Text(AgentChoice.providerName(provider))
                .font(.system(size: 12, weight: .medium))
        }
        .padding(.leading, 5).padding(.trailing, 11).padding(.vertical, 5)
        .background(Capsule().fill(Color(nsColor: .controlBackgroundColor)))
        .overlay(Capsule().strokeBorder(ProviderTokenStyle.tint(provider).opacity(hovering ? 0.7 : 0.25)))
        .shadow(color: .black.opacity(hovering ? 0.12 : 0.05), radius: hovering ? 3 : 1, y: 1)
        .opacity(full ? 0.5 : 1)
        .contentShape(Capsule())
        .onHover { inside in withAnimation(AppMotion.micro) { hovering = inside } }
        .onTapGesture { if !full { add() } }
        .draggable(TeamPresetBuilder.providerPayload + provider) {
            ProviderToken(provider: provider, size: 36)
        }
        .help(full ? "The team is full (10 members)." : "Drag into a slot, or click to add \(AgentChoice.providerName(provider)).")
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(AgentChoice.providerName(provider))
        .accessibilityAddTraits(.isButton)
        .accessibilityHint("Adds a member that uses \(AgentChoice.providerName(provider))")
        .accessibilityAction { if !full { add() } }
    }
}

/// An open, numbered slot that takes a provider from the bench or a member being moved.
private struct EmptySlot: View {
    let number: Int
    let isNext: Bool
    let targeted: Bool

    var body: some View {
        HStack(spacing: 12) {
            Text("\(number)")
                .font(.system(size: 20, weight: .bold, design: .rounded).monospacedDigit())
                .foregroundStyle(targeted ? VisualTokens.accent : Color.primary.opacity(isNext ? 0.35 : 0.2))
                .frame(width: 30)
            Text(targeted ? "Release to place here" : (isNext ? "Drop a provider here" : "Open slot"))
                .font(SettingsType.note)
                .foregroundStyle(targeted ? VisualTokens.accent : (isNext ? Color.secondary : Color.secondary.opacity(0.7)))
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 14)
        .frame(maxWidth: .infinity, minHeight: 56, maxHeight: .infinity)
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(targeted ? VisualTokens.accent.opacity(0.10) : Color.primary.opacity(isNext ? 0.03 : 0.015))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .strokeBorder(style: StrokeStyle(lineWidth: targeted ? 1.6 : 1, dash: targeted ? [] : [5, 4]))
                .foregroundStyle(targeted ? VisualTokens.accent : Color.primary.opacity(isNext ? 0.25 : 0.14))
        )
        .scaleEffect(targeted && !AppMotion.reduceMotion ? 1.015 : 1)
        .animation(AppMotion.micro, value: targeted)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Open slot \(number)")
    }
}

/// One member on the board: its badge (drag to move), name, provider, model, thinking, role and status.
private struct TeamMemberCard: View {
    @Binding var member: TeamPresetMember
    let number: Int
    let isFirst: Bool
    let isLast: Bool
    let nameIssue: String?
    let providers: [ProviderInfo]
    let catalogs: [String: ProviderCatalogInfo]
    let phase: AgentChoice.ProvidersPhase
    let targeted: Bool
    let move: (Int) -> Void
    let remove: () -> Void

    private var catalog: ProviderCatalogInfo? { catalogs[member.provider] }
    private var modelOptions: [AgentModelInfo] { catalog?.modelItems ?? [] }
    private var reasoningOptions: [String] { AgentChoice.reasoningOptions(member.model, in: catalog) }
    private var status: AgentChoice.Status {
        AgentChoice.status(provider: member.provider, model: member.model, reasoning: member.reasoning,
                           providers: providers, catalogs: catalogs, phase: phase, subject: "this member")
    }

    var body: some View {
        let tint = ProviderTokenStyle.tint(member.provider)
        let status = status
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top, spacing: 12) {
                ProviderToken(provider: member.provider, size: 38, number: number,
                              available: AgentChoice.isUsable(member.provider, in: providers))
                    .padding(.top, 1)
                    .draggable(TeamPresetBuilder.memberPayload + member.id) {
                        ProviderToken(provider: member.provider, size: 38, number: number)
                    }
                    .help("Drag to another slot to move \(member.name), or onto the bench to take it off the team")
                VStack(alignment: .leading, spacing: 2) {
                    TextField("Member name", text: $member.name, prompt: Text("Name"))
                        .textFieldStyle(.plain)
                        .font(.system(size: 14, weight: .semibold, design: .rounded))
                        .accessibilityLabel("Name of member \(number)")
                    Text(nameIssue ?? subtitle)
                        .font(SettingsType.meta)
                        .foregroundStyle(nameIssue == nil ? Color.secondary : VisualTokens.error)
                        .lineLimit(2)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
                Menu {
                    Button("Move Up") { move(-1) }.disabled(isFirst)
                    Button("Move Down") { move(1) }.disabled(isLast)
                    Divider()
                    Button("Remove from Team", role: .destructive, action: remove)
                } label: {
                    Image(systemName: "ellipsis")
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(.secondary)
                        .frame(width: 20, height: 20)
                }
                .menuStyle(.borderlessButton)
                .menuIndicator(.hidden)
                .fixedSize()
                .accessibilityLabel("More for \(member.name)")
            }

            VStack(spacing: 6) {
                row("Provider") {
                    Picker("Provider", selection: providerBinding) {
                        ForEach(AgentChoice.providerOptions(providers, keeping: member.provider), id: \.self) { id in
                            Text(AgentChoice.isUsable(id, in: providers)
                                 ? AgentChoice.providerName(id)
                                 : "\(AgentChoice.providerName(id)) · Unavailable").tag(id)
                        }
                    }
                }
                row("Model") {
                    Picker("Model", selection: modelBinding) {
                        Text("Provider default").tag("")
                        if !member.model.isEmpty, !modelOptions.contains(where: { $0.id == member.model }) {
                            Text("\(member.model) · Unavailable").tag(member.model)
                        }
                        ForEach(modelOptions) { item in
                            Text(item.displayName).tag(item.id)
                        }
                    }
                    .disabled(catalog == nil)
                }
                row("Thinking") {
                    Picker("Thinking", selection: $member.reasoning) {
                        Text("Provider default").tag("")
                        if !member.reasoning.isEmpty, !reasoningOptions.contains(member.reasoning) {
                            Text("\(AgentChoice.reasoningDisplayName(member.reasoning)) · Unsupported").tag(member.reasoning)
                        }
                        ForEach(reasoningOptions, id: \.self) { value in
                            Text(AgentChoice.reasoningDisplayName(value)).tag(value)
                        }
                    }
                    .disabled(reasoningOptions.isEmpty && member.reasoning.isEmpty)
                }
                row("Role") {
                    Picker("Role", selection: $member.role) {
                        Text("None").tag(TeamPresetRole?.none)
                        ForEach(TeamPresetRole.allCases) { role in
                            Text(role.title).tag(TeamPresetRole?.some(role))
                        }
                    }
                }
            }

            Label(status.label, systemImage: status.symbol)
                .font(SettingsType.meta.weight(.semibold))
                .foregroundStyle(status.isReady ? VisualTokens.success : status.color)
                .help(status.detail)
                .accessibilityLabel("Status: \(status.label). \(status.detail)")
            if !status.isReady {
                Text(status.detail)
                    .font(SettingsType.meta).foregroundStyle(.secondary)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(Color(nsColor: .controlBackgroundColor).opacity(0.7))
        )
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(LinearGradient(colors: [tint.opacity(0.16), tint.opacity(0.02)], startPoint: .topLeading, endPoint: .bottomTrailing))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .strokeBorder(borderColor(tint), lineWidth: targeted || nameIssue != nil ? 1.5 : 1)
        )
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Slot \(number), \(member.name), \(AgentChoice.providerName(member.provider))")
        .accessibilityAction(named: "Move Up") { if !isFirst { move(-1) } }
        .accessibilityAction(named: "Move Down") { if !isLast { move(1) } }
        .accessibilityAction(named: "Remove from Team", remove)
    }

    private func borderColor(_ tint: Color) -> Color {
        if targeted { return VisualTokens.accent }
        if nameIssue != nil { return VisualTokens.error.opacity(0.8) }
        return tint.opacity(0.3)
    }

    private var subtitle: String {
        var parts = [AgentChoice.providerName(member.provider)]
        if let role = member.role { parts.append(role.title) }
        return parts.joined(separator: " · ")
    }

    private func row<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        HStack(spacing: 8) {
            Text(title)
                .font(SettingsType.note)
                .foregroundStyle(.secondary)
                .frame(width: 58, alignment: .leading)
            content()
                .labelsHidden()
                .fixedSize()  // natural width, left edges aligned (a wider frame only centers the popup)
            Spacer(minLength: 0)
        }
    }

    /// A new provider starts on its own defaults: its model and thinking are cleared.
    private var providerBinding: Binding<String> {
        Binding(
            get: { member.provider },
            set: { value in
                guard value != member.provider else { return }
                member.provider = value
                member.model = ""
                member.reasoning = ""
            }
        )
    }

    /// Thinking the new model does not offer goes back to the provider's default.
    private var modelBinding: Binding<String> {
        Binding(
            get: { member.model },
            set: { value in
                member.model = value
                if !member.reasoning.isEmpty,
                   !AgentChoice.reasoningOptions(value, in: catalog).contains(member.reasoning) {
                    member.reasoning = ""
                }
            }
        )
    }
}
