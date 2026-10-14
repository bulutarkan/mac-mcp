import AppKit
import SwiftUI

/// Settings › Privacy (#19): one place to pause Mac MCP, keep nothing, name apps and sites it must never
/// observe, and see what it keeps and where data can go. The server enforces every rule
/// (privacy_policy.py); this view only reads and changes them through the dashboard API.
struct PrivacyPane: View {
    @ObservedObject var state: AppState

    @MacMCPState private var paused = false
    @MacMCPState private var fileVault: Bool?
    @MacMCPState private var excludeFromBackups = false
    @MacMCPState private var blockedCategories: [String] = []
    @MacMCPState private var pendingDelete: [String: Any]?
    @MacMCPState private var deleting = false
    @MacMCPState private var privateMode = false
    @MacMCPState private var excludedApps: [String] = []
    @MacMCPState private var excludedSites: [String] = []
    @MacMCPState private var stores: [[String: Any]] = []
    @MacMCPState private var recipients: [[String: Any]] = []
    @MacMCPState private var newApp = ""
    @MacMCPState private var newSite = ""
    @MacMCPState private var loaded = false
    @MacMCPState private var message = ""

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("Privacy").font(.title2.weight(.semibold))
                    Text("What Mac MCP may look at, what it keeps, and where your data can go. Mac MCP enforces these rules itself; an AI app cannot turn them off.")
                        .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
                if !loaded, !message.isEmpty {
                    LoadProblem(message: message) { await load() }
                } else if !message.isEmpty {
                    Text(message).font(.caption).foregroundStyle(.secondary)
                }
                if !loaded, message.isEmpty {
                    ProgressView().controlSize(.small)
                }

                GroupBox {
                    VStack(alignment: .leading, spacing: 12) {
                        Toggle(isOn: Binding(get: { paused }, set: { save(["paused": $0]) })) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Pause observation").font(.subheadline.weight(.semibold))
                                Text("Mac MCP stops reading the screen, web pages, the clipboard and your voice, and starts no agents, until you turn this off. File and command tools keep working.")
                                    .font(.caption2).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                            }
                        }
                        Divider()
                        Toggle(isOn: Binding(get: { privateMode }, set: { save(["private_mode": $0]) })) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Private mode").font(.subheadline.weight(.semibold))
                                Text("Work continues, but Mac MCP keeps nothing about it: no activity history, usage, memories, lessons or run timelines. The security audit log still records tool names and outcomes.")
                                    .font(.caption2).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                    .padding(6)
                }
                .disabled(!loaded)

                exclusionBox(
                    title: "Never observe these apps",
                    detail: "No screenshot, window read or action in them. A whole-screen capture is refused while one is on screen. Use the app's name or bundle id.",
                    items: excludedApps,
                    text: $newApp,
                    placeholder: "App name, e.g. 1Password",
                    suggestions: runningApps(),
                    key: "excluded_apps"
                )
                GroupBox {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("Also never observe").font(.subheadline.weight(.semibold))
                        Text("Mac MCP recognizes these by name and address. Recognition can miss some, so add the ones that matter above too.")
                            .font(.caption2).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                        categoryToggle("password_manager", "Password managers")
                        categoryToggle("finance", "Banking and finance sites")
                        categoryToggle("health", "Health portals")
                    }
                    .padding(6)
                }
                .disabled(!loaded)

                exclusionBox(
                    title: "Never observe these sites",
                    detail: "Their pages are never read, captured or acted in, including subdomains. Tab lists show them without title or address.",
                    items: excludedSites,
                    text: $newSite,
                    placeholder: "Site, e.g. mybank.com",
                    suggestions: [],
                    key: "excluded_sites"
                )

                GroupBox("What Mac MCP keeps on this Mac") {
                    VStack(alignment: .leading, spacing: 0) {
                        atRestRow
                        Divider()
                        ForEach(Array(stores.enumerated()), id: \.offset) { index, store in
                            if index > 0 { Divider() }
                            storeRow(store)
                        }
                    }
                    .padding(.vertical, 4)
                }

                HStack {
                    Spacer()
                    Button("Move all Mac MCP data to the Trash…") {
                        pendingDelete = ["key": "all", "name": "all Mac MCP data"]
                    }
                    .disabled(!loaded || deleting)
                }

                GroupBox("Where your data can go") {
                    VStack(alignment: .leading, spacing: 0) {
                        ForEach(Array(recipients.enumerated()), id: \.offset) { index, recipient in
                            if index > 0 { Divider() }
                            recipientRow(recipient)
                        }
                    }
                    .padding(.vertical, 4)
                }
            }
            .padding(22)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .task { await load() }
        .alert(
            "Move \(pendingDelete?["name"] as? String ?? "this") to the Trash?",
            isPresented: Binding(get: { pendingDelete != nil }, set: { if !$0 { pendingDelete = nil } })
        ) {
            Button("Move to Trash", role: .destructive) {
                if let key = pendingDelete?["key"] as? String { delete(key) }
                pendingDelete = nil
            }
            Button("Cancel", role: .cancel) { pendingDelete = nil }
        } message: {
            Text("Mac MCP stops for a moment, moves the files into one dated folder in the Trash, and starts again. Running agents and jobs stop. Nothing is erased until you empty the Trash.")
        }
    }

    private func categoryToggle(_ key: String, _ title: String) -> some View {
        Toggle(title, isOn: Binding(
            get: { blockedCategories.contains(key) },
            set: { on in save(["blocked_categories": on ? blockedCategories + [key] : blockedCategories.filter { $0 != key }]) }
        ))
        .font(.callout)
    }

    private func delete(_ key: String) {
        deleting = true
        message = "Moving to the Trash…"
        Task {
            let args = key == "all" ? ["privacy", "delete", "--all", "--yes", "--json"]
                : ["privacy", "delete", "--store", key, "--yes", "--json"]
            let result = await state.runCLICommand(args)
            let line = result.output.split(separator: "\n").last(where: { $0.hasPrefix("{") }).map(String.init) ?? ""
            let object = (try? JSONSerialization.jsonObject(with: Data(line.utf8))) as? [String: Any]
            let moved = (object?["moved"] as? [Any])?.count ?? 0
            let failed = (object?["failed"] as? [Any])?.count ?? 0
            message = object == nil ? "Couldn’t move the data. See Help & Diagnostics."
                : failed == 0 ? "Moved \(moved) item\(moved == 1 ? "" : "s") to the Trash (\((object?["folder"] as? String).map { URL(fileURLWithPath: $0).lastPathComponent } ?? "Trash"))."
                : "Moved \(moved), could not move \(failed). See Help & Diagnostics."
            deleting = false
            await load()
        }
    }

    private func exclusionBox(title: String, detail: String, items: [String], text: Binding<String>,
                              placeholder: String, suggestions: [String], key: String) -> some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 8) {
                Text(title).font(.subheadline.weight(.semibold))
                Text(detail).font(.caption2).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                if items.isEmpty {
                    Text("None").font(.caption).foregroundStyle(.secondary)
                }
                ForEach(items, id: \.self) { item in
                    HStack {
                        Text(item).font(.callout)
                        Spacer()
                        Button("Remove") { save([key: items.filter { $0 != item }]) }
                            .controlSize(.small)
                            .accessibilityLabel("Remove \(item)")
                    }
                }
                HStack {
                    TextField(placeholder, text: text)
                        .textFieldStyle(.roundedBorder)
                        .onSubmit { add(text, to: items, key: key) }
                    if !suggestions.isEmpty {
                        Menu("Running apps") {
                            ForEach(suggestions, id: \.self) { name in
                                Button(name) { save([key: items + [name]]) }
                            }
                        }
                        .fixedSize()
                    }
                    Button("Add") { add(text, to: items, key: key) }
                        .disabled(text.wrappedValue.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
            .padding(6)
        }
        .disabled(!loaded)
    }

    /// FileVault encrypts these stores on disk; Time Machine copies may not be encrypted.
    private var atRestRow: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label {
                Text(fileVault == true ? "FileVault is on: everything below is encrypted on disk."
                     : fileVault == false ? "FileVault is off: these files are not encrypted on disk. Turn it on in System Settings › Privacy & Security."
                     : "Couldn’t check FileVault.")
                    .font(.caption).fixedSize(horizontal: false, vertical: true)
            } icon: {
                Image(systemName: fileVault == true ? "lock.shield.fill" : "exclamationmark.shield")
                    .foregroundStyle(fileVault == true ? Color.green : Color.orange)
            }
            Toggle(isOn: Binding(get: { excludeFromBackups }, set: { save(["exclude_from_backups": $0]) })) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Keep Mac MCP data out of Time Machine").font(.callout.weight(.medium))
                    Text("Activity, memories, lessons, agent output, jobs and file history are left out of backups, which may not be encrypted. A restored Mac then starts without them.")
                        .font(.caption2).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .padding(.vertical, 6)
        .disabled(!loaded)
    }

    private func storeRow(_ store: [String: Any]) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: (store["owner_only"] as? Bool) == true ? "lock.fill" : "lock.open")
                .foregroundStyle((store["owner_only"] as? Bool) == true ? Color.green : Color.orange)
                .accessibilityLabel((store["owner_only"] as? Bool) == true ? "Only your account can read it" : "Not owner-only")
            VStack(alignment: .leading, spacing: 2) {
                Text(store["name"] as? String ?? "").font(.callout.weight(.medium))
                Text(store["holds"] as? String ?? "").font(.caption2).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 2) {
                Text((store["exists"] as? Bool) == true
                     ? ByteCountFormatter.string(fromByteCount: Int64(store["bytes"] as? Int ?? 0), countStyle: .file)
                     : "Empty").font(.caption.monospacedDigit())
                Text("Kept: \(store["kept"] as? String ?? "")").font(.caption2).foregroundStyle(.secondary)
                if (store["in_backups"] as? Bool) == false {
                    Text("Not in backups").font(.caption2).foregroundStyle(.secondary)
                }
            }
            if (store["deletable"] as? Bool) == true, (store["exists"] as? Bool) == true {
                Button {
                    pendingDelete = store
                } label: { Image(systemName: "trash") }
                    .buttonStyle(.borderless)
                    .help("Move to the Trash")
                    .disabled(deleting)
                    .accessibilityLabel("Move \(store["name"] as? String ?? "store") to the Trash")
            }
            if let path = (store["paths"] as? [String])?.first, (store["exists"] as? Bool) == true {
                Button {
                    NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
                } label: { Image(systemName: "folder") }
                    .buttonStyle(.borderless)
                    .help("Show in Finder")
                    .accessibilityLabel("Show \(store["name"] as? String ?? "store") in Finder")
            }
        }
        .padding(.vertical, 6)
    }

    private func recipientRow(_ recipient: [String: Any]) -> some View {
        let active = (recipient["active"] as? Bool) == true
        return HStack(alignment: .top, spacing: 10) {
            Image(systemName: active ? "arrow.up.right.circle.fill" : "circle.dashed")
                .foregroundStyle(active ? Color.accentColor : Color.secondary)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(recipient["name"] as? String ?? "").font(.callout.weight(.medium))
                Text(recipient["sends"] as? String ?? "").font(.caption2).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer()
            Text(active ? "On" : "Off").font(.caption).foregroundStyle(active ? .primary : .secondary)
        }
        .padding(.vertical, 6)
    }

    private func runningApps() -> [String] {
        let names = NSWorkspace.shared.runningApplications
            .filter { $0.activationPolicy == .regular }
            .compactMap(\.localizedName)
        return Array(Set(names).subtracting(excludedApps)).sorted()
    }

    private func add(_ text: Binding<String>, to items: [String], key: String) {
        let value = text.wrappedValue.trimmingCharacters(in: .whitespaces)
        guard !value.isEmpty else { return }
        text.wrappedValue = ""
        save([key: items + [value]])
    }

    private func apply(_ object: [String: Any]) {
        paused = object["paused"] as? Bool ?? false
        privateMode = object["private_mode"] as? Bool ?? false
        excludedApps = object["excluded_apps"] as? [String] ?? []
        excludedSites = object["excluded_sites"] as? [String] ?? []
        if let rows = object["stores"] as? [[String: Any]] { stores = rows }
        if object.keys.contains("filevault") { fileVault = object["filevault"] as? Bool }
        if let value = object["exclude_from_backups"] as? Bool { excludeFromBackups = value }
        if let value = object["blocked_categories"] as? [String] { blockedCategories = value }
        if let rows = object["recipients"] as? [[String: Any]] { recipients = rows }
    }

    private func load() async {
        do {
            if let object = try await state.dashboardJSON("dashboard/api/privacy", body: nil) as? [String: Any] {
                apply(object)
                loaded = true
                message = ""
            }
        } catch {
            message = "Start Mac MCP to see and change privacy settings."
        }
    }

    private func save(_ changes: [String: Any]) {
        Task {
            do {
                if let object = try await state.dashboardJSON("dashboard/api/privacy/settings", body: changes) as? [String: Any] {
                    apply(object)
                    message = "Saved · applies right away"
                    if changes.keys.contains("exclude_from_backups") { await load() }
                }
            } catch {
                message = "Couldn’t save. Is Mac MCP running?"
            }
        }
    }
}
