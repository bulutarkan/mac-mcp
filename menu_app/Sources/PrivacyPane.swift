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
    @MacMCPState private var runDetailDays = 30
    @MacMCPState private var runDetailChoices: [Int] = [7, 30, 90]
    @MacMCPState private var message = ""
    @MacMCPState private var knowledgeFolders: [[String: Any]] = []
    @MacMCPState private var knowledgePending = 0
    @MacMCPState private var knowledgeMessage = ""
    @MacMCPState private var knowledgeBusy = false

    @ObservedObject private var tabs = SettingsTabs.shared

    var body: some View {
        VStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 13) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("Privacy").font(.title2.weight(.semibold))
                    Text("What Mac MCP may look at, what it keeps, and where your data can go. Mac MCP enforces these rules itself; an AI app cannot turn them off.")
                        .font(SettingsType.note).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
                SettingsTabPicker(selection: $tabs.privacy, label: "Privacy section")
            }
            .padding(.horizontal, 22)
            .padding(.top, 22)
            .padding(.bottom, 12)
            .frame(maxWidth: .infinity, alignment: .leading)

            Divider()

            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    if !loaded, !message.isEmpty {
                        LoadProblem(message: message) { await load() }
                    } else if !message.isEmpty {
                        Text(message).font(SettingsType.note).foregroundStyle(.secondary)
                    }
                    if !loaded, message.isEmpty {
                        ProgressView().controlSize(.small)
                    }


                    switch tabs.privacy {
                    case .protection:
                        GroupBox {
                            VStack(alignment: .leading, spacing: 0) {
                                switchRow("Pause observation",
                                          "Mac MCP stops reading the screen, web pages, the clipboard and your voice, and starts no agents, until you turn this off. File and command tools keep working.",
                                          isOn: Binding(get: { paused }, set: { save(["paused": $0]) }),
                                          on: "Paused: nothing is observed and no agent starts.")
                                Divider().padding(.vertical, 10)
                                switchRow("Private mode",
                                          "Work continues, but Mac MCP keeps nothing about it: no activity history, usage, memories, lessons or run timelines. The security audit log still records tool names and outcomes.",
                                          isOn: Binding(get: { privateMode }, set: { save(["private_mode": $0]) }),
                                          on: "On: nothing about the work is kept.")
                            }
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
                        GroupBox("Also never observe") {
                            VStack(alignment: .leading, spacing: 0) {
                                Text("Mac MCP recognizes these by name and address. Recognition can miss some, so add the ones that matter above too.")
                                    .font(SettingsType.note).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                                    .padding(.bottom, 8)
                                categoryToggle("password_manager", "Password managers", symbol: "key.fill")
                                Divider().padding(.vertical, 6)
                                categoryToggle("finance", "Banking and finance sites", symbol: "building.columns.fill")
                                Divider().padding(.vertical, 6)
                                categoryToggle("health", "Health portals", symbol: "cross.case.fill")
                            }
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
                    case .documents:
                        knowledgeBox
                    case .data:
                        if !stores.isEmpty { storageSummary }

                        GroupBox("What Mac MCP keeps on this Mac") {
                            VStack(alignment: .leading, spacing: 0) {
                                atRestRow
                                Divider()
                                ForEach(Array(sortedStores.enumerated()), id: \.offset) { index, store in
                                    if index > 0 { Divider() }
                                    storeRow(store, color: storeColor(index))
                                }
                                Divider()
                                HStack(alignment: .center, spacing: 16) {
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text("Scheduled task run details").font(SettingsType.body)
                                        Text("Results, output and each step’s text. How a run ended, when, and its usage stay for 90 days.")
                                            .font(SettingsType.note).foregroundStyle(.secondary)
                                            .fixedSize(horizontal: false, vertical: true)
                                    }
                                    Spacer()
                                    Picker("Keep run details", selection: Binding(get: { runDetailDays }, set: { days in
                                        runDetailDays = days
                                        save(["run_detail_retention_days": days])
                                    })) {
                                        ForEach(runDetailChoices, id: \.self) { days in Text("\(days) days").tag(days) }
                                    }
                                    .labelsHidden()
                                    .fixedSize()
                                    .disabled(!loaded)
                                }
                                .padding(.vertical, 10)
                                Divider()
                                HStack(spacing: 16) {
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text("Move everything to the Trash").font(SettingsType.body)
                                        Text("All of the above in one dated folder; nothing is erased until you empty the Trash.")
                                            .font(SettingsType.note).foregroundStyle(.secondary)
                                            .fixedSize(horizontal: false, vertical: true)
                                    }
                                    Spacer()
                                    Button("Move all Mac MCP data to the Trash…") {
                                        pendingDelete = ["key": "all", "name": "all Mac MCP data"]
                                    }
                                    .disabled(!loaded || deleting)
                                }
                                .padding(.top, 10)
                            }
                        }

                        GroupBox("Where your data can go") {
                            VStack(alignment: .leading, spacing: 0) {
                                ForEach(Array(sortedRecipients.enumerated()), id: \.offset) { index, recipient in
                                    if index > 0 { Divider() }
                                    recipientRow(recipient)
                                }
                            }
                        }
                    }
                }
                .padding(22)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .id(tabs.privacy)  // each tab opens at its top
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

    /// Folders Mac MCP can search (#197): the owner picks them; agents answer from them with sources.
    private var knowledgeBox: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline) {
                Text("Folders Mac MCP can search").font(SettingsType.title)
                Spacer()
                Button("Add Folder…") { chooseKnowledgeFolder() }
                    .disabled(!loaded || knowledgeBusy)
            }
            VStack(alignment: .leading, spacing: 0) {
                if knowledgeFolders.isEmpty {
                    HStack(spacing: 12) {
                        Image(systemName: "folder.badge.plus").font(.system(size: 18)).foregroundStyle(.secondary)
                            .accessibilityHidden(true)
                        Text("No folders yet. Add one, such as Documents/Contracts, and your AI apps can answer from it with the file and page.")
                            .font(SettingsType.note).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    }
                    .padding(.horizontal, 14).padding(.vertical, 12)
                }
                ForEach(Array(knowledgeFolders.enumerated()), id: \.offset) { index, folder in
                    if index > 0 { Divider() }
                    knowledgeRow(folder)
                }
            }
            .usageListBox()
            Text("Your AI apps can ask about what is in these folders — contracts, invoices, notes, PDFs and Word files, scans and photos of pages too — and answer with the file and page. This only adds smart search: Mac MCP still works with files anywhere else, as before. It reads these folders on this Mac and keeps a private index; nothing is uploaded.")
                .font(SettingsType.note).foregroundStyle(.tertiary).fixedSize(horizontal: false, vertical: true)
            if knowledgePending > 0 {
                Label("Reading \(knowledgePending) new or changed file\(knowledgePending == 1 ? "" : "s")…", systemImage: "arrow.triangle.2.circlepath")
                    .font(SettingsType.note).foregroundStyle(.secondary)
            }
            if !knowledgeMessage.isEmpty {
                Text(knowledgeMessage).font(SettingsType.note).foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .disabled(!loaded)
    }

    private func knowledgeRow(_ folder: [String: Any]) -> some View {
        let path = folder["path"] as? String ?? ""
        let files = folder["files"] as? Int ?? 0
        let indexed = folder["indexed"] as? Int ?? 0
        let noText = folder["no_text"] as? Int ?? 0
        var detail = files == 0 ? "Not read yet" : "\(UsageFormat.exact(indexed)) of \(UsageFormat.count(files, "file")) searchable"
        if noText > 0 { detail += ", \(UsageFormat.exact(noText)) without text (scanned)" }
        return HStack(spacing: 12) {
            Image(nsImage: NSWorkspace.shared.icon(forFile: path)).resizable().frame(width: 28, height: 28)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 3) {
                Text(URL(fileURLWithPath: path).lastPathComponent).font(SettingsType.body.weight(.medium))
                Text((path as NSString).abbreviatingWithTildeInPath)
                    .font(SettingsType.meta).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
            }
            .frame(minWidth: 130, maxWidth: 210, alignment: .leading)
            VStack(alignment: .leading, spacing: 4) {
                UsageShareMeter(share: files == 0 ? 0 : Double(indexed) / Double(files), color: Color.accentColor, height: 5)
                Text(detail).font(SettingsType.meta).foregroundStyle(.secondary).lineLimit(1)
            }
            Button {
                NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
            } label: { Image(systemName: "folder") }
                .buttonStyle(.borderless)
                .help("Show in Finder")
                .accessibilityLabel("Show \(URL(fileURLWithPath: path).lastPathComponent) in Finder")
            Button("Remove") { changeKnowledge(["action": "remove", "path": path]) }
                .disabled(knowledgeBusy)
                .accessibilityLabel("Stop searching \(URL(fileURLWithPath: path).lastPathComponent)")
        }
        .padding(.horizontal, 14).padding(.vertical, 10)
    }

    private func chooseKnowledgeFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.prompt = "Add"
        panel.message = "Choose a folder Mac MCP may search, such as Documents/Contracts."
        guard panel.runModal() == .OK, let url = panel.url else { return }
        changeKnowledge(["action": "add", "path": url.path])
    }

    private func changeKnowledge(_ body: [String: Any]) {
        knowledgeBusy = true
        Task {
            let result = await state.taskAPI("dashboard/api/knowledge/folders", body: body)
            if result.ok {
                applyKnowledge(result.object)
                knowledgeMessage = ""
                await load()  // the stores list shows the index size
            } else {
                knowledgeMessage = result.object["message"] as? String ?? "Couldn’t change the folders."
            }
            knowledgeBusy = false
        }
    }

    private func applyKnowledge(_ object: [String: Any]) {
        knowledgeFolders = object["folders"] as? [[String: Any]] ?? []
        knowledgePending = object["pending_files"] as? Int ?? 0
    }

    /// A switch with what it does; while it is on, a line says so in orange.
    private func switchRow(_ title: String, _ detail: String, isOn: Binding<Bool>, on: String) -> some View {
        HStack(alignment: .center, spacing: 16) {
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(SettingsType.title)
                Text(detail).font(SettingsType.note).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                if isOn.wrappedValue {
                    Label(on, systemImage: "circle.fill").font(SettingsType.note).foregroundStyle(.orange)
                        .labelStyle(DotLabelStyle())
                }
            }
            Spacer()
            Toggle(title, isOn: isOn).labelsHidden().toggleStyle(.switch)
        }
    }

    private func categoryToggle(_ key: String, _ title: String, symbol: String) -> some View {
        HStack(spacing: 12) {
            Image(systemName: symbol).font(.system(size: 13)).foregroundStyle(.secondary).frame(width: 20)
                .accessibilityHidden(true)
            Text(title).font(SettingsType.body)
            Spacer()
            Toggle(title, isOn: Binding(
                get: { blockedCategories.contains(key) },
                set: { on in save(["blocked_categories": on ? blockedCategories + [key] : blockedCategories.filter { $0 != key }]) }
            ))
            .labelsHidden()
            .toggleStyle(.switch)
        }
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

    /// Apps or sites Mac MCP must never observe: each one with its icon, then a field to add another.
    private func exclusionBox(title: String, detail: String, items: [String], text: Binding<String>,
                              placeholder: String, suggestions: [String], key: String) -> some View {
        GroupBox(title) {
            VStack(alignment: .leading, spacing: 0) {
                Text(detail).font(SettingsType.note).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    .padding(.bottom, 8)
                if items.isEmpty {
                    Text(key == "excluded_apps" ? "No apps yet." : "No sites yet.")
                        .font(SettingsType.note).foregroundStyle(.tertiary)
                        .padding(.bottom, 8)
                }
                ForEach(items, id: \.self) { item in
                    HStack(spacing: 10) {
                        exclusionIcon(item, app: key == "excluded_apps")
                        Text(item).font(SettingsType.body)
                        Spacer()
                        Button("Remove") { save([key: items.filter { $0 != item }]) }
                            .controlSize(.small)
                            .accessibilityLabel("Remove \(item)")
                    }
                    .padding(.vertical, 5)
                    Divider()
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
                .padding(.top, 8)
            }
        }
        .disabled(!loaded)
    }

    /// The app's own icon when it can be found by name or bundle id; a globe for a site.
    @ViewBuilder
    private func exclusionIcon(_ item: String, app: Bool) -> some View {
        if app, let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: item)
            ?? ["/Applications", "/System/Applications", "/Applications/Utilities"].lazy
                .map({ URL(fileURLWithPath: "\($0)/\(item).app") })
                .first(where: { FileManager.default.fileExists(atPath: $0.path) }) {
            Image(nsImage: NSWorkspace.shared.icon(forFile: url.path)).resizable().frame(width: 20, height: 20)
                .accessibilityHidden(true)
        } else {
            Image(systemName: app ? "app.dashed" : "globe").font(.system(size: 14)).foregroundStyle(.secondary)
                .frame(width: 20, height: 20)
                .accessibilityHidden(true)
        }
    }

    /// FileVault encrypts these stores on disk; Time Machine copies may not be encrypted.
    private var atRestRow: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label {
                Text(fileVault == true ? "FileVault is on: everything below is encrypted on disk."
                     : fileVault == false ? "FileVault is off: these files are not encrypted on disk. Turn it on in System Settings › Privacy & Security."
                     : "Couldn’t check FileVault.")
                    .font(SettingsType.note).fixedSize(horizontal: false, vertical: true)
            } icon: {
                Image(systemName: fileVault == true ? "lock.shield.fill" : "exclamationmark.shield")
                    .foregroundStyle(fileVault == true ? Color.green : Color.orange)
            }
            HStack(spacing: 16) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Keep Mac MCP data out of Time Machine").font(SettingsType.body)
                    Text("Activity, memories, lessons, agent output, jobs and file history are left out of backups, which may not be encrypted. A restored Mac then starts without them.")
                        .font(SettingsType.note).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
                Spacer()
                Toggle("Keep Mac MCP data out of Time Machine",
                       isOn: Binding(get: { excludeFromBackups }, set: { save(["exclude_from_backups": $0]) }))
                    .labelsHidden()
                    .toggleStyle(.switch)
            }
        }
        .padding(.bottom, 10)
        .disabled(!loaded)
    }

    // MARK: Your Data at a glance

    private func bytes(_ store: [String: Any]) -> Int { (store["exists"] as? Bool) == true ? store["bytes"] as? Int ?? 0 : 0 }

    /// The stores, largest first; empty ones last.
    private var sortedStores: [[String: Any]] {
        stores.enumerated().sorted { lhs, rhs in
            bytes(lhs.element) != bytes(rhs.element) ? bytes(lhs.element) > bytes(rhs.element) : lhs.offset < rhs.offset
        }.map(\.element)
    }

    private static let storePalette: [Color] = [
        UsageColors.adaptive(0x0A7CFF, 0x3D9BFF), UsageColors.adaptive(0x7B61E8, 0xA08CFF),
        UsageColors.adaptive(0x1F9E86, 0x3CC8A8), UsageColors.adaptive(0xD9774F, 0xF0956A),
        UsageColors.adaptive(0xB8901C, 0xE8C55A),
    ]

    /// The five largest stores have a colour of their own; the rest share grey, as "Other" in the bar.
    private func storeColor(_ index: Int) -> Color {
        index < Self.storePalette.count ? Self.storePalette[index] : UsageColors.adaptive(0x9A9FA8, 0x8A8F98)
    }

    /// Like macOS Storage: how much Mac MCP keeps, one bar split by store, and whether only you can read it.
    private var storageSummary: some View {
        let sorted = sortedStores
        let total = max(1, sorted.reduce(0) { $0 + bytes($1) })
        let top = Array(sorted.prefix(Self.storePalette.count)).filter { bytes($0) > 0 }
        let other = sorted.dropFirst(Self.storePalette.count).reduce(0) { $0 + bytes($1) }
        let open = stores.filter { ($0["owner_only"] as? Bool) != true && ($0["exists"] as? Bool) == true }.count
        return VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline, spacing: 5) {
                Text(UsageFormat.bytes(total)).font(.system(size: 34, weight: .semibold)).monospacedDigit()
                Text("kept on this Mac").font(.system(size: 15, weight: .medium)).foregroundStyle(.secondary)
            }
            Text(open == 0 ? "In \(UsageFormat.count(stores.count, "place")), each readable only by your account."
                 : "In \(UsageFormat.count(stores.count, "place")); \(UsageFormat.count(open, "place")) can be read by other accounts on this Mac.")
                .font(SettingsType.body).foregroundStyle(open == 0 ? AnyShapeStyle(.secondary) : AnyShapeStyle(Color.orange))
            GeometryReader { proxy in
                HStack(spacing: 2) {
                    ForEach(Array(top.enumerated()), id: \.offset) { index, store in
                        RoundedRectangle(cornerRadius: 3, style: .continuous).fill(storeColor(index))
                            .frame(width: max(4, (proxy.size.width - 12) * CGFloat(bytes(store)) / CGFloat(total)))
                    }
                    if other > 0 {
                        RoundedRectangle(cornerRadius: 3, style: .continuous).fill(storeColor(Self.storePalette.count))
                            .frame(maxWidth: .infinity)
                    }
                }
            }
            .frame(height: 12)
            .accessibilityHidden(true)
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 190), spacing: 16, alignment: .leading)], alignment: .leading, spacing: 6) {
                ForEach(Array(top.enumerated()), id: \.offset) { index, store in
                    legend(store["name"] as? String ?? "", bytes(store), storeColor(index))
                }
                if other > 0 { legend("Other", other, storeColor(Self.storePalette.count)) }
            }
            .font(.system(size: 12))
        }
        .padding(.bottom, 6)
    }

    private func legend(_ name: String, _ size: Int, _ color: Color) -> some View {
        HStack(spacing: 5) {
            RoundedRectangle(cornerRadius: 2).fill(color).frame(width: 8, height: 8)
            Text(name).foregroundStyle(.secondary).lineLimit(1)
            Text(UsageFormat.bytes(size)).fontWeight(.medium).monospacedDigit()
        }
        .accessibilityElement(children: .combine)
    }

    private func storeRow(_ store: [String: Any], color: Color) -> some View {
        let ownerOnly = (store["owner_only"] as? Bool) == true
        let exists = (store["exists"] as? Bool) == true
        return HStack(alignment: .center, spacing: 12) {
            RoundedRectangle(cornerRadius: 3, style: .continuous).fill(exists ? color : Color.primary.opacity(0.12))
                .frame(width: 10, height: 10)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Text(store["name"] as? String ?? "").font(SettingsType.body.weight(.medium))
                    if !ownerOnly && exists {
                        Image(systemName: "lock.open").font(.system(size: 11)).foregroundStyle(.orange)
                            .help("Other accounts on this Mac can read it")
                    }
                }
                Text(store["holds"] as? String ?? "").font(SettingsType.note).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .accessibilityElement(children: .combine)
            .accessibilityLabel("\(store["name"] as? String ?? ""), \(ownerOnly ? "only your account can read it" : "not owner-only")")
            Spacer()
            VStack(alignment: .trailing, spacing: 2) {
                Text(exists ? UsageFormat.bytes(store["bytes"] as? Int ?? 0) : "Empty")
                    .font(SettingsType.body.weight(.medium)).monospacedDigit()
                Text("Kept: \(store["kept"] as? String ?? "")").font(SettingsType.meta).foregroundStyle(.secondary)
                if (store["in_backups"] as? Bool) == false {
                    Text("Not in backups").font(SettingsType.meta).foregroundStyle(.secondary)
                }
            }
            HStack(spacing: 8) {
                if (store["deletable"] as? Bool) == true, exists {
                    Button {
                        pendingDelete = store
                    } label: { Image(systemName: "trash") }
                        .buttonStyle(.borderless)
                        .foregroundStyle(.secondary)
                        .help("Move to the Trash")
                        .disabled(deleting)
                        .accessibilityLabel("Move \(store["name"] as? String ?? "store") to the Trash")
                }
                if let path = (store["paths"] as? [String])?.first, exists {
                    Button {
                        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
                    } label: { Image(systemName: "folder") }
                        .buttonStyle(.borderless)
                        .foregroundStyle(.secondary)
                        .help("Show in Finder")
                        .accessibilityLabel("Show \(store["name"] as? String ?? "store") in Finder")
                }
            }
            .frame(width: 44, alignment: .trailing)
        }
        .padding(.vertical, 9)
    }

    /// The services data can go to, the ones in use first.
    private var sortedRecipients: [[String: Any]] {
        recipients.enumerated().sorted { lhs, rhs in
            let left = (lhs.element["active"] as? Bool) == true, right = (rhs.element["active"] as? Bool) == true
            return left != right ? left : lhs.offset < rhs.offset
        }.map(\.element)
    }

    private func recipientRow(_ recipient: [String: Any]) -> some View {
        let active = (recipient["active"] as? Bool) == true
        let key = recipient["key"] as? String ?? ""
        return HStack(alignment: .center, spacing: 12) {
            recipientIcon(key)
            VStack(alignment: .leading, spacing: 2) {
                Text(recipient["name"] as? String ?? "").font(SettingsType.body.weight(.medium))
                Text(recipient["sends"] as? String ?? "").font(SettingsType.note).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer()
            HStack(spacing: 6) {
                Circle().fill(active ? Color.green : Color.primary.opacity(0.2)).frame(width: 7, height: 7)
                Text(active ? "On" : "Off").font(SettingsType.note).foregroundStyle(active ? .primary : .secondary)
            }
            .accessibilityElement(children: .combine)
        }
        .padding(.vertical, 8)
        .opacity(active ? 1 : 0.75)
    }

    /// The agent's maker for an agent; a symbol for Mac MCP's own routes out.
    @ViewBuilder
    private func recipientIcon(_ key: String) -> some View {
        let symbols = ["ai_apps": "app.connected.to.app.below.fill", "public_endpoint": "globe",
                       "voice": "waveform", "decisions": "sparkles"]
        if let symbol = symbols[key] {
            UsageSymbolTile(symbol: symbol)
        } else {
            UsageAgentLogo(agent: key)
        }
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
        if let days = object["run_detail_retention_days"] as? Int { runDetailDays = days }
        if let choices = object["run_detail_retention_choices"] as? [Int], !choices.isEmpty { runDetailChoices = choices }
    }

    private func load() async {
        do {
            if let object = try await state.dashboardJSON("dashboard/api/privacy", body: nil) as? [String: Any] {
                apply(object)
                loaded = true
                message = ""
            }
            let knowledge = await state.taskAPI("dashboard/api/knowledge")
            if knowledge.ok { applyKnowledge(knowledge.object) }
        } catch {
            // The server can answer with an error; only a stopped server means "start it".
            message = state.serverRunning ? "Couldn’t load Privacy settings. Try again."
                : "Start Mac MCP to see and change privacy settings."
        }
    }

    private func save(_ changes: [String: Any]) {
        Task {
            do {
                if let object = try await state.dashboardJSON("dashboard/api/privacy/settings", body: changes) as? [String: Any] {
                    apply(object)
                    message = "Saved · applies right away"
                    if changes.keys.contains("exclude_from_backups") || changes.keys.contains("run_detail_retention_days") {
                        await load()  // the store list says how long things are kept
                    }
                }
            } catch {
                message = state.serverRunning ? "Couldn’t save this change. Try again."
                    : "Couldn’t save: Mac MCP isn’t running. Start it and try again."
            }
        }
    }
}
