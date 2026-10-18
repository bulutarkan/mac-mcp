import AppKit
import SwiftUI

/// Settings › Apps › Reading Messages (#261). macOS keeps Messages history behind Full Disk Access;
/// Mac MCP reads it through Mac MCP Messages Reader, a helper inside this app, so the owner grants
/// that access to the helper alone. The state comes from /dashboard/api/apps/messages-access,
/// which asks the helper to open the database and reads no message.
struct MessagesAccessSection: View {
    @ObservedObject var state: AppState

    @MacMCPState private var access: String?
    @MacMCPState private var checking = false

    private var readerURL: URL {
        Bundle.main.bundleURL.appendingPathComponent("Contents/Helpers/Mac MCP Messages Reader.app")
    }

    var body: some View {
        GroupBox("Reading Messages") {
            VStack(alignment: .leading, spacing: 10) {
                Text("Agents can read and search your Messages conversations when you ask them to. Reading only: nothing is sent, marked as read or deleted this way. macOS keeps Messages behind Full Disk Access, so Mac MCP reads it through Mac MCP Messages Reader, a small helper inside Mac MCP that opens only the Messages database. Give Full Disk Access to that helper, not to anything else.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)

                HStack(spacing: 8) {
                    if checking {
                        ProgressView().controlSize(.small)
                        Text("Checking…").font(.callout)
                    } else {
                        Image(systemName: access == "granted" ? "checkmark.circle.fill" : "exclamationmark.circle")
                            .foregroundStyle(access == "granted" ? Color.green : Color.orange)
                            .accessibilityHidden(true)
                        Text(statusText).font(.callout)
                    }
                    Spacer()
                    Button("Check Again") { Task { await check() } }.disabled(checking)
                }

                if access != "granted" && access != nil {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("1. Open Full Disk Access.")
                        Text("2. Drag Mac MCP Messages Reader from the Finder window into the list, or click + and choose it.")
                        Text("3. Turn it on, then click Check Again.")
                    }
                    .font(.caption)
                    .fixedSize(horizontal: false, vertical: true)
                }

                HStack(spacing: 10) {
                    Button("Open Full Disk Access") {
                        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles") {
                            NSWorkspace.shared.open(url)
                        }
                    }
                    Button("Show Messages Reader in Finder") {
                        NSWorkspace.shared.activateFileViewerSelecting([readerURL])
                    }
                    .disabled(!FileManager.default.fileExists(atPath: readerURL.path))
                }
                Text("To stop it, turn Mac MCP Messages Reader off in Full Disk Access. Excluding Messages in Settings › Privacy stops agents from reading it too.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.top, 5)
        }
        .task { await check() }
    }

    private var statusText: String {
        switch access {
        case "granted": return "Agents can read Messages."
        case "needs_full_disk_access": return "Off: Mac MCP Messages Reader has no Full Disk Access."
        case "reader_missing": return "Mac MCP Messages Reader is missing; reinstall Mac MCP."
        case nil: return "Start Mac MCP to check."
        default: return "Mac MCP could not check Messages access."
        }
    }

    private func check() async {
        checking = true
        let result = await state.taskAPI("dashboard/api/apps/messages-access")
        checking = false
        access = result.ok ? (result.object["state"] as? String ?? "error") : nil
    }
}
