import AppKit
import SwiftUI

/// Settings › Devices › iPhone: the owner's iPhone through iPhone Mirroring. Whether it is open, and how far agents
/// may go on it: off, read only (the default) or full access, never more than this Mac's own permission setting.

// MARK: - A section that folds away

struct DeviceSection<Controls: View, Content: View>: View {
    let title: String
    let symbol: String
    let summary: String
    @Binding var isOpen: Bool
    @ViewBuilder var controls: () -> Controls
    @ViewBuilder var content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 10) {
                Button {
                    withAnimation(.easeInOut(duration: 0.2)) { isOpen.toggle() }
                } label: {
                    HStack(spacing: 10) {
                        Image(systemName: "chevron.right")
                            .font(.caption.weight(.bold)).foregroundStyle(.secondary)
                            .rotationEffect(.degrees(isOpen ? 90 : 0))
                        Image(systemName: symbol).font(.title3).foregroundStyle(Color.accentColor).frame(width: 26)
                        VStack(alignment: .leading, spacing: 1) {
                            Text(title).font(SettingsType.title)
                            Text(summary).font(SettingsType.note).foregroundStyle(.secondary)
                        }
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("\(title), \(summary)")
                .accessibilityValue(isOpen ? "expanded" : "collapsed")
                .accessibilityHint(isOpen ? "Hides this section" : "Shows this section")
                Spacer(minLength: 12)
                controls()
            }
            if isOpen {
                content().transition(.opacity)
            }
        }
        .padding(.top, 4)
    }
}

// MARK: - Access

enum IPhoneAccess {
    static let options = ["full", "read_only", "off"]

    static func title(_ access: String) -> String {
        switch access {
        case "full": return "Full access"
        case "off": return "Off"
        default: return "Read only"
        }
    }

    static func blurb(_ access: String) -> String {
        switch access {
        case "full": return "See the screen, and tap, type, scroll and open apps for you."
        case "off": return "Agents can’t see or use your iPhone."
        default: return "See the screen and read its text. Changes nothing."
        }
    }

    static func symbol(_ access: String) -> String {
        switch access {
        case "full": return "hand.tap.fill"
        case "off": return "nosign"
        default: return "eye"
        }
    }
}

extension AutomationExplainer {
    static func iphone(profile: String) -> AutomationExplainer {
        AutomationExplainer(
            icon: AnyView(Image(systemName: "iphone").font(.title2).foregroundStyle(Color.accentColor)),
            title: "How agents use your iPhone",
            points: [
                Point(symbol: "rectangle.on.rectangle", title: "Through iPhone Mirroring",
                      text: "Your iPhone stays locked and nearby, signed in to the same Apple Account. Agents see what iPhone Mirroring shows on this Mac."),
                Point(symbol: "slider.horizontal.3", title: "You choose how far",
                      text: "Read only lets agents look and read. Full access also lets them tap, type and open apps. Off keeps them away."),
                Point(symbol: "checkmark.shield", title: "This Mac’s own setting comes first",
                      text: "Full access on a Mac set to Read Only only reads. This Mac is set to \(MacAccess.profileTitle(profile))."),
                Point(symbol: "cursorarrow.motionlines", title: "A moment in front",
                      text: "To tap or type, iPhone Mirroring comes forward for a moment, then your pointer and your app go back."),
                Point(symbol: "hand.raised", title: "Privacy",
                      text: "Pausing in Privacy stops this too. Add iPhone Mirroring to the excluded apps there to keep agents off it entirely."),
            ],
            footer: "Using your iPhone ends the mirroring. Lock it again and choose Connect to continue."
        )
    }
}

// MARK: - The section

struct IPhoneSection: View {
    @ObservedObject var state: AppState
    let profile: String
    @Binding var isOpen: Bool

    @MacMCPState private var mirroring: [String: Any] = [:]
    @MacMCPState private var granted = "read_only"
    @MacMCPState private var effective = "read_only"
    @MacMCPState private var limited = false
    @MacMCPState private var loaded = false
    @MacMCPState private var problem = ""
    @MacMCPState private var working = false
    @MacMCPState private var showInfo = false

    private var isOpenOnMac: Bool { mirroring["open"] as? Bool ?? false }
    private var connected: Bool { isOpenOnMac && (mirroring["connected"] as? Bool ?? false) }
    private var phoneName: String { mirroring["name"] as? String ?? "iPhone" }

    /// Connected, connecting, in use (the owner is using it) or not open.
    private var phase: (title: String, detail: String, color: Color) {
        if connected { return ("Connected", "Agents reach it through iPhone Mirroring.", .green) }
        if isOpenOnMac {
            if mirroring["state"] as? String == "connecting" { return ("Connecting…", "iPhone Mirroring is reaching your iPhone.", .blue) }
            return ("In use on your iPhone", "Lock your iPhone, then choose Connect in iPhone Mirroring.", .orange)
        }
        return ("Not open", "Open iPhone Mirroring to let agents reach it.", .gray)
    }

    var body: some View {
        DeviceSection(title: "iPhone", symbol: "iphone", summary: summary, isOpen: $isOpen) {
            HStack(spacing: 10) {
                Button { showInfo.toggle() } label: { Image(systemName: "info.circle") }
                    .buttonStyle(.borderless)
                    .help("How do agents use my iPhone?")
                    .accessibilityLabel("How do agents use my iPhone?")
                    .popover(isPresented: $showInfo, arrowEdge: .bottom) { AutomationExplainer.iphone(profile: profile) }
                phasePill
            }
        } content: {
            HStack(alignment: .top, spacing: 16) {
                phoneCard
                VStack(alignment: .leading, spacing: 10) {
                    Text("What may agents do on your iPhone?").font(SettingsType.title)
                    // #125: the choices in one list box with hairlines, as in the Usage tabs.
                    VStack(spacing: 0) {
                        ForEach(Array(IPhoneAccess.options.enumerated()), id: \.element) { index, option in
                            if index > 0 { Divider().padding(.leading, 54) }
                            accessTile(option)
                        }
                    }
                    .usageListBox()
                    if limited {
                        Label("This Mac is set to \(MacAccess.profileTitle(profile)) in Permissions & Safety, so agents only read your iPhone. Change that setting to let them tap and type.",
                              systemImage: "checkmark.shield")
                            .font(SettingsType.note).foregroundStyle(.orange)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if !problem.isEmpty {
                        Label(problem, systemImage: "exclamationmark.triangle").font(SettingsType.note).foregroundStyle(.orange)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .task {
            while !Task.isCancelled {
                await load()
                try? await Task.sleep(nanoseconds: 3_000_000_000)
            }
        }
    }

    private var summary: String {
        guard loaded else { return "Through iPhone Mirroring" }
        return (mirroring["name"] as? String ?? "Through iPhone Mirroring") + " · " + IPhoneAccess.title(limited ? effective : granted)
    }

    private var phasePill: some View {
        HStack(spacing: 5) {
            Circle().fill(phase.color).frame(width: 7, height: 7)
            Text(connected ? "Connected" : isOpenOnMac ? (phase.color == .blue ? "Connecting" : "In use") : "Not open")
                .font(.caption.weight(.semibold))
        }
        .padding(.horizontal, 8).padding(.vertical, 3)
        .background(Capsule().fill(phase.color.opacity(0.14)))
        .foregroundStyle(phase.color)
        .accessibilityElement(children: .combine)
    }

    private var phoneCard: some View {
        VStack(spacing: 12) {
            ZStack {
                RoundedRectangle(cornerRadius: 22, style: .continuous)
                    .fill(LinearGradient(colors: [Color.accentColor.opacity(connected ? 0.22 : 0.08), Color.accentColor.opacity(0.03)],
                                         startPoint: .top, endPoint: .bottom))
                    .frame(width: 92, height: 150)
                Image(systemName: "iphone")
                    .font(.system(size: 66, weight: .ultraLight))
                    .foregroundStyle(.primary.opacity(connected ? 0.85 : 0.4))
            }
            .accessibilityHidden(true)
            VStack(spacing: 3) {
                Text(phoneName).font(SettingsType.title)
                StatusDot(text: phase.title, color: phase.color)
                Text(phase.detail)
                    .font(SettingsType.note).foregroundStyle(.secondary).multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !isOpenOnMac {
                Button("Open iPhone Mirroring") { Task { await openMirroring() } }
                    .disabled(working)
            }
        }
        .frame(width: 190)
        .padding(.horizontal, 14).padding(.vertical, 12)
        .usageListBox()
    }

    private func accessTile(_ option: String) -> some View {
        let selected = granted == option
        return Button {
            Task { await choose(option) }
        } label: {
            HStack(spacing: 12) {
                Image(systemName: IPhoneAccess.symbol(option))
                    .font(.system(size: 14, weight: .medium))
                    .foregroundStyle(selected ? Color.accentColor : Color.secondary)
                    .frame(width: 28, height: 28)
                    .background(Circle().fill(selected ? Color.accentColor.opacity(0.14) : Color.primary.opacity(0.05)))
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 6) {
                        Text(IPhoneAccess.title(option)).font(SettingsType.title)
                        if option == "read_only" { SettingsBadge(text: "Default") }
                    }
                    Text(IPhoneAccess.blurb(option)).font(SettingsType.note).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer()
                Image(systemName: selected ? "checkmark.circle.fill" : "circle")
                    .font(.system(size: 17))
                    .foregroundStyle(selected ? Color.accentColor : Color.secondary.opacity(0.5))
            }
            .padding(.horizontal, 14).padding(.vertical, 9)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(working)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    // MARK: Server

    private func apply(_ object: [String: Any]) {
        mirroring = object["mirroring"] as? [String: Any] ?? [:]
        let access = object["access"] as? [String: Any] ?? [:]
        granted = access["granted"] as? String ?? "read_only"
        effective = access["effective"] as? String ?? granted
        limited = access["limited_by_this_mac"] as? Bool ?? false
        loaded = true
    }

    private func load() async {
        let result = await state.taskAPI("dashboard/api/iphone")
        if result.ok { apply(result.object) }
    }

    private func choose(_ option: String) async {
        guard option != granted else { return }
        working = true
        let result = await state.taskAPI("dashboard/api/iphone/access", body: ["access": option])
        working = false
        if result.ok { apply(result.object); problem = "" } else {
            problem = result.object["message"] as? String ?? "Couldn’t change that. Try again."
        }
    }

    private func openMirroring() async {
        working = true
        let result = await state.taskAPI("dashboard/api/iphone/open", body: [:])
        working = false
        problem = result.ok ? "" : (result.object["message"] as? String ?? result.object["error"] as? String ?? "Couldn’t open iPhone Mirroring.")
        await load()
    }
}
