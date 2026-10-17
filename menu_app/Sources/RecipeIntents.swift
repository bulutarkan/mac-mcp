import AppIntents
import Foundation

/// "Run Mac MCP Recipe" for Shortcuts and Siri (#195). It asks for the same confirmation as a
/// macmcp://recipe/run link before anything runs, so a shortcut can't run a recipe unseen.

struct RecipeEntity: AppEntity {
    static var typeDisplayRepresentation: TypeDisplayRepresentation = "Mac MCP Recipe"
    static var defaultQuery = RecipeQuery()

    let id: String
    let name: String

    var displayRepresentation: DisplayRepresentation { DisplayRepresentation(title: "\(name)") }
}

struct RecipeQuery: EntityQuery {
    func entities(for identifiers: [RecipeEntity.ID]) async throws -> [RecipeEntity] {
        try await suggestedEntities().filter { identifiers.contains($0.id) }
    }

    func suggestedEntities() async throws -> [RecipeEntity] {
        try await RecipeLauncher.shared.recipes().map { RecipeEntity(id: $0.recipeID, name: $0.name ?? $0.recipeID) }
    }
}

struct RunRecipeIntent: AppIntent {
    static var title: LocalizedStringResource = "Run Mac MCP Recipe"
    static var description = IntentDescription("Runs one of your saved Mac MCP recipes after you confirm it in Mac MCP.")

    @Parameter(title: "Recipe")
    var recipe: RecipeEntity

    static var parameterSummary: some ParameterSummary { Summary("Run \(\.$recipe) in Mac MCP") }

    @MainActor
    func perform() async throws -> some IntentResult & ReturnsValue<String> & ProvidesDialog {
        let outcome = await RecipeLauncher.shared.runFromShortcut(recipe.id)
        return .result(value: outcome, dialog: "\(outcome)")
    }
}

struct MacMCPShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(intent: RunRecipeIntent(), phrases: ["Run a recipe in \(.applicationName)"])
    }
}
