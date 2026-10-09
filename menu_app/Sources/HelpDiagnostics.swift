import AppKit
import Foundation
import SwiftUI
import UniformTypeIdentifiers

@MainActor
final class DiagnosticsCenter: ObservableObject {
    @Published private(set) var report: DoctorReport?
    @Published private(set) var running = false
    @Published private(set) var lastRun: Date?
    @Published private(set) var runError: String?
    @Published private(set) var exportedReport: URL?
    @Published private(set) var exportError: String?
    @Published var sheet: HelpSheet?

    struct HelpSheet: Identifiable, Equatable {
        let id = UUID()
        let title: String
        let text: String
    }

    func run(using state: AppState) async {
        guard !running else { return }
        running = true
        runError = nil
        let result = await state.runCLICommand(["doctor", "--json"])
        running = false
        lastRun = Date()
        if let parsed = DoctorReport.parse(result.output) {
            report = parsed
        } else {
            let firstLine = result.output.split(separator: "\n").first.map(String.init) ?? "no output"
            runError = "Diagnostics could not run (\(firstLine))."
        }
    }

    func export(to url: URL, using state: AppState) async {
        exportError = nil
        exportedReport = nil
        let result = await state.runCLICommand(["doctor", "--json", "--support-bundle", url.path])
        if FileManager.default.fileExists(atPath: url.path) {
            exportedReport = url
            if let parsed = DoctorReport.parse(result.output) { report = parsed }
        } else {
            exportError = "The support report could not be written."
        }
    }

    func showLog(_ component: String, using state: AppState) async {
        let result = await state.runCLICommand(["logs", component, "-n", "200"])
        let text = result.output.trimmingCharacters(in: .whitespacesAndNewlines)
        sheet = HelpSheet(title: "Recent \(component) log", text: text.isEmpty ? "The log is empty." : text)
    }

    func preview(_ url: URL) {
        let text = (try? String(contentsOf: url, encoding: .utf8)) ?? "The report could not be read."
        sheet = HelpSheet(title: url.lastPathComponent, text: text)
    }

    func restartAndRecheck(using state: AppState) async {
        _ = await state.restartServerAndWait()
        await run(using: state)
    }
}

struct HelpDiagnosticsPane: View {
    @ObservedObject var state: AppState
    @ObservedObject var settings: SettingsStore
    @ObservedObject var center: DiagnosticsCenter
    let openPane: (String) -> Void

    private static let repository = "https://github.com/bulutarkan/mac-mcp"

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("Help & Diagnostics").font(.title2.weight(.semibold))
                    Text("Check this Mac, fix what is wrong, and prepare a private report if you need help.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                diagnosticsBox
                permissionsBox
                supportBox
                logsBox
                versionBox
            }
            .padding(22)
        }
        .task {
            if center.report == nil { await center.run(using: state) }
        }
        .sheet(item: $center.sheet) { sheet in
            VStack(alignment: .leading, spacing: 10) {
                Text(sheet.title).font(.headline)
                ScrollView {
                    Text(sheet.text)
                        .font(.system(.caption, design: .monospaced))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                HStack {
                    Button("Copy") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(sheet.text, forType: .string)
                    }
                    Spacer()
                    Button("Close") { center.sheet = nil }.keyboardShortcut(.defaultAction)
                }
            }
            .padding(18)
            .frame(width: 640, height: 460)
        }
    }

    // MARK: Diagnostics

    private var diagnosticsBox: some View {
        GroupBox("Diagnostics") {
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 10) {
                    verdict
                    Spacer()
                    if center.running { ProgressView().controlSize(.small) }
                    Button(center.report == nil ? "Run Diagnostics" : "Recheck") {
                        Task { await center.run(using: state) }
                    }
                    .disabled(center.running)
                }
                if let error = center.runError {
                    Label(error, systemImage: "exclamationmark.triangle.fill")
                        .font(.caption).foregroundStyle(.red)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let report = center.report {
                    ForEach(report.problems) { check in
                        Divider()
                        checkRow(check)
                    }
                    if !report.passed.isEmpty {
                        Divider()
                        DisclosureGroup("Passed and informational checks (\(report.passed.count))") {
                            VStack(alignment: .leading, spacing: 5) {
                                ForEach(report.passed) { check in
                                    Label(check.summary, systemImage: check.status == "pass" ? "checkmark.circle" : "info.circle")
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                            }
                            .padding(.top, 4)
                        }
                        .font(.caption)
                    }
                }
            }
            .padding(.top, 5)
        }
    }

    @ViewBuilder
    private var verdict: some View {
        if let report = center.report {
            let (text, symbol, color): (String, String, Color) = {
                if report.ok { return ("Everything looks good", "checkmark.seal.fill", .green) }
                if report.health == "degraded" { return ("Mac MCP works locally; the public endpoint needs attention", "exclamationmark.triangle.fill", .orange) }
                return ("\(report.problems.count) issue\(report.problems.count == 1 ? "" : "s") found", "exclamationmark.octagon.fill", .red)
            }()
            VStack(alignment: .leading, spacing: 2) {
                Label(text, systemImage: symbol).font(.subheadline.weight(.semibold)).foregroundStyle(color)
                if let lastRun = center.lastRun {
                    Text("Checked \(lastRun.formatted(date: .omitted, time: .shortened))")
                        .font(.caption2).foregroundStyle(.secondary)
                }
            }
        } else {
            Text(center.running ? "Checking this Mac…" : "Diagnostics have not run yet.")
                .font(.subheadline).foregroundStyle(.secondary)
        }
    }

    private func checkRow(_ check: DoctorCheck) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: check.status == "fail" ? "xmark.octagon.fill" : "exclamationmark.triangle.fill")
                .foregroundStyle(check.status == "fail" ? Color.red : Color.orange)
                .accessibilityLabel(check.status == "fail" ? "Failed" : "Warning")
            VStack(alignment: .leading, spacing: 4) {
                Text(check.summary).font(.subheadline.weight(.medium)).fixedSize(horizontal: false, vertical: true)
                if let remediation = check.remediation {
                    Text(remediation).font(.caption).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }
            }
            Spacer(minLength: 8)
            if let recovery = check.details?.recovery {
                recoveryButton(recovery)
            }
        }
    }

    @ViewBuilder
    private func recoveryButton(_ recovery: DoctorRecovery) -> some View {
        switch recovery.action {
        case "restart":
            Button("Restart Mac MCP") { Task { await center.restartAndRecheck(using: state) } }
                .disabled(state.busyAction != nil || center.running)
        case "open_settings":
            Button(recovery.pane == "advanced" ? "Open Advanced" : "Open Connections") {
                openPane(recovery.pane ?? "connections")
            }
        case "view_logs":
            Button("View Log") { Task { await center.showLog(recovery.log ?? "server", using: state) } }
        case "open_system_settings":
            if let raw = recovery.url, let url = URL(string: raw) {
                Button("Open System Settings") { NSWorkspace.shared.open(url) }
            }
        default:
            EmptyView()
        }
    }

    // MARK: Permissions

    private var permissionsBox: some View {
        GroupBox("macOS Permissions") {
            VStack(alignment: .leading, spacing: 10) {
                if let report = center.report, !report.permissions.isEmpty {
                    if report.permissions.first?.details?.context == "doctor_process" {
                        Label("Mac MCP is not running, so these describe the app itself rather than the server. Start Mac MCP and recheck.",
                              systemImage: "info.circle")
                            .font(.caption).foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    ForEach(report.permissions) { check in
                        permissionRow(check)
                        if check.id != report.permissions.last?.id { Divider() }
                    }
                } else {
                    Text("Run diagnostics to see which permissions Mac MCP has.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            .padding(.top, 5)
        }
    }

    private func permissionRow(_ check: DoctorCheck) -> some View {
        let name = permissionTitle(check.checkID)
        let (label, color) = permissionBadge(check)
        return HStack(alignment: .top, spacing: 10) {
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(name).font(.subheadline.weight(.semibold))
                    Text(label)
                        .font(.caption2.weight(.semibold))
                        .padding(.horizontal, 6).padding(.vertical, 2)
                        .background(color.opacity(0.15), in: Capsule())
                        .foregroundStyle(color)
                }
                if let features = check.details?.features, !features.isEmpty {
                    Text("Needed for " + features.joined(separator: ", ") + ".")
                        .font(.caption).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if check.status != "pass" {
                    Text(check.remediation ?? check.summary)
                        .font(.caption).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }
            }
            Spacer(minLength: 8)
            if check.status != "pass", let raw = check.details?.recovery?.url, let url = URL(string: raw) {
                Button("Open System Settings") { NSWorkspace.shared.open(url) }
            }
        }
    }

    private func permissionTitle(_ id: String) -> String {
        switch id {
        case "permissions.accessibility": return "Accessibility"
        case "permissions.screen_recording": return "Screen Recording"
        case "permissions.automation": return "Automation"
        case "permissions.microphone": return "Microphone"
        default: return id
        }
    }

    private func permissionBadge(_ check: DoctorCheck) -> (String, Color) {
        switch check.details?.state {
        case "granted": return ("Allowed", .green)
        case "denied": return ("Not allowed", .red)
        case "not_determined": return ("Not asked yet", .secondary)
        default:
            return check.status == "pass" ? ("Allowed", .green) : ("Not checked", .secondary)
        }
    }

    // MARK: Support report

    private var supportBox: some View {
        GroupBox("Support Report") {
            VStack(alignment: .leading, spacing: 10) {
                Text("A report holds the diagnostic results above plus your macOS and Python versions. It never includes .env values, settings values, logs, credentials, cookies, prompts or chat content. Nothing is sent anywhere: you decide whether to attach it.")
                    .font(.caption).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                HStack(spacing: 8) {
                    Button("Export Support Report…") { exportReport() }
                    if let url = center.exportedReport {
                        Button("Preview") { center.preview(url) }
                        Button("Show in Finder") { NSWorkspace.shared.activateFileViewerSelecting([url]) }
                    }
                    Spacer()
                }
                if let url = center.exportedReport {
                    Label("Saved to \(url.path)", systemImage: "checkmark.circle.fill")
                        .font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                }
                if let error = center.exportError {
                    Label(error, systemImage: "exclamationmark.triangle.fill").font(.caption).foregroundStyle(.red)
                }
            }
            .padding(.top, 5)
        }
    }

    private func exportReport() {
        let panel = NSSavePanel()
        let stamp = ISO8601DateFormatter().string(from: Date()).prefix(10)
        panel.nameFieldStringValue = "mac-mcp-support-\(stamp).json"
        panel.allowedContentTypes = [.json]
        panel.canCreateDirectories = true
        guard panel.runModal() == .OK, let url = panel.url else { return }
        Task { await center.export(to: url, using: state) }
    }

    // MARK: Logs

    private var logsBox: some View {
        GroupBox("Logs") {
            VStack(alignment: .leading, spacing: 8) {
                Text("Shows the last 200 lines with known tokens and credentials redacted. Logs are never added to a support report.")
                    .font(.caption).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                HStack(spacing: 8) {
                    Button("Server Log") { Task { await center.showLog("server", using: state) } }
                    if settings.publicEndpointMode == "cloudflare" {
                        Button("Cloudflare Tunnel Log") { Task { await center.showLog("cloudflared", using: state) } }
                    } else if settings.publicEndpointMode == "ngrok" {
                        Button("ngrok Log") { Task { await center.showLog("ngrok", using: state) } }
                    }
                    Spacer()
                }
            }
            .padding(.top, 5)
        }
    }

    // MARK: Version and links

    private var versionInfo: String {
        var parts = ["Mac MCP \(state.version == "—" ? "unknown version" : "v" + state.version)"]
        if let info = state.updateCheckInfo {
            parts.append("commit \(info.deployedShort)")
            if let release = info.releaseID, !release.isEmpty { parts.append("latest verified release \(release)") }
        }
        parts.append("macOS \(ProcessInfo.processInfo.operatingSystemVersionString)")
        return parts.joined(separator: " · ")
    }

    private var versionBox: some View {
        GroupBox("Version & Help") {
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 8) {
                    Text(versionInfo).font(.caption).textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer()
                    Button("Copy Version Info") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(versionInfo, forType: .string)
                    }
                }
                Divider()
                HStack(spacing: 8) {
                    Button("Documentation") { open("\(Self.repository)#readme") }
                    Button("Report a Problem") { open("\(Self.repository)/issues/new/choose") }
                    Button("Report a Security Issue") { open("\(Self.repository)/security/policy") }
                    Spacer()
                }
                Text("These open GitHub in your browser; nothing is submitted for you. Never paste tokens or full logs into a public issue, and report vulnerabilities privately.")
                    .font(.caption2).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(.top, 5)
        }
    }

    private func open(_ raw: String) {
        if let url = URL(string: raw) { NSWorkspace.shared.open(url) }
    }
}
