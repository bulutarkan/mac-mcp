import AppKit
import Foundation

/// Runs saved recipes from macmcp://recipe/run?id=rcp_…&param=value links
/// (Shortcuts "Open URL", Raycast, Spotlight). Any app or web page can open such a
/// link, so every run is shown to the person first and needs an explicit Run.
@MainActor
final class RecipeLauncher {
    static let shared = RecipeLauncher()

    struct LaunchRequest: Equatable {
        let recipeID: String
        let values: [String: String]
    }

    struct RecipeSummary: Decodable {
        let recipeID: String
        let name: String?
        let consequential: Bool?

        enum CodingKeys: String, CodingKey {
            case name, consequential
            case recipeID = "recipe_id"
        }
    }

    struct RecipeListEnvelope: Decodable {
        let recipes: [RecipeSummary]
    }

    struct RunEnvelope: Decodable {
        let ok: Bool
        let status: String?
        let name: String?
        let message: String?
        let stepsExecuted: Int?

        enum CodingKeys: String, CodingKey {
            case ok, status, name, message
            case stepsExecuted = "steps_executed"
        }
    }

    private var baseURL: () -> URL? = { nil }
    private var authorize: (inout URLRequest) -> Void = { _ in }

    func configure(baseURL: @escaping () -> URL?, authorize: @escaping (inout URLRequest) -> Void) {
        self.baseURL = baseURL
        self.authorize = authorize
    }

    /// Parses only macmcp://recipe/run with a well-formed id and at most 20 plain values.
    nonisolated static func parse(_ url: URL) -> LaunchRequest? {
        guard url.scheme?.lowercased() == "macmcp",
              url.host?.lowercased() == "recipe",
              url.path == "/run",
              let components = URLComponents(url: url, resolvingAgainstBaseURL: false)
        else { return nil }
        var recipeID: String?
        var values: [String: String] = [:]
        for item in components.queryItems ?? [] {
            if item.name == "id" {
                recipeID = item.value
            } else if item.name.range(of: "^[a-z][a-z0-9_]{0,31}$", options: .regularExpression) != nil {
                values[item.name] = item.value ?? ""
            } else {
                return nil
            }
        }
        guard let recipeID,
              recipeID.range(of: "^rcp_[0-9a-f]{12}$", options: .regularExpression) != nil,
              values.count <= 20,
              values.values.allSatisfy({ $0.count <= 2000 })
        else { return nil }
        return LaunchRequest(recipeID: recipeID, values: values)
    }

    func handle(_ url: URL) {
        guard let request = Self.parse(url) else {
            report(title: "Recipe link not valid", body: "Use macmcp://recipe/run?id=rcp_… with simple name=value parameters.")
            return
        }
        Task { await run(request) }
    }

    /// The active recipes, for the Shortcuts action's recipe picker.
    func recipes() async throws -> [RecipeSummary] {
        guard let base = baseURL() else { throw URLError(.cannotConnectToHost) }
        let list: RecipeListEnvelope = try await send(base.appendingPathComponent("dashboard/api/recipes"), body: nil)
        return list.recipes
    }

    /// The Shortcuts and Siri action "Run Mac MCP Recipe": the same confirmation as a link, and what happened.
    func runFromShortcut(_ recipeID: String) async -> String {
        await run(LaunchRequest(recipeID: recipeID, values: [:]), source: "A shortcut")
    }

    @discardableResult
    private func run(_ request: LaunchRequest, source: String = "A link") async -> String {
        guard let base = baseURL() else {
            return report(title: "Mac MCP is not configured", body: "Open Mac MCP and start the server, then try again.")
        }
        let summary: RecipeSummary
        do {
            let list: RecipeListEnvelope = try await send(base.appendingPathComponent("dashboard/api/recipes"), body: nil)
            guard let found = list.recipes.first(where: { $0.recipeID == request.recipeID }) else {
                return report(title: "Recipe not found", body: "No active recipe has the id \(request.recipeID).")
            }
            summary = found
        } catch {
            return report(title: "Mac MCP is not running", body: "Start Mac MCP, then run the recipe again.")
        }
        guard confirm(summary, values: request.values, source: source) else { return "Not run: you chose Cancel." }
        do {
            let result: RunEnvelope = try await send(
                base.appendingPathComponent("dashboard/api/recipes/run"),
                body: ["recipe_id": request.recipeID, "values": request.values]
            )
            let name = result.name ?? summary.name ?? request.recipeID
            if result.ok {
                return report(title: "Recipe completed", body: "\(name) finished (\(result.stepsExecuted ?? 0) steps).")
            } else if result.status == "approval_required" {
                return report(title: "Recipe needs approval", body: "\(name) is waiting for approval in Mac MCP.")
            } else {
                return report(title: "Recipe did not complete", body: "\(name): \(result.message ?? result.status ?? "failed")")
            }
        } catch {
            return report(title: "Recipe did not run", body: "Mac MCP could not run the recipe: \(error.localizedDescription)")
        }
    }

    private func confirm(_ recipe: RecipeSummary, values: [String: String], source: String = "A link") -> Bool {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = "Run “\(recipe.name ?? recipe.recipeID)”?"
        var lines = values.keys.sorted().map { "\($0): \(values[$0] ?? "")" }
        if lines.isEmpty { lines = ["No parameters."] }
        if recipe.consequential == true {
            lines.append("")
            lines.append("This recipe changes things on your Mac or in apps.")
        }
        alert.informativeText = "\(source) asked Mac MCP to run this saved recipe.\n\n" + lines.joined(separator: "\n")
        alert.addButton(withTitle: "Run")
        alert.addButton(withTitle: "Cancel")
        return alert.runModal() == .alertFirstButtonReturn
    }

    @discardableResult
    private func report(title: String, body: String) -> String {
        Task {
            if await AgentNotificationController.shared.scheduleMessage(title: title, body: body) { return }
            let alert = NSAlert()
            alert.messageText = title
            alert.informativeText = body
            alert.runModal()
        }
        return "\(title): \(body)"
    }

    private func send<T: Decodable>(_ url: URL, body: [String: Any]?) async throws -> T {
        var request = URLRequest(url: url)
        request.timeoutInterval = body == nil ? 5 : 120
        request.httpMethod = body == nil ? "GET" : "POST"
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        authorize(&request)
        if let body {
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw URLError(.badServerResponse)
        }
        return try JSONDecoder().decode(T.self, from: data)
    }
}

final class MenuAppDelegate: NSObject, NSApplicationDelegate {
    /// Opens Settings at a pane named by its link; set by the app once its state exists.
    @MainActor static var openSettings: ((String) -> Void)?

    /// macmcp://settings/<pane> (e.g. scheduled-tasks) only opens Settings, so any link may use it.
    nonisolated static func settingsPane(_ url: URL) -> String? {
        guard url.scheme?.lowercased() == "macmcp", url.host?.lowercased() == "settings" else { return nil }
        let name = url.path.trimmingCharacters(in: CharacterSet(charactersIn: "/")).lowercased()
        return name.isEmpty ? nil : name
    }

    func application(_ application: NSApplication, open urls: [URL]) {
        Task { @MainActor in
            for url in urls {
                if url.host?.lowercased() == "settings" {
                    if let pane = Self.settingsPane(url) { Self.openSettings?(pane) }
                    continue
                }
                RecipeLauncher.shared.handle(url)
            }
        }
    }
}
