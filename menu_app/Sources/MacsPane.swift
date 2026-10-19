import AppKit
import SwiftUI

/// Settings › Macs (#147): the owner's other Macs. This Mac can invite another one (a link and a
/// one-time code), allow or refuse it after comparing a 6-digit check, and end its access; it can
/// also add Macs that invited it and see whether they answer. Each Mac keeps its own address, and
/// a credential issued by the other Mac, never its Mac MCP API key (fleet.py).

struct FleetPeer: Identifiable, Equatable {
    let peerID: String
    let name: String
    let url: String
    let state: String
    let permission: String
    let checkCode: String?
    let reachable: Bool
    let lastOkAt: Double?
    let lastError: String?
    let version: String?
    let uptime: Double?
    var id: String { peerID }

    init(_ object: [String: Any]) {
        peerID = object["peer_id"] as? String ?? ""
        name = object["name"] as? String ?? "Mac"
        url = object["url"] as? String ?? ""
        state = object["state"] as? String ?? ""
        permission = object["permission"] as? String ?? ""
        checkCode = object["check_code"] as? String
        reachable = object["reachable"] as? Bool ?? false
        lastOkAt = object["last_ok_at"] as? Double
        lastError = object["last_error"] as? String
        let status = object["status"] as? [String: Any] ?? [:]
        version = status["version"] as? String
        uptime = status["uptime_s"] as? Double
    }

    var dotColor: Color {
        switch state {
        case "paired": return reachable ? .green : .orange
        case "waiting": return .blue
        default: return .red
        }
    }

    var detail: String {
        switch state {
        case "waiting":
            return "Waiting for its owner to allow this Mac. It shows " + (checkCode ?? "a check") + " there too."
        case "refused": return "Its owner refused. Ask for a new invitation to try again."
        case "expired": return "The request ran out of time. Ask for a new invitation."
        case "removed_there": return "That Mac no longer lets this one in."
        default:
            if reachable {
                var parts = ["Mac MCP " + (version ?? "?")]
                if let uptime { parts.append("running " + MacsPane.span(uptime)) }
                parts.append(MacsPane.permissionWords(permission))
                return parts.joined(separator: " · ")
            }
            let since = lastOkAt.map { " since " + Date(timeIntervalSince1970: $0).formatted(date: .abbreviated, time: .shortened) } ?? ""
            return "Not answering" + since + ". It may be asleep, off, or its address changed."
        }
    }
}

struct FleetClient: Identifiable, Equatable {
    let clientID: String
    let name: String
    let permission: String
    let lastSeenAt: Double?
    var id: String { clientID }

    init(_ object: [String: Any]) {
        clientID = object["client_id"] as? String ?? ""
        name = object["name"] as? String ?? "Mac"
        permission = object["permission"] as? String ?? "status"
        lastSeenAt = object["last_seen_at"] as? Double
    }
}

struct FleetRequest: Identifiable, Equatable {
    let requestID: String
    let name: String
    let permission: String
    let checkCode: String
    var id: String { requestID }

    init(_ object: [String: Any]) {
        requestID = object["request_id"] as? String ?? ""
        name = object["client_name"] as? String ?? "A Mac"
        permission = object["permission"] as? String ?? "status"
        checkCode = object["check_code"] as? String ?? ""
    }
}

struct MacsPane: View {
    @ObservedObject var state: AppState

    @MacMCPState private var thisName = ""
    @MacMCPState private var thisURL = ""
    @MacMCPState private var peers: [FleetPeer] = []
    @MacMCPState private var clients: [FleetClient] = []
    @MacMCPState private var requests: [FleetRequest] = []
    @MacMCPState private var loaded = false
    @MacMCPState private var message = ""
    @MacMCPState private var invitePermission = "status"
    @MacMCPState private var invitation: (link: String, code: String, expiresAt: Double)?
    @MacMCPState private var pasted = ""
    @MacMCPState private var typedAddress = ""
    @MacMCPState private var typedCode = ""
    @MacMCPState private var addIssue = ""
    @MacMCPState private var busy = false
    @MacMCPState private var renaming: String?
    @MacMCPState private var removingPeer: FleetPeer?
    @MacMCPState private var removingClient: FleetClient?

    init(state: AppState, invitation: (link: String, code: String, expiresAt: Double)? = nil) {
        self.state = state
        _invitation = MacMCPState(initialValue: invitation)
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("Macs").font(.title2.weight(.semibold))
                    Text("See your other Macs and run their recipes from here. Each Mac keeps its own address; "
                         + "nothing goes through a server in between.")
                        .font(.callout).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if !message.isEmpty {
                    Label(message, systemImage: "exclamationmark.triangle").font(.caption).foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                }
                ForEach(requests) { request in requestBox(request) }
                thisMacBox
                yourMacsBox
                if !clients.isEmpty { clientsBox }
                Spacer(minLength: 0)
            }
            .padding(22)
        }
        .task {
            while !Task.isCancelled {
                await load()
                try? await Task.sleep(nanoseconds: 2_000_000_000)
            }
        }
        .confirmationDialog("Remove \(removingPeer?.name ?? "this Mac")?", isPresented: Binding(
            get: { removingPeer != nil }, set: { if !$0 { removingPeer = nil } })) {
            Button("Remove", role: .destructive) {
                if let peer = removingPeer { Task { await act("dashboard/api/fleet/peers/remove", ["peer_id": peer.peerID]) } }
                removingPeer = nil
            }
        } message: {
            Text("This Mac stops seeing it and asks it to forget this Mac too. You can pair them again later.")
        }
        .confirmationDialog("Stop letting \(removingClient?.name ?? "that Mac") in?", isPresented: Binding(
            get: { removingClient != nil }, set: { if !$0 { removingClient = nil } })) {
            Button("Stop", role: .destructive) {
                if let client = removingClient { Task { await act("dashboard/api/fleet/clients/revoke", ["client_id": client.clientID]) } }
                removingClient = nil
            }
        } message: {
            Text("Its key stops working at once. It can be invited again later.")
        }
    }

    // MARK: Someone is asking

    private func requestBox(_ request: FleetRequest) -> some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 8) {
                Label("\(request.name) wants to connect to this Mac", systemImage: "desktopcomputer.and.arrow.down")
                    .font(.headline)
                Text("It asks to \(Self.permissionVerb(request.permission)). Allow it only if that Mac shows the same check:")
                    .font(.callout).foregroundStyle(.secondary)
                Text(request.checkCode)
                    .font(.system(size: 24, weight: .semibold, design: .monospaced)).tracking(2)
                    .accessibilityLabel("Check \(request.checkCode)")
                HStack {
                    Button("Allow") { Task { await decide(request, allow: true) } }
                        .buttonStyle(.borderedProminent)
                        .keyboardShortcut(.defaultAction)
                    Button("Refuse") { Task { await decide(request, allow: false) } }
                    Spacer()
                }
                .disabled(busy)
            }
            .padding(6)
        }
    }

    // MARK: This Mac

    private var thisMacBox: some View {
        GroupBox("This Mac") {
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    if let draft = renaming {
                        TextField("Name", text: Binding(get: { draft }, set: { renaming = $0 }))
                            .textFieldStyle(.roundedBorder).frame(maxWidth: 260)
                            .onSubmit { Task { await rename() } }
                        Button("Save") { Task { await rename() } }
                        Button("Cancel") { renaming = nil }
                    } else {
                        Text(thisName.isEmpty ? "This Mac" : thisName).font(.subheadline.weight(.medium))
                        Button("Rename") { renaming = thisName }.buttonStyle(.link)
                    }
                    Spacer()
                }
                if thisURL.isEmpty {
                    Label("Other Macs reach this one at its public address. Turn on Cloudflare, ngrok or a custom HTTPS "
                          + "URL in Connections to invite one.", systemImage: "exclamationmark.triangle")
                        .font(.caption).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true)
                } else {
                    Text("Address: " + thisURL).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                    HStack(spacing: 10) {
                        Picker("Invited Mac can", selection: $invitePermission) {
                            Text("see this Mac's status").tag("status")
                            Text("see status and run recipes").tag("recipes")
                        }
                        .frame(maxWidth: 300)
                        Button {
                            Task { await invite() }
                        } label: {
                            Label("Invite a Mac", systemImage: "plus.circle")
                        }
                        .disabled(busy)
                        Spacer()
                    }
                    if let invitation { invitationView(invitation) }
                }
            }
            .padding(.top, 5)
        }
    }

    private func invitationView(_ invitation: (link: String, code: String, expiresAt: Double)) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("On the other Mac, open Settings › Macs and paste this link under Your Macs:")
                .font(.caption).foregroundStyle(.secondary)
            HStack {
                Text(invitation.link).font(.system(.caption, design: .monospaced)).lineLimit(1).truncationMode(.middle)
                    .textSelection(.enabled)
                Button("Copy") {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(invitation.link, forType: .string)
                }
            }
            Text("Or type both of these there (the code alone does not say which Mac it is for):")
                .font(.caption).foregroundStyle(.secondary)
            HStack(alignment: .firstTextBaseline, spacing: 22) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Address").font(.caption2).foregroundStyle(.secondary)
                    Text(Self.typedForm(of: thisURL)).font(.system(size: 17, weight: .semibold, design: .monospaced))
                        .textSelection(.enabled)
                }
                VStack(alignment: .leading, spacing: 2) {
                    Text("Code").font(.caption2).foregroundStyle(.secondary)
                    Text(invitation.code).font(.system(size: 17, weight: .semibold, design: .monospaced)).tracking(2)
                        .textSelection(.enabled)
                }
            }
            HStack {
                Text("Works once, until " + Date(timeIntervalSince1970: invitation.expiresAt).formatted(date: .omitted, time: .shortened)
                     + ". You still allow the Mac before it can see anything.")
                    .font(.caption2).foregroundStyle(.secondary)
                Spacer()
                Button("Cancel Invitation") { Task { await cancelInvitation() } }.buttonStyle(.link)
            }
        }
        .padding(10)
        .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 8))
    }

    // MARK: Your Macs

    private var yourMacsBox: some View {
        GroupBox("Your Macs") {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    TextField("Paste the invitation link from the other Mac", text: $pasted)
                        .textFieldStyle(.roundedBorder)
                        .onSubmit { Task { await add() } }
                }
                HStack {
                    Text("or type its").font(.caption).foregroundStyle(.secondary)
                    TextField("Address", text: $typedAddress)
                        .textFieldStyle(.roundedBorder)
                        .onSubmit { Task { await add() } }
                    TextField("Code", text: $typedCode)
                        .textFieldStyle(.roundedBorder).frame(width: 130)
                        .onSubmit { Task { await add() } }
                    Button("Add a Mac") { Task { await add() } }
                        .disabled(busy || (pasted.trimmingCharacters(in: .whitespaces).isEmpty
                                           && typedCode.trimmingCharacters(in: .whitespaces).isEmpty))
                }
                if !addIssue.isEmpty {
                    Label(addIssue, systemImage: "exclamationmark.triangle").font(.caption).foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if peers.isEmpty {
                    Text(loaded ? "No other Macs yet. Invite this Mac from the other one, then add it here."
                                : "Loading…")
                        .font(.caption).foregroundStyle(.secondary).padding(.vertical, 4)
                } else {
                    ForEach(peers) { peer in
                        HStack(alignment: .top, spacing: 10) {
                            Circle().fill(peer.dotColor).frame(width: 8, height: 8).padding(.top, 5)
                                .accessibilityHidden(true)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(peer.name).font(.subheadline.weight(.medium))
                                Text(peer.detail).font(.caption).foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            .accessibilityElement(children: .combine)
                            Spacer()
                            Button("Remove") { removingPeer = peer }
                        }
                        .padding(.vertical, 4)
                        if peer.id != peers.last?.id { Divider() }
                    }
                }
            }
            .padding(.top, 5)
        }
    }

    private var clientsBox: some View {
        GroupBox("Macs That Can See This Mac") {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(clients) { client in
                    HStack(spacing: 10) {
                        Image(systemName: "desktopcomputer").foregroundStyle(.secondary).accessibilityHidden(true)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(client.name).font(.subheadline.weight(.medium))
                            Text(Self.permissionWords(client.permission) + (client.lastSeenAt.map {
                                " · last here " + Date(timeIntervalSince1970: $0).formatted(date: .abbreviated, time: .shortened)
                            } ?? ""))
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        .accessibilityElement(children: .combine)
                        Spacer()
                        Button("Stop") { removingClient = client }
                    }
                    .padding(.vertical, 4)
                    if client.id != clients.last?.id { Divider() }
                }
            }
            .padding(.top, 5)
        }
    }

    // MARK: Words

    static func permissionWords(_ permission: String) -> String {
        permission == "recipes" ? "Status and recipes" : "Status only"
    }

    static func permissionVerb(_ permission: String) -> String {
        permission == "recipes" ? "see this Mac's status and run its recipes" : "see this Mac's status"
    }

    /// What to type for this Mac's address: the host for HTTPS (https:// is assumed), the whole URL otherwise.
    static func typedForm(of url: String) -> String {
        url.hasPrefix("https://") ? String(url.dropFirst("https://".count)) : url
    }

    static func span(_ seconds: Double) -> String {
        let minutes = Int(seconds / 60)
        if minutes < 60 { return "\(max(1, minutes)) min" }
        let hours = minutes / 60
        return hours < 48 ? "\(hours) h" : "\(hours / 24) days"
    }

    // MARK: Server

    private func load() async {
        let result = await state.taskAPI("dashboard/api/fleet")
        guard result.ok else {
            message = result.object["message"] as? String ?? "Start Mac MCP to see your Macs."
            return
        }
        let me = result.object["this_mac"] as? [String: Any] ?? [:]
        thisName = me["name"] as? String ?? ""
        thisURL = me["url"] as? String ?? ""
        peers = (result.object["peers"] as? [[String: Any]] ?? []).map(FleetPeer.init)
        clients = (result.object["clients"] as? [[String: Any]] ?? []).map(FleetClient.init)
        requests = (result.object["requests"] as? [[String: Any]] ?? []).map(FleetRequest.init)
        if result.object["invitation"] is NSNull || result.object["invitation"] == nil { invitation = nil }
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

    private func invite() async {
        busy = true
        let result = await state.taskAPI("dashboard/api/fleet/invite", body: ["permission": invitePermission])
        busy = false
        guard result.ok, let link = result.object["link"] as? String, let code = result.object["code"] as? String,
              let expires = result.object["expires_at"] as? Double else {
            message = result.object["message"] as? String ?? "Couldn’t make an invitation."
            return
        }
        invitation = (link, code, expires)
        message = ""
    }

    private func cancelInvitation() async {
        await act("dashboard/api/fleet/invite/cancel", [:])
        invitation = nil
    }

    private func decide(_ request: FleetRequest, allow: Bool) async {
        await act("dashboard/api/fleet/requests/decide", ["request_id": request.requestID, "allow": allow])
        if allow { invitation = nil }
    }

    private func add() async {
        let link = pasted.trimmingCharacters(in: .whitespacesAndNewlines)
        let address = typedAddress.trimmingCharacters(in: .whitespacesAndNewlines)
        let code = typedCode.trimmingCharacters(in: .whitespacesAndNewlines)
        let text: String
        if !link.isEmpty {
            text = link
        } else if !address.isEmpty && !code.isEmpty {
            text = address + " " + code
        } else {
            addIssue = code.isEmpty ? "Paste the link, or type the other Mac's address and its code."
                                    : "Also type the other Mac's address (shown next to the code there)."
            return
        }
        busy = true
        addIssue = ""
        let result = await state.taskAPI("dashboard/api/fleet/peers/add", body: ["invitation": text])
        busy = false
        if result.ok {
            pasted = ""
            typedAddress = ""
            typedCode = ""
        } else {
            addIssue = result.object["message"] as? String ?? "Couldn’t add that Mac."
        }
        await load()
    }

    private func rename() async {
        guard let name = renaming?.trimmingCharacters(in: .whitespaces), !name.isEmpty else { return }
        await act("dashboard/api/fleet/name", ["name": name])
        renaming = nil
    }
}
