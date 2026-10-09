import Foundation

/// `mac-mcp doctor --json`, decoded leniently: rows carry different detail
/// shapes, and a field we cannot read must never hide the whole report.
struct DoctorRecovery: Decodable, Equatable {
    let action: String
    let url: String?
    let pane: String?
    let log: String?
}

struct DoctorTarget: Decodable, Equatable {
    let app: String
    let state: String
}

struct DoctorDetails: Decodable, Equatable {
    var recovery: DoctorRecovery?
    var features: [String]?
    var settingsPath: String?
    var listedAs: String?
    var context: String?
    var state: String?
    var targets: [DoctorTarget]?

    enum CodingKeys: String, CodingKey {
        case recovery, features, context, state, targets
        case settingsPath = "settings_path"
        case listedAs = "listed_as"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        recovery = try? c.decodeIfPresent(DoctorRecovery.self, forKey: .recovery)
        features = try? c.decodeIfPresent([String].self, forKey: .features)
        settingsPath = try? c.decodeIfPresent(String.self, forKey: .settingsPath)
        listedAs = try? c.decodeIfPresent(String.self, forKey: .listedAs)
        context = try? c.decodeIfPresent(String.self, forKey: .context)
        state = try? c.decodeIfPresent(String.self, forKey: .state)
        targets = try? c.decodeIfPresent([DoctorTarget].self, forKey: .targets)
    }
}

struct DoctorCheck: Decodable, Identifiable, Equatable {
    let checkID: String
    let category: String
    let status: String
    let summary: String
    let remediation: String?
    let details: DoctorDetails?

    var id: String { checkID }

    enum CodingKeys: String, CodingKey {
        case category, status, summary, remediation, details
        case checkID = "check_id"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        checkID = try c.decode(String.self, forKey: .checkID)
        category = (try? c.decode(String.self, forKey: .category)) ?? ""
        status = (try? c.decode(String.self, forKey: .status)) ?? "info"
        summary = (try? c.decode(String.self, forKey: .summary)) ?? ""
        remediation = try? c.decodeIfPresent(String.self, forKey: .remediation)
        details = try? c.decodeIfPresent(DoctorDetails.self, forKey: .details)
    }
}

struct DoctorReport: Decodable, Equatable {
    let ok: Bool
    let health: String?
    let version: String?
    let checks: [DoctorCheck]

    var problems: [DoctorCheck] { checks.filter { ($0.status == "fail" || $0.status == "warn") && $0.category != "permissions" } }
    var passed: [DoctorCheck] { checks.filter { ($0.status == "pass" || $0.status == "info") && $0.category != "permissions" } }
    var permissions: [DoctorCheck] { checks.filter { $0.category == "permissions" } }

    /// The JSON object in CLI output; anything printed around it is ignored.
    static func parse(_ output: String) -> DoctorReport? {
        guard let start = output.firstIndex(of: "{"), let end = output.lastIndex(of: "}") else { return nil }
        return try? JSONDecoder().decode(DoctorReport.self, from: Data(output[start...end].utf8))
    }
}
