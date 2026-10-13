import Foundation
import ServiceManagement

/// While "Start Mac MCP when you log in" is on, the menu bar app is a login item, so its
/// icon is there as soon as the owner logs in; the supervisor job starts the server itself.
/// Only the installed app registers: a build tested from /tmp or an isolated install never
/// becomes a login item.
enum LoginItem {
    static let defaultBundleID = "com.bulutarkan.mac-mcp.menu"

    static func applies(bundleURL: URL, bundleID: String?, home: URL) -> Bool {
        guard bundleID == defaultBundleID else { return false }
        let installed = [
            home.appendingPathComponent("Applications/Mac MCP.app"),
            URL(fileURLWithPath: "/Applications/Mac MCP.app"),
        ]
        let path = bundleURL.standardizedFileURL.resolvingSymlinksInPath().path
        return installed.contains { $0.standardizedFileURL.resolvingSymlinksInPath().path == path }
    }

    /// Register or unregister this app to match the setting. Returns false when macOS refused.
    @discardableResult
    static func sync(enabled: Bool, bundle: Bundle = .main) -> Bool {
        let home = FileManager.default.homeDirectoryForCurrentUser
        guard applies(bundleURL: bundle.bundleURL, bundleID: bundle.bundleIdentifier, home: home) else { return true }
        let service = SMAppService.mainApp
        do {
            if enabled, service.status != .enabled {
                try service.register()
            } else if !enabled, service.status == .enabled {
                try service.unregister()
            }
            return true
        } catch {
            return false
        }
    }
}
