import AppKit
import SwiftUI

/// One saved login as `mac-mcp logins list --json` reports it (never a password).
struct SavedLogin: Decodable, Identifiable {
    let id: String
    let site: String
    let username: String
    let label: String?
}

/// One usable row of a Passwords app export, as `mac-mcp logins import-preview --json` reports it.
struct LoginImportItem: Decodable, Identifiable {
    let index: Int
    let site: String
    let username: String
    let title: String?
    var id: Int { index }
}

/// Settings > Permissions & Safety > Saved Logins: the logins agents may use through
/// browser_sign_in. Everything goes through the Mac MCP CLI; passwords travel on stdin.
struct SavedLoginsCard: View {
    @ObservedObject var state: AppState
    @MacMCPState private var logins: [SavedLogin] = []
    @MacMCPState private var message: String?
    @MacMCPState private var failed = false
    @MacMCPState private var busy = false
    @MacMCPState private var site = ""
    @MacMCPState private var username = ""
    @MacMCPState private var password = ""
    @MacMCPState private var importPath: String?
    @MacMCPState private var importItems: [LoginImportItem] = []
    @MacMCPState private var importSelected: Set<Int> = []
    @MacMCPState private var deleteExport = true

    var body: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 10) {
                Text("Agents can sign in to these sites with browser_sign_in. The password is typed into the page and is never shown to the agent or its AI provider, and only pages on the saved site get it.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)

                if logins.isEmpty {
                    Text("No saved logins yet.")
                        .font(.caption)
                        .foregroundStyle(.tertiary)
                } else {
                    ForEach(logins) { login in
                        HStack(spacing: 8) {
                            Image(systemName: "key.fill").foregroundStyle(.secondary)
                            VStack(alignment: .leading, spacing: 1) {
                                Text(login.site).font(.callout.weight(.medium))
                                Text(login.username).font(.caption).foregroundStyle(.secondary)
                            }
                            Spacer()
                            Button("Remove") { Task { await remove(login) } }
                                .disabled(busy)
                                .accessibilityLabel("Remove saved login for \(login.site)")
                        }
                    }
                }

                Divider()

                Text("Add a login").font(.caption.weight(.semibold))
                HStack(spacing: 8) {
                    TextField("Site, e.g. github.com", text: $site)
                    TextField("Username or email", text: $username)
                    SecureField("Password", text: $password)
                    Button("Save") { Task { await add() } }
                        .disabled(busy || site.isEmpty || username.isEmpty || password.isEmpty)
                }
                .textFieldStyle(.roundedBorder)

                Divider()

                HStack {
                    Button("Import from Passwords export…") { chooseExport() }
                        .disabled(busy)
                    Spacer()
                }
                Text("In the Passwords app choose File > Export All Passwords, then pick that file here and select the sites agents may use.")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)

                if importPath != nil {
                    importPicker
                }

                if let message {
                    Label(message, systemImage: failed ? "exclamationmark.triangle" : "checkmark.circle")
                        .font(.caption)
                        .foregroundStyle(failed ? Color.orange : Color.secondary)
                }
            }
            .padding(.vertical, 2)
        } label: {
            Label("Saved Logins", systemImage: "key")
        }
        .task { await reload() }
    }

    private var importPicker: some View {
        VStack(alignment: .leading, spacing: 6) {
            if importItems.isEmpty {
                Text("No usable logins in that file.").font(.caption).foregroundStyle(.secondary)
            } else {
                HStack {
                    Text("\(importSelected.count) of \(importItems.count) selected").font(.caption.weight(.semibold))
                    Spacer()
                    Button(importSelected.count == importItems.count ? "Select none" : "Select all") {
                        importSelected = importSelected.count == importItems.count ? [] : Set(importItems.map(\.index))
                    }
                    .buttonStyle(.link)
                }
                ScrollView {
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(importItems) { item in
                            Toggle(isOn: Binding(
                                get: { importSelected.contains(item.index) },
                                set: { on in
                                    if on { importSelected.insert(item.index) } else { importSelected.remove(item.index) }
                                }
                            )) {
                                Text("\(item.site)  ·  \(item.username)").font(.caption)
                            }
                            .toggleStyle(.checkbox)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(maxHeight: 160)
                Toggle("Delete the export file afterwards (it holds every password in plain text)", isOn: $deleteExport)
                    .toggleStyle(.checkbox)
                    .font(.caption)
            }
            HStack {
                Button("Import \(importSelected.count)") { Task { await runImport() } }
                    .disabled(busy || importSelected.isEmpty)
                Button("Cancel") { resetImport() }
            }
        }
        .padding(8)
        .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 6))
    }

    // MARK: - CLI

    private func json(_ output: String) -> [String: Any]? {
        for line in output.split(separator: "\n").reversed() where line.hasPrefix("{") {
            if let data = line.data(using: .utf8),
               let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                return object
            }
        }
        return nil
    }

    private func decode<T: Decodable>(_ type: T.Type, key: String, from output: String) -> T? {
        guard let object = json(output), let value = object[key],
              let data = try? JSONSerialization.data(withJSONObject: value) else { return nil }
        return try? JSONDecoder().decode(type, from: data)
    }

    private func report(_ text: String, failed: Bool) {
        message = text
        self.failed = failed
    }

    private func reload() async {
        let result = await state.runCLICommand(["logins", "list", "--json"])
        if let rows = decode([SavedLogin].self, key: "logins", from: result.output) {
            logins = rows
        } else if result.code != 0 {
            report("Saved logins could not be loaded.", failed: true)
        }
    }

    private func add() async {
        busy = true
        let secret = password
        password = ""
        let result = await state.runCLICommand(
            ["logins", "add", "--site", site, "--username", username, "--json"], input: secret + "\n"
        )
        busy = false
        if result.code == 0 {
            report("Saved login for \(site).", failed: false)
            site = ""
            username = ""
            await reload()
        } else {
            report((json(result.output)?["message"] as? String) ?? "The login could not be saved.", failed: true)
        }
    }

    private func remove(_ login: SavedLogin) async {
        busy = true
        let result = await state.runCLICommand(["logins", "remove", login.id, "--json"])
        busy = false
        report(result.code == 0 ? "Removed the login for \(login.site)." : "The login could not be removed.", failed: result.code != 0)
        await reload()
    }

    private func chooseExport() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.commaSeparatedText]
        panel.allowsMultipleSelection = false
        panel.message = "Choose the file exported from the Passwords app"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        Task { await preview(url.path) }
    }

    private func preview(_ path: String) async {
        busy = true
        let result = await state.runCLICommand(["logins", "import-preview", path, "--json"])
        busy = false
        guard let items = decode([LoginImportItem].self, key: "items", from: result.output) else {
            report((json(result.output)?["message"] as? String) ?? "That file is not a Passwords export.", failed: true)
            return
        }
        importPath = path
        importItems = items
        importSelected = []
        message = nil
    }

    private func runImport() async {
        guard let path = importPath else { return }
        busy = true
        var args = ["logins", "import", path, "--select", importSelected.sorted().map(String.init).joined(separator: ","), "--json"]
        if deleteExport { args.append("--delete-file") }
        let result = await state.runCLICommand(args)
        busy = false
        if let object = json(result.output), result.code == 0 {
            let imported = object["imported"] as? Int ?? 0
            let deleted = object["file_deleted"] as? Bool ?? false
            report("Imported \(imported) login(s)." + (deleted ? " The export file was deleted." : ""), failed: false)
            resetImport()
            await reload()
        } else {
            report("The logins could not be imported.", failed: true)
        }
    }

    private func resetImport() {
        importPath = nil
        importItems = []
        importSelected = []
    }
}
