import SwiftUI

/// How a provider, model and thinking choice is offered and checked, shared by the Default Agent
/// card and each member of a team preset (Settings › Agents).
enum AgentChoice {
    /// What the app knows about the provider list while it has none to check against.
    enum ProvidersPhase: Equatable {
        case loading
        case failed(String)
        case loaded
    }

    /// The order providers are listed in everywhere in Agents settings.
    static let providerOrder = ["opencode", "codex", "claude", "kimi", "grok"] + AgentProviders.more

    static func sorted(_ ids: [String]) -> [String] {
        ids.sorted { (providerOrder.firstIndex(of: $0) ?? 99) < (providerOrder.firstIndex(of: $1) ?? 99) }
    }

    /// Providers that are on and found; a disabled or missing one is never offered (fail closed).
    static func usableProviders(_ providers: [ProviderInfo]) -> [String] {
        sorted(providers.filter { $0.enabled && $0.detected }.map(\.id))
    }

    /// The usable providers, plus the saved one so a picker can still show it after it went away.
    static func providerOptions(_ providers: [ProviderInfo], keeping current: String) -> [String] {
        var values = usableProviders(providers)
        if !current.isEmpty && !values.contains(current) { values.append(current) }
        return sorted(values)
    }

    static func isUsable(_ provider: String, in providers: [ProviderInfo]) -> Bool {
        providers.contains { $0.id == provider && $0.enabled && $0.detected }
    }

    static func providerName(_ id: String) -> String {
        AgentProviders.name(id) ?? id
    }

    /// The model a choice runs with: the one picked, else the catalog's default.
    static func selectedModel(_ model: String, in catalog: ProviderCatalogInfo?) -> AgentModelInfo? {
        guard let catalog else { return nil }
        let modelID = model.isEmpty ? (catalog.defaultModel ?? "") : model
        guard !modelID.isEmpty else { return nil }
        return catalog.modelItems.first { $0.id == modelID }
    }

    static func reasoningOptions(_ model: String, in catalog: ProviderCatalogInfo?) -> [String] {
        selectedModel(model, in: catalog)?.reasoningValues ?? []
    }

    static func reasoningDisplayName(_ value: String) -> String {
        switch value.lowercased() {
        case "xhigh": return "Extra High"
        case "extra-high": return "Extra High"
        case "max": return "Max"
        case "ultra": return "Ultra"
        default: return value.capitalized
        }
    }

    struct Status: Equatable {
        let label: String
        let detail: String
        let symbol: String
        let color: Color

        var isReady: Bool { label == "Ready" }
    }

    /// Whether a provider, model and thinking choice can run now, and what to fix when it cannot.
    /// `subject` names the choice in the text, such as "this default" or "this member".
    static func status(
        provider providerID: String,
        model: String,
        reasoning: String,
        providers: [ProviderInfo],
        catalogs: [String: ProviderCatalogInfo],
        phase: ProvidersPhase,
        subject: String
    ) -> Status {
        guard !providerID.isEmpty else {
            return Status(
                label: "Not configured",
                detail: "Delegated requests must keep passing a provider explicitly until a default is selected.",
                symbol: "minus.circle",
                color: .secondary
            )
        }
        let name = providerName(providerID)
        if providers.isEmpty {
            switch phase {
            case .loading:
                return Status(label: "Loading", detail: "Checking the delegated provider before validating \(subject).",
                              symbol: "arrow.clockwise", color: .secondary)
            case .failed(let message):
                return Status(label: "Unavailable", detail: message, symbol: "exclamationmark.triangle.fill", color: .orange)
            case .loaded:
                break
            }
        }
        guard let provider = providers.first(where: { $0.id == providerID }) else {
            return Status(label: "Unavailable", detail: "The saved provider is not present in the latest provider response.",
                          symbol: "exclamationmark.triangle.fill", color: .orange)
        }
        guard provider.enabled else {
            return Status(label: "Disabled", detail: "Enable \(name) before using \(subject).",
                          symbol: "pause.circle.fill", color: .orange)
        }
        guard provider.detected else {
            return Status(label: "Unavailable", detail: "\(name) CLI is not currently detected.",
                          symbol: "exclamationmark.triangle.fill", color: .orange)
        }
        let catalog = catalogs[providerID]
        guard let catalog, catalog.available else {
            return Status(label: "Unavailable", detail: "The live model catalog for \(name) is unavailable.",
                          symbol: "exclamationmark.triangle.fill", color: .orange)
        }
        if catalog.catalogFreshness == "stale" {
            return Status(label: "Stale", detail: "The provider model catalog is stale. Refresh the provider before spawning \(subject).",
                          symbol: "clock.badge.exclamationmark", color: .orange)
        }
        let selected = selectedModel(model, in: catalog)
        if !model.isEmpty && selected == nil {
            return Status(label: "Stale model", detail: "\(model) is no longer in the live provider catalog.",
                          symbol: "exclamationmark.triangle.fill", color: .orange)
        }
        if !reasoning.isEmpty {
            guard let selected else {
                return Status(label: "Check thinking", detail: "Choose a catalog-backed model before pinning a thinking level.",
                              symbol: "exclamationmark.triangle.fill", color: .orange)
            }
            guard selected.reasoningValues.contains(reasoning) else {
                return Status(label: "Unsupported",
                              detail: "\(reasoningDisplayName(reasoning)) is not supported by \(selected.displayName).",
                              symbol: "exclamationmark.triangle.fill", color: .orange)
            }
        }
        let modelName = selected?.displayName ?? "Provider default model"
        let thinking = reasoning.isEmpty ? "provider default thinking" : reasoningDisplayName(reasoning)
        return Status(label: "Ready", detail: "\(name) · \(modelName) · \(thinking).",
                      symbol: "checkmark.circle.fill", color: .green)
    }
}
