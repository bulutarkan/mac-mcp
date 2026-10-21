import AppKit
import SwiftUI

/// Settings › Devices (#147): the owner's Macs, side by side, and their iPhone (IPhoneSection.swift), each in a
/// section that folds away. Each Mac is a card with its picture, whether
/// it is online, busy or offline, and what this Mac may do there. A Mac is added with an invitation
/// from the other one (a link, or its address and a code), allowed there after a 6-digit check, and
/// can be stopped at any time. Busy keeps every pairing but lets no other Mac use this one.

// MARK: - Models

enum MacAccess {
    static func title(_ access: String) -> String {
        switch access {
        case "full": return "Full access"
        case "standard": return "Standard access"
        case "read_only": return "Read only"
        default: return "Status only"
        }
    }

    static func blurb(_ access: String) -> String {
        switch access {
        case "full": return "Run commands, change files, use its apps and browser."
        case "read_only": return "Read files, search, see its screen and pages. Changes nothing."
        default: return "See that it is on and which version it runs."
        }
    }

    static func symbol(_ access: String) -> String {
        switch access {
        case "full": return "bolt.fill"
        case "read_only": return "eye"
        default: return "dot.radiowaves.left.and.right"
        }
    }

    /// How long full access lasts once allowed (#80); "none" is no limit, the default.
    static let durations = ["none", "1h", "today", "7d"]

    static func durationTitle(_ duration: String) -> String {
        switch duration {
        case "1h": return "For 1 hour"
        case "today": return "Until the end of today"
        case "7d": return "For 7 days"
        default: return "No time limit"
        }
    }

    /// When full access ends, said the short way: "until 14:30" today, else with the date.
    static func until(_ time: Double) -> String {
        let date = Date(timeIntervalSince1970: time)
        return Calendar.current.isDateInToday(date)
            ? date.formatted(date: .omitted, time: .shortened)
            : date.formatted(date: .abbreviated, time: .shortened)
    }

    static func profileTitle(_ profile: String) -> String {
        switch profile {
        case "trusted": return "Trusted"
        case "read_only": return "Read Only"
        default: return "Standard"
        }
    }
}

struct FleetPeer: Identifiable, Equatable {
    let peerID: String
    let name: String
    let state: String
    let presence: String
    let granted: String
    let effective: String
    let limited: Bool
    let checkCode: String?
    let model: String
    let version: String?
    let macos: String?
    let lastOkAt: Double?
    var id: String { peerID }

    init(_ object: [String: Any]) {
        peerID = object["peer_id"] as? String ?? ""
        name = object["name"] as? String ?? "Mac"
        state = object["state"] as? String ?? ""
        presence = object["presence"] as? String ?? "offline"
        let access = object["access"] as? [String: Any] ?? [:]
        granted = access["granted"] as? String ?? object["permission"] as? String ?? "status"
        effective = access["effective"] as? String ?? granted
        limited = access["limited_by_this_mac"] as? Bool ?? false
        checkCode = object["check_code"] as? String
        model = object["model"] as? String ?? "Mac"
        let status = object["status"] as? [String: Any] ?? [:]
        version = status["version"] as? String
        macos = status["macos"] as? String
        lastOkAt = object["last_ok_at"] as? Double
    }

    var accessLine: String {
        if presence == "waiting" { return "Waiting for its owner to allow this Mac" }
        if presence == "ended" { return state == "refused" ? "Its owner didn’t allow this Mac" : "No longer connected" }
        if limited { return MacAccess.title(granted) + " · " + MacAccess.title(effective).lowercased() + " by its own settings" }
        return MacAccess.title(granted)
    }

    var detailLine: String {
        switch presence {
        case "waiting": return "It shows the code " + (checkCode ?? "") + " there too."
        case "busy": return "Set to Busy by its owner. It takes nothing now."
        case "offline":
            let since = lastOkAt.map { " since " + Date(timeIntervalSince1970: $0).formatted(date: .abbreviated, time: .shortened) } ?? ""
            return "Not answering" + since + ". It may be asleep or off."
        case "ended": return "Invite this Mac again from it to reconnect."
        default:
            return [model, macos.map { "macOS " + $0 }, version.map { "Mac MCP " + $0 }].compactMap { $0 }.joined(separator: " · ")
        }
    }
}

struct FleetClient: Identifiable, Equatable {
    let clientID: String
    let name: String
    let granted: String
    let effective: String
    let limited: Bool
    let lastSeenAt: Double?
    /// Full access ends then (#80); nil when it does not end.
    let fullUntil: Double?
    /// Full access was given for a while and that time is over: it reads only now.
    let fullExpired: Bool
    var id: String { clientID }

    init(_ object: [String: Any]) {
        clientID = object["client_id"] as? String ?? ""
        fullUntil = object["full_until"] as? Double
        fullExpired = object["full_expired"] as? Bool ?? false
        name = object["name"] as? String ?? "Mac"
        let access = object["access"] as? [String: Any] ?? [:]
        granted = access["granted"] as? String ?? object["permission"] as? String ?? "status"
        effective = access["effective"] as? String ?? granted
        limited = access["limited_by_this_mac"] as? Bool ?? false
        lastSeenAt = object["last_seen_at"] as? Double
    }
}

struct FleetRequest: Identifiable, Equatable {
    let requestID: String
    let name: String
    let permission: String
    let fullFor: String
    let checkCode: String
    var id: String { requestID }

    init(_ object: [String: Any]) {
        requestID = object["request_id"] as? String ?? ""
        name = object["client_name"] as? String ?? "A Mac"
        permission = object["permission"] as? String ?? "status"
        fullFor = object["full_for"] as? String ?? "none"
        checkCode = object["check_code"] as? String ?? ""
    }
}

struct FleetInvitation: Equatable {
    let link: String
    let code: String
    let address: String
    let expiresAt: Double
}

// MARK: - Pieces

struct MacPicture: View {
    let model: String
    var size: CGFloat = 46

    var body: some View {
        Image(systemName: Self.symbol(for: model))
            .font(.system(size: size * 0.62, weight: .light))
            .frame(width: size, height: size)
            .foregroundStyle(.primary.opacity(0.85))
            .accessibilityHidden(true)
    }

    static func symbol(for model: String) -> String {
        let name = model.lowercased()
        if name.contains("macbook") { return "laptopcomputer" }
        if name.contains("mac mini") { return "macmini" }
        if name.contains("mac studio") { return "macstudio" }
        if name.contains("mac pro") { return "macpro.gen3" }
        return "desktopcomputer"
    }
}

struct PresencePill: View {
    let presence: String

    var body: some View {
        HStack(spacing: 5) {
            Circle().fill(color).frame(width: 7, height: 7)
            Text(title).font(.caption.weight(.semibold))
        }
        .padding(.horizontal, 8).padding(.vertical, 3)
        .background(Capsule().fill(color.opacity(0.14)))
        .foregroundStyle(color)
        .accessibilityElement(children: .combine)
    }

    var title: String {
        switch presence {
        case "available": return "Available"
        case "online": return "Online"
        case "busy": return "Busy"
        case "waiting": return "Waiting"
        case "ended": return "Not connected"
        default: return "Offline"
        }
    }

    var color: Color {
        switch presence {
        case "online", "available": return .green
        case "busy": return .orange
        case "waiting": return .blue
        case "ended": return .red
        default: return .gray
        }
    }
}

extension AutomationExplainer {
    static func macs(profile: String) -> AutomationExplainer {
        AutomationExplainer(
            icon: AnyView(Image(systemName: "laptopcomputer.and.iphone").font(.title2).foregroundStyle(Color.accentColor)),
            title: "How your Macs work together",
            points: [
                Point(symbol: "link", title: "Invite, then allow",
                      text: "A Mac joins with an invitation from the other one. Its owner sees who asks and a 6-digit code, and allows it."),
                Point(symbol: "sparkles", title: "One AI app, every Mac",
                      text: "The AI app connected to this Mac can see your other Macs and, with access, use them: run a command there, read a file, open a page."),
                Point(symbol: "checkmark.shield", title: "Each Mac’s own setting comes first",
                      text: "A connected Mac never gets more than the other Mac allows in Permissions & Safety. Full access on a Read Only Mac only reads. This Mac is set to \(MacAccess.profileTitle(profile))."),
                Point(symbol: "moon.fill", title: "Busy",
                      text: "Turn Available off and no other Mac can use this one until you turn it back on. Nothing is removed."),
            ],
            footer: "Macs reach each other at their own addresses (Cloudflare, ngrok or your own HTTPS address). There is no server in between, and a Mac’s API key is never shared."
        )
    }
}

// MARK: - Pane

struct MacsPane: View {
    @ObservedObject var state: AppState

    @MacMCPState private var thisMac: [String: Any] = [:]
    @MacMCPState private var peers: [FleetPeer] = []
    @MacMCPState private var clients: [FleetClient] = []
    @MacMCPState private var requests: [FleetRequest] = []
    @MacMCPState private var loaded = false
    @MacMCPState private var message = ""
    @MacMCPState private var busy = false
    @MacMCPState private var showInfo = false
    @MacMCPState private var inviting = false
    @MacMCPState private var adding = false
    @MacMCPState private var renaming: String?
    @MacMCPState private var removingPeer: FleetPeer?
    @MacMCPState private var stoppingClient: FleetClient?
    @MacMCPState private var hovered: String?
    @AppStorage("MacMCP.devices.macsOpen") private var macsOpen = true
    @AppStorage("MacMCP.devices.iphoneOpen") private var iphoneOpen = true

    private var available: Bool { thisMac["available"] as? Bool ?? true }
    private var profile: String { thisMac["profile"] as? String ?? "standard" }
    private var thisName: String { thisMac["name"] as? String ?? "This Mac" }
    private var thisModel: String { thisMac["model"] as? String ?? "Mac" }
    private var thisURL: String { thisMac["url"] as? String ?? "" }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                header
                if !message.isEmpty {
                    Label(message, systemImage: "exclamationmark.triangle").font(.caption).foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                }
                ForEach(requests) { request in requestCard(request) }
                DeviceSection(title: "Macs", symbol: "laptopcomputer", summary: macsSummary, isOpen: $macsOpen) {
                    macsControls
                } content: {
                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 210, maximum: 320), spacing: 12)], spacing: 12) {
                        thisMacCard
                        ForEach(peers) { peer in peerCard(peer) }
                        addCard
                    }
                    if !clients.isEmpty { whoCanUse }
                }
                IPhoneSection(state: state, profile: profile, isOpen: $iphoneOpen)
                Spacer(minLength: 0)
            }
            .padding(22)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .task {
            while !Task.isCancelled {
                await load()
                try? await Task.sleep(nanoseconds: 2_000_000_000)
            }
        }
        .sheet(isPresented: $inviting) {
            InviteMacSheet(state: state, profile: profile, address: thisURL) { inviting = false }
        }
        .sheet(isPresented: $adding) {
            AddMacSheet(state: state) { adding = false; Task { await load() } }
        }
        .confirmationDialog("Remove \(removingPeer?.name ?? "this Mac")?", isPresented: Binding(
            get: { removingPeer != nil }, set: { if !$0 { removingPeer = nil } })) {
            Button("Remove", role: .destructive) {
                if let peer = removingPeer { Task { await act("dashboard/api/fleet/peers/remove", ["peer_id": peer.peerID]) } }
                removingPeer = nil
            }
        } message: {
            Text("This Mac stops using it and asks it to forget this Mac too. You can connect them again later.")
        }
        .confirmationDialog("Stop \(stoppingClient?.name ?? "that Mac") from using this Mac?", isPresented: Binding(
            get: { stoppingClient != nil }, set: { if !$0 { stoppingClient = nil } })) {
            Button("Stop", role: .destructive) {
                if let client = stoppingClient { Task { await act("dashboard/api/fleet/clients/revoke", ["client_id": client.clientID]) } }
                stoppingClient = nil
            }
        } message: {
            Text("It can’t use this Mac from now on. You can invite it again later.")
        }
    }

    // MARK: Header

    private var header: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Devices").font(.title2.weight(.semibold))
            Text("Use your Macs and your iPhone from the AI app connected to this Mac.")
                .font(.callout).foregroundStyle(.secondary)
        }
    }

    private var macsSummary: String {
        // #19: before the first answer nothing is known yet; after a failed one, say so.
        guard loaded else { return message.isEmpty ? "Checking…" : "Couldn’t load your Macs" }
        return peers.isEmpty ? "Only this Mac yet" : "This Mac and \(peers.count) more"
    }

    private var macsControls: some View {
        HStack(spacing: 10) {
            Button { showInfo.toggle() } label: { Image(systemName: "info.circle") }
                .buttonStyle(.borderless)
                .help("How do my Macs work together?")
                .accessibilityLabel("How do my Macs work together?")
                .popover(isPresented: $showInfo, arrowEdge: .bottom) { AutomationExplainer.macs(profile: profile) }
            HStack(spacing: 8) {
                    Text(available ? "Available" : "Busy").font(.callout.weight(.medium))
                        .foregroundStyle(available ? Color.green : Color.orange)
                    Toggle("Available to my other Macs", isOn: Binding(get: { available }, set: { value in
                        Task { await act("dashboard/api/fleet/availability", ["available": value]) }
                    }))
                    .toggleStyle(.switch).labelsHidden().tint(.green)
                    .help(available ? "Your other Macs can use this one. Turn off to be Busy."
                                    : "Busy: no other Mac can use this one. Turn on to be available.")
                    .accessibilityLabel("Available to my other Macs")
            }
            Button {
                inviting = true
            } label: {
                Label("Invite a Mac", systemImage: "person.badge.plus")
            }
            .buttonStyle(.borderedProminent)
            .disabled(thisURL.isEmpty || !available)
            .help(thisURL.isEmpty ? "Turn on a public address in Connections first."
                                  : available ? "Let another of your Macs use this one." : "Turn Available on to invite a Mac.")
        }
    }

    // MARK: Someone is asking

    private func requestCard(_ request: FleetRequest) -> some View {
        HStack(alignment: .center, spacing: 16) {
            MacPicture(model: "Mac", size: 52)
            VStack(alignment: .leading, spacing: 6) {
                Text("\(request.name) wants to use this Mac").font(.headline)
                Label(MacAccess.title(request.permission)
                        + (request.permission == "full" && request.fullFor != "none"
                            ? " " + MacAccess.durationTitle(request.fullFor).lowercased() : "")
                        + " — " + MacAccess.blurb(request.permission),
                      systemImage: MacAccess.symbol(request.permission))
                    .font(.caption).foregroundStyle(.secondary)
                HStack(spacing: 10) {
                    Text("Allow it only if that Mac shows").font(.caption).foregroundStyle(.secondary)
                    Text(request.checkCode).font(.system(size: 20, weight: .semibold, design: .monospaced)).tracking(2)
                        .accessibilityLabel("code \(request.checkCode)")
                }
            }
            Spacer(minLength: 10)
            VStack(spacing: 8) {
                Button("Allow") { Task { await decide(request, allow: true) } }
                    .buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction)
                Button("Don’t Allow") { Task { await decide(request, allow: false) } }
            }
            .disabled(busy)
        }
        .taskCard(highlight: .accentColor)
    }

    // MARK: Cards

    private var thisMacCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top) {
                MacPicture(model: thisModel)
                Spacer()
                PresencePill(presence: available ? "available" : "busy")
            }
            VStack(alignment: .leading, spacing: 3) {
                if let draft = renaming {
                    HStack(spacing: 6) {
                        TextField("Name", text: Binding(get: { draft }, set: { renaming = $0 }))
                            .textFieldStyle(.roundedBorder)
                            .onSubmit { Task { await rename() } }
                        Button("Save") { Task { await rename() } }
                    }
                } else {
                    HStack(spacing: 6) {
                        Text(thisName).font(.headline).lineLimit(1)
                        Button { renaming = thisName } label: { Image(systemName: "pencil") }
                            .buttonStyle(.borderless).help("Rename this Mac").accessibilityLabel("Rename this Mac")
                    }
                }
                Text("This Mac · " + thisModel).font(.caption).foregroundStyle(.secondary)
                Text(thisURL.isEmpty ? "No public address yet: other Macs can’t reach it."
                                     : "Set to \(MacAccess.profileTitle(profile)) in Permissions & Safety")
                    .font(.caption).foregroundStyle(thisURL.isEmpty ? Color.orange : Color.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, minHeight: 172, alignment: .topLeading)
        .taskCard(highlight: Color.accentColor.opacity(0.6))
    }

    private func peerCard(_ peer: FleetPeer) -> some View {
        let hovering = hovered == peer.id
        return VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top) {
                MacPicture(model: peer.model)
                    .opacity(peer.presence == "offline" || peer.presence == "ended" ? 0.45 : 1)
                Spacer()
                PresencePill(presence: peer.presence)
            }
            VStack(alignment: .leading, spacing: 3) {
                Text(peer.name).font(.headline).lineLimit(1)
                Label(peer.accessLine, systemImage: MacAccess.symbol(peer.limited ? peer.effective : peer.granted))
                    .font(.caption).foregroundStyle(peer.limited ? Color.orange : Color.secondary)
                Text(peer.detailLine).font(.caption).foregroundStyle(.secondary)
                    .lineLimit(2).fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, minHeight: 172, alignment: .topLeading)
        .taskCard(hovering: hovering)
        .overlay(alignment: .bottomTrailing) {
            Menu {
                Button("Remove from This Mac", role: .destructive) { removingPeer = peer }
            } label: {
                Image(systemName: "ellipsis.circle").font(.body)
            }
            .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
            .padding(10)
            .opacity(hovering ? 1 : 0.55)
            .accessibilityLabel("More for \(peer.name)")
        }
        .onHover { inside in hovered = inside ? peer.id : (hovered == peer.id ? nil : hovered) }
        .accessibilityElement(children: .contain)
    }

    private var addCard: some View {
        Button {
            adding = true
        } label: {
            VStack(spacing: 8) {
                Image(systemName: "plus").font(.system(size: 22, weight: .light))
                Text("Add a Mac").font(.callout.weight(.medium))
                Text("Use another Mac that invited this one").font(.caption).foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
            }
            .frame(maxWidth: .infinity, minHeight: 172)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .foregroundStyle(Color.accentColor)
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .strokeBorder(style: StrokeStyle(lineWidth: 1.2, dash: [5, 4]))
                .foregroundStyle(Color.accentColor.opacity(0.5))
        )
    }

    // MARK: Who can use this Mac

    private var whoCanUse: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Who can use this Mac").font(.headline)
            VStack(spacing: 0) {
                ForEach(clients) { client in
                    HStack(spacing: 12) {
                        Image(systemName: "desktopcomputer").foregroundStyle(.secondary).frame(width: 22)
                            .accessibilityHidden(true)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(client.name).font(.callout.weight(.medium))
                            Text(clientLine(client)).font(.caption)
                                .foregroundStyle(client.limited ? Color.orange : Color.secondary)
                        }
                        .accessibilityElement(children: .combine)
                        Spacer()
                        accessMenu(client)
                        Button("Stop") { stoppingClient = client }
                    }
                    .padding(.vertical, 8)
                    if client.id != clients.last?.id { Divider() }
                }
            }
            .taskCard()
            if !available {
                Label("This Mac is Busy, so none of them can use it right now.", systemImage: "moon.fill")
                    .font(.caption).foregroundStyle(.orange)
            }
        }
    }

    /// The owner gives full access again for a while, or less (#80). Only here: the other Mac cannot ask for it.
    private func accessMenu(_ client: FleetClient) -> some View {
        Menu("Access") {
            Section("Full access") {
                ForEach(MacAccess.durations, id: \.self) { duration in
                    Button(MacAccess.durationTitle(duration)) {
                        Task { await setAccess(client, "full", duration) }
                    }
                }
            }
            Button(MacAccess.title("read_only")) { Task { await setAccess(client, "read_only") } }
            Button(MacAccess.title("status")) { Task { await setAccess(client, "status") } }
        }
        .menuStyle(.borderlessButton)
        .fixedSize()
        .disabled(busy)
        .help("Change what \(client.name) may do here")
    }

    private func clientLine(_ client: FleetClient) -> String {
        if client.fullExpired, let until = client.fullUntil {
            return "Full access ended \(MacAccess.until(until)) · reads only now"
        }
        var line = MacAccess.title(client.granted)
        if client.granted == "full", let until = client.fullUntil {
            line += " until " + MacAccess.until(until)
        }
        if client.limited {
            line += " · \(MacAccess.title(client.effective).lowercased()), because this Mac is set to \(MacAccess.profileTitle(profile))"
        }
        if let seen = client.lastSeenAt {
            line += " · last used " + Date(timeIntervalSince1970: seen).formatted(date: .abbreviated, time: .shortened)
        }
        return line
    }

    // MARK: Server

    private func load() async {
        let result = await state.taskAPI("dashboard/api/fleet")
        guard result.ok else {
            message = result.object["message"] as? String ?? "Start Mac MCP to see your Macs."
            return
        }
        thisMac = result.object["this_mac"] as? [String: Any] ?? [:]
        peers = (result.object["peers"] as? [[String: Any]] ?? []).map(FleetPeer.init)
        clients = (result.object["clients"] as? [[String: Any]] ?? []).map(FleetClient.init)
        requests = (result.object["requests"] as? [[String: Any]] ?? []).map(FleetRequest.init)
        if !requests.isEmpty { inviting = false }  // the invitation was used: the request is answered on the card
        loaded = true
        if message.hasPrefix("Start Mac MCP") { message = "" }
    }

    private func act(_ path: String, _ body: [String: Any]) async {
        busy = true
        let result = await state.taskAPI(path, body: body)
        busy = false
        message = result.ok ? "" : (result.object["message"] as? String ?? "Couldn’t do that. Try again.")
        await load()
    }

    private func setAccess(_ client: FleetClient, _ permission: String, _ duration: String = "none") async {
        await act("dashboard/api/fleet/clients/access",
                  ["client_id": client.clientID, "permission": permission, "full_for": duration])
    }

    private func decide(_ request: FleetRequest, allow: Bool) async {
        await act("dashboard/api/fleet/requests/decide", ["request_id": request.requestID, "allow": allow])
    }

    private func rename() async {
        guard let name = renaming?.trimmingCharacters(in: .whitespaces), !name.isEmpty else { return }
        await act("dashboard/api/fleet/name", ["name": name])
        renaming = nil
    }
}

// MARK: - Invite a Mac

struct InviteMacSheet: View {
    @ObservedObject var state: AppState
    let profile: String
    let address: String
    let done: () -> Void

    @MacMCPState private var access = "full"
    @MacMCPState private var fullFor = "none"
    @MacMCPState private var invitation: FleetInvitation?
    @MacMCPState private var problem = ""
    @MacMCPState private var working = false
    @MacMCPState private var showInfo = false
    @MacMCPState private var now = Date()

    init(state: AppState, profile: String, address: String, invitation: FleetInvitation? = nil,
         done: @escaping () -> Void) {
        self.state = state
        self.profile = profile
        self.address = address
        self.done = done
        _invitation = MacMCPState(initialValue: invitation)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(spacing: 10) {
                Image(systemName: "person.badge.plus").font(.title2).foregroundStyle(Color.accentColor)
                Text("Invite a Mac").font(.title3.weight(.semibold))
                Spacer()
            }
            if let invitation { invitationView(invitation) } else { chooser }
            if !problem.isEmpty {
                Label(problem, systemImage: "exclamationmark.triangle").font(.caption).foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                if invitation != nil {
                    Button("Cancel Invitation") {
                        Task { _ = await state.taskAPI("dashboard/api/fleet/invite/cancel", body: [:]); done() }
                    }
                }
                Spacer()
                Button(invitation == nil ? "Cancel" : "Done") { done() }
                if invitation == nil {
                    Button("Create Invitation") { Task { await create() } }
                        .buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction).disabled(working)
                }
            }
        }
        .padding(22)
        .frame(width: 520)
        .task {
            while !Task.isCancelled {
                now = Date()
                try? await Task.sleep(nanoseconds: 1_000_000_000)
            }
        }
    }

    private var chooser: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 6) {
                Text("What may the other Mac do here?").font(.callout.weight(.medium))
                Button { showInfo.toggle() } label: { Image(systemName: "info.circle") }
                    .buttonStyle(.borderless)
                    .help("Why could a Mac get less than I choose?")
                    .accessibilityLabel("Why could a Mac get less than I choose?")
                    .popover(isPresented: $showInfo, arrowEdge: .bottom) { AutomationExplainer.macs(profile: profile) }
            }
            ForEach(["full", "read_only", "status"], id: \.self) { option in
                Button {
                    access = option
                } label: {
                    HStack(spacing: 12) {
                        Image(systemName: MacAccess.symbol(option)).font(.title3).frame(width: 26)
                            .foregroundStyle(access == option ? Color.accentColor : Color.secondary)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(MacAccess.title(option)).font(.callout.weight(.semibold))
                            Text(MacAccess.blurb(option)).font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Image(systemName: access == option ? "checkmark.circle.fill" : "circle")
                            .foregroundStyle(access == option ? Color.accentColor : Color.secondary)
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .taskCard(highlight: access == option ? .accentColor : nil)
                .accessibilityAddTraits(access == option ? .isSelected : [])
            }
            if access == "full" {
                HStack(spacing: 8) {
                    Picker("How long", selection: $fullFor) {
                        ForEach(MacAccess.durations, id: \.self) { Text(MacAccess.durationTitle($0)).tag($0) }
                    }
                    .fixedSize()
                    Text(fullFor == "none" ? "Until you change it or stop it."
                                           : "From when you allow it. Then it reads only, until you give more.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            if let note = limitNote {
                Label(note, systemImage: "checkmark.shield").font(.caption).foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    /// Said before inviting, so "I gave full access and it only reads" never comes as a surprise.
    private var limitNote: String? {
        guard access == "full", profile != "trusted" else { return nil }
        return "This Mac is set to \(MacAccess.profileTitle(profile)) in Permissions & Safety, so the other Mac gets "
            + (profile == "read_only" ? "read-only" : "standard") + " access here. Change that setting to give it more."
    }

    private func invitationView(_ invitation: FleetInvitation) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            Label("On the other Mac, open Mac MCP › Settings › Devices and choose Add a Mac.", systemImage: "1.circle.fill")
                .font(.callout)
            VStack(alignment: .leading, spacing: 6) {
                Label("Paste this link there:", systemImage: "2.circle.fill").font(.callout)
                HStack {
                    Text(invitation.link).font(.system(.caption, design: .monospaced)).lineLimit(1).truncationMode(.middle)
                        .textSelection(.enabled)
                    Spacer(minLength: 6)
                    Button {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(invitation.link, forType: .string)
                    } label: { Label("Copy", systemImage: "doc.on.doc") }
                }
                .padding(10)
                .background(RoundedRectangle(cornerRadius: 8).fill(Color.primary.opacity(0.05)))
            }
            HStack(alignment: .firstTextBaseline, spacing: 26) {
                Text("or type").font(.caption).foregroundStyle(.secondary)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Address").font(.caption2).foregroundStyle(.secondary)
                    Text(Self.typedForm(of: invitation.address)).font(.system(size: 16, weight: .semibold, design: .monospaced))
                        .textSelection(.enabled)
                }
                VStack(alignment: .leading, spacing: 2) {
                    Text("Code").font(.caption2).foregroundStyle(.secondary)
                    Text(invitation.code).font(.system(size: 16, weight: .semibold, design: .monospaced)).tracking(2)
                        .textSelection(.enabled)
                }
            }
            Label("Then allow it here. It asks for " + MacAccess.title(access).lowercased()
                  + (access == "full" && fullFor != "none" ? " " + MacAccess.durationTitle(fullFor).lowercased() : "")
                  + ".", systemImage: "3.circle.fill")
                .font(.callout)
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text(remaining(invitation)).font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    private func remaining(_ invitation: FleetInvitation) -> String {
        let left = Int(invitation.expiresAt - now.timeIntervalSince1970)
        if left <= 0 { return "This invitation has expired. Create a new one." }
        return String(format: "Waiting for the other Mac · works once, for %d:%02d", left / 60, left % 60)
    }

    static func typedForm(of url: String) -> String {
        url.hasPrefix("https://") ? String(url.dropFirst("https://".count)) : url
    }

    private func create() async {
        working = true
        let result = await state.taskAPI("dashboard/api/fleet/invite",
                                         body: ["permission": access, "full_for": access == "full" ? fullFor : "none"])
        working = false
        guard result.ok, let link = result.object["link"] as? String, let code = result.object["code"] as? String,
              let expires = result.object["expires_at"] as? Double else {
            problem = result.object["message"] as? String ?? "Couldn’t make an invitation."
            return
        }
        invitation = FleetInvitation(link: link, code: code, address: result.object["address"] as? String ?? address,
                                     expiresAt: expires)
        problem = ""
        // The other Mac's request arrives as a notification; ask for them once, here, where it makes sense.
        _ = await AgentNotificationController.shared.requestAuthorization()
    }
}

// MARK: - Add a Mac

struct AddMacSheet: View {
    @ObservedObject var state: AppState
    let done: () -> Void

    @MacMCPState private var link = ""
    @MacMCPState private var address = ""
    @MacMCPState private var code = ""
    @MacMCPState private var problem = ""
    @MacMCPState private var working = false
    @MacMCPState private var waiting: FleetPeer?

    init(state: AppState, waiting: FleetPeer? = nil, done: @escaping () -> Void) {
        self.state = state
        self.done = done
        _waiting = MacMCPState(initialValue: waiting)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(spacing: 10) {
                Image(systemName: "plus.circle").font(.title2).foregroundStyle(Color.accentColor)
                Text("Add a Mac").font(.title3.weight(.semibold))
            }
            if let waiting { waitingView(waiting) } else { entry }
            if !problem.isEmpty {
                Label(problem, systemImage: "exclamationmark.triangle").font(.caption).foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                Spacer()
                Button(waiting == nil ? "Cancel" : "Done") { done() }
                if waiting == nil {
                    Button("Add") { Task { await add() } }
                        .buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction)
                        .disabled(working || (link.trimmingCharacters(in: .whitespaces).isEmpty
                                              && code.trimmingCharacters(in: .whitespaces).isEmpty))
                }
            }
        }
        .padding(22)
        .frame(width: 520)
        .task {
            while !Task.isCancelled {
                if let current = waiting, current.presence == "waiting" { await poll(current) }
                try? await Task.sleep(nanoseconds: 1_500_000_000)
            }
        }
    }

    private var entry: some View {
        VStack(alignment: .leading, spacing: 12) {
            Label("On the other Mac, open Mac MCP › Settings › Devices and choose Invite a Mac.", systemImage: "1.circle.fill")
                .font(.callout)
            Label("Paste its link here:", systemImage: "2.circle.fill").font(.callout)
            TextField("Invitation link", text: $link).textFieldStyle(.roundedBorder)
                .onSubmit { Task { await add() } }
            HStack(spacing: 8) {
                Text("or type its").font(.caption).foregroundStyle(.secondary)
                TextField("Address", text: $address).textFieldStyle(.roundedBorder)
                TextField("Code", text: $code).textFieldStyle(.roundedBorder).frame(width: 130)
                    .onSubmit { Task { await add() } }
            }
        }
    }

    private func waitingView(_ peer: FleetPeer) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 14) {
                MacPicture(model: peer.model, size: 54)
                VStack(alignment: .leading, spacing: 4) {
                    Text(peer.name).font(.headline)
                    PresencePill(presence: peer.presence)
                }
            }
            if peer.presence == "waiting" {
                Text("On \(peer.name), allow this Mac. Check that it shows:").font(.callout)
                Text(peer.checkCode ?? "").font(.system(size: 30, weight: .semibold, design: .monospaced)).tracking(3)
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("Waiting for its owner…").font(.caption).foregroundStyle(.secondary)
                }
            } else if peer.presence == "ended" {
                Label("Its owner didn’t allow this Mac.", systemImage: "xmark.circle").foregroundStyle(.red)
            } else {
                Label("Connected. " + peer.accessLine + ".", systemImage: "checkmark.circle.fill")
                    .foregroundStyle(.green).font(.callout.weight(.medium))
            }
        }
    }

    private func add() async {
        let pasted = link.trimmingCharacters(in: .whitespacesAndNewlines)
        let typedAddress = address.trimmingCharacters(in: .whitespacesAndNewlines)
        let typedCode = code.trimmingCharacters(in: .whitespacesAndNewlines)
        let text: String
        if !pasted.isEmpty {
            text = pasted
        } else if !typedAddress.isEmpty && !typedCode.isEmpty {
            text = typedAddress + " " + typedCode
        } else {
            problem = typedCode.isEmpty ? "Paste the link, or type the other Mac’s address and code."
                                        : "Also type the other Mac’s address (it is shown next to the code there)."
            return
        }
        working = true
        problem = ""
        let result = await state.taskAPI("dashboard/api/fleet/peers/add", body: ["invitation": text])
        working = false
        guard result.ok, let peer = result.object["peer"] as? [String: Any] else {
            problem = result.object["message"] as? String ?? "Couldn’t add that Mac."
            return
        }
        waiting = FleetPeer(peer)
    }

    private func poll(_ current: FleetPeer) async {
        let result = await state.taskAPI("dashboard/api/fleet")
        let rows = (result.object["peers"] as? [[String: Any]] ?? []).map(FleetPeer.init)
        if let fresh = rows.first(where: { $0.peerID == current.peerID }) { waiting = fresh }
    }
}
