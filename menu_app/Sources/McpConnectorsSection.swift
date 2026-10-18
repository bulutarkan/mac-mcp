import AppKit
import SwiftUI

/// Settings › MCP Servers › Connect a Service (#260): GitHub, Notion, Slack and Google Drive through
/// their own hosted MCP servers, without an endpoint or a command. The catalog comes from
/// /dashboard/api/mcp-servers (mcp_connectors.py); the credential is sent once and kept in the
/// Keychain by Mac MCP, never read back here.

struct McpConnector: Identifiable {
    let id: String
    let title: String
    let summary: String
    let auth: String
    let symbol: String
    let manageURL: String
    let manageHint: String
    let tokenURL: String?
    let tokenHint: String
    let setupURL: String?
    let setupSteps: [String]
    let redirectURI: String?
    let verified: Bool

    init(_ object: [String: Any]) {
        id = object["id"] as? String ?? ""
        title = object["title"] as? String ?? id
        summary = object["summary"] as? String ?? ""
        auth = object["auth"] as? String ?? "oauth"
        symbol = object["symbol"] as? String ?? "puzzlepiece.extension"
        manageURL = object["manage_url"] as? String ?? ""
        manageHint = object["manage_hint"] as? String ?? ""
        tokenURL = object["token_url"] as? String
        tokenHint = object["token_hint"] as? String ?? ""
        setupURL = object["setup_url"] as? String
        setupSteps = object["setup_steps"] as? [String] ?? []
        redirectURI = object["redirect_uri"] as? String
        verified = object["verified"] as? Bool ?? true
    }
}

struct McpConnectorsSection: View {
    @ObservedObject var state: AppState
    let connectors: [McpConnector]
    let servers: [McpServer]
    @Binding var connecting: McpConnector?
    let changed: () async -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Connect a Service").font(.headline)
            LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible(), spacing: 10)], spacing: 10) {
                ForEach(connectors) { connector in
                    card(connector)
                }
            }
        }
        .sheet(item: $connecting) { connector in
            McpConnectorSheet(state: state, connector: connector) { connected in
                connecting = nil
                if connected { Task { await changed() } }
            }
        }
    }

    private func card(_ connector: McpConnector) -> some View {
        let server = servers.first { $0.catalog == connector.id }
        return VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .top, spacing: 9) {
                Image(systemName: connector.symbol)
                    .font(.system(size: 15, weight: .medium))
                    .frame(width: 28, height: 28)
                    .background(RoundedRectangle(cornerRadius: 7).fill(Color.accentColor.opacity(0.14)))
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 2) {
                    Text(connector.title).font(.subheadline.weight(.semibold))
                    Text(connector.summary).font(.caption).foregroundStyle(.secondary)
                        .lineLimit(3).fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 0)
            HStack(spacing: 8) {
                if let server {
                    Label(server.status == "connected" ? "Connected · \(server.tools.count) tools" : server.statusLabel,
                          systemImage: server.status == "connected" ? "checkmark.circle.fill" : "exclamationmark.circle")
                        .font(.caption).foregroundStyle(server.status == "connected" ? Color.green : server.statusColor)
                } else if connector.auth == "oauth_app" {
                    Text("Uses an app of your own").font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                if server == nil {
                    Button("Connect") { connecting = connector }
                        .accessibilityLabel("Connect \(connector.title)")
                } else if server?.status == "sign_in_required" {
                    Button("Sign In Again") { connecting = connector }
                        .accessibilityLabel("Sign in to \(connector.title) again")
                }
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, minHeight: 112, alignment: .topLeading)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color.primary.opacity(0.04)))
        .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(Color.primary.opacity(0.08)))
        .accessibilityElement(children: .contain)
    }
}

// MARK: - Connect sheet

struct McpConnectorSheet: View {
    @ObservedObject var state: AppState
    let connector: McpConnector
    let done: (Bool) -> Void

    @MacMCPState private var token = ""
    @MacMCPState private var clientID = ""
    @MacMCPState private var clientSecret = ""
    @MacMCPState private var busy = false
    @MacMCPState private var waiting = false
    @MacMCPState private var message = ""
    @MacMCPState private var copied = false

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 10) {
                Image(systemName: connector.symbol).font(.title2).accessibilityHidden(true)
                Text("Connect \(connector.title)").font(.title3.weight(.semibold))
            }
            Text(connector.summary).font(.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            switch connector.auth {
            case "token": tokenSteps
            case "oauth_app": appSteps
            default:
                Text("Mac MCP opens \(connector.title) in your browser. Choose what Mac MCP may use and approve it, then come back here.")
                    .font(.callout).fixedSize(horizontal: false, vertical: true)
            }

            Text("Agents use its tools with your permission profile, like Mac MCP’s own tools. The sign-in stays in your Keychain. \(connector.manageHint)")
                .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)

            if waiting {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("Waiting for you to finish in the browser…").font(.callout)
                }
            }
            if !message.isEmpty {
                Text(message).font(.caption).foregroundStyle(.red).textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                if busy && !waiting { ProgressView().controlSize(.small) }
                Spacer()
                Button("Cancel") { Task { await cancel() } }.keyboardShortcut(.cancelAction)
                Button(connector.auth == "token" ? "Connect" : "Sign In with \(connector.title)") {
                    Task { await connect() }
                }
                .keyboardShortcut(.defaultAction).disabled(!ready || busy || waiting)
            }
        }
        .padding(20)
        .frame(width: 520)
    }

    private var tokenSteps: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(connector.tokenHint).font(.callout).fixedSize(horizontal: false, vertical: true)
            if let link = connector.tokenURL.flatMap(URL.init(string:)) {
                Link("Create a Token on \(connector.title)", destination: link).font(.callout)
            }
            SecureField("Token", text: $token)
                .textFieldStyle(.roundedBorder)
                .accessibilityLabel("\(connector.title) token")
        }
    }

    private var appSteps: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(Array(connector.setupSteps.enumerated()), id: \.offset) { index, step in
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text("\(index + 1).").font(.callout.monospacedDigit()).foregroundStyle(.secondary)
                    Text(step).font(.callout).fixedSize(horizontal: false, vertical: true)
                }
            }
            if let link = connector.setupURL.flatMap(URL.init(string:)) {
                Link("Open \(link.host ?? connector.title)", destination: link).font(.callout)
            }
            if let redirect = connector.redirectURI {
                HStack(spacing: 8) {
                    Text(redirect).font(.caption.monospaced()).textSelection(.enabled)
                        .lineLimit(1).truncationMode(.middle)
                    Button(copied ? "Copied" : "Copy") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(redirect, forType: .string)
                        copied = true
                    }
                    .controlSize(.small)
                    .accessibilityLabel("Copy redirect URL")
                }
                .padding(8)
                .background(RoundedRectangle(cornerRadius: 6).fill(Color.primary.opacity(0.05)))
            }
            TextField("Client ID", text: $clientID).textFieldStyle(.roundedBorder)
            SecureField("Client Secret", text: $clientSecret).textFieldStyle(.roundedBorder)
            if !connector.verified {
                Text("Mac MCP follows \(connector.title)’s published sign-in steps; this connector has not been tried with a real \(connector.title) account yet.")
                    .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var ready: Bool {
        switch connector.auth {
        case "token": return !token.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        case "oauth_app": return !clientID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        default: return true
        }
    }

    private func connect() async {
        var body: [String: Any] = ["action": "connect", "connector": connector.id]
        switch connector.auth {
        case "token": body["token"] = token.trimmingCharacters(in: .whitespacesAndNewlines)
        case "oauth_app":
            body["client_id"] = clientID.trimmingCharacters(in: .whitespacesAndNewlines)
            body["client_secret"] = clientSecret.trimmingCharacters(in: .whitespacesAndNewlines)
        default: break
        }
        busy = true
        message = ""
        let response = await state.taskAPI("dashboard/api/mcp-servers/action", body: body)
        busy = false
        let result = response.object["result"] as? [String: Any] ?? [:]
        guard response.ok else {
            message = response.object["message"] as? String ?? "Couldn’t connect \(connector.title)."
            return
        }
        if connector.auth == "token" {
            token = ""
            done(true)
            return
        }
        guard let page = (result["authorize_url"] as? String).flatMap(URL.init(string:)) else {
            message = result["error"] as? String ?? "\(connector.title) did not offer a sign-in page."
            return
        }
        NSWorkspace.shared.open(page)
        waiting = true
        await waitForSignIn()
    }

    private func waitForSignIn() async {
        let deadline = Date().addingTimeInterval(11 * 60)
        while waiting, Date() < deadline {
            try? await Task.sleep(nanoseconds: 1_000_000_000)
            guard waiting else { return }
            let status = await state.taskAPI("dashboard/api/mcp-servers")
            let flow = (status.object["sign_ins"] as? [String: Any])?[connector.id] as? [String: Any]
            switch flow?["state"] as? String {
            case "connected":
                waiting = false
                done(true)
                return
            case "failed":
                waiting = false
                message = flow?["error"] as? String ?? "The sign-in did not finish."
                return
            case "waiting", "starting":
                continue
            default:
                if status.ok {
                    waiting = false
                    message = "The sign-in stopped. Start it again."
                    return
                }
            }
        }
        if waiting {
            waiting = false
            message = "The sign-in was not finished in time. Start it again."
        }
    }

    private func cancel() async {
        if waiting {
            waiting = false
            _ = await state.taskAPI("dashboard/api/mcp-servers/action",
                                    body: ["action": "cancel_sign_in", "name": connector.id])
        }
        done(false)
    }
}
