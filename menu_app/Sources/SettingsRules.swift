import Foundation

/// The two Voice settings typed by hand, checked the way Microsoft's text-to-speech (edge-tts) checks
/// them before it speaks (#125): a value it would refuse is never saved.
enum VoiceSettingsFormat {
    static let voiceHint = "A voice name such as tr-TR-AhmetNeural or en-US-AriaNeural."
    static let rateHint = "Slower or faster than normal, such as -5% or +10%."

    /// The voice as saved, or nil when edge-tts would not take it.
    static func voice(_ raw: String) -> String? {
        let value = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        let short = #"^[a-z]{2,}-[A-Z]{2,}-[A-Za-z0-9-]+Neural$"#
        let long = #"^Microsoft Server Speech Text to Speech Voice \(.+,.+\)$"#
        guard value.range(of: short, options: .regularExpression) != nil
                || value.range(of: long, options: .regularExpression) != nil else { return nil }
        return value
    }

    /// The rate as saved ("+10%", "-5%"), or nil when it is not a signed percentage. A number
    /// without its sign or percent ("10", "-5") is read as that percentage.
    static func rate(_ raw: String) -> String? {
        let value = raw.trimmingCharacters(in: .whitespacesAndNewlines).replacingOccurrences(of: " ", with: "")
        guard value.range(of: #"^[+-]?[0-9]{1,3}%?$"#, options: .regularExpression) != nil else { return nil }
        var digits = value
        var sign = "+"
        if let first = digits.first, first == "+" || first == "-" {
            sign = String(first)
            digits.removeFirst()
        }
        if digits.hasSuffix("%") { digits.removeLast() }
        guard let number = Int(digits) else { return nil }
        return "\(sign)\(number)%"
    }

    static func voiceProblem(_ raw: String) -> String {
        raw.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            ? "Enter a voice name, such as tr-TR-AhmetNeural. Not saved."
            : "“\(raw.trimmingCharacters(in: .whitespacesAndNewlines))” is not a voice name. Use one such as tr-TR-AhmetNeural. Not saved."
    }

    static func rateProblem(_ raw: String) -> String {
        raw.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            ? "Enter a rate, such as -5% or +10%. Not saved."
            : "“\(raw.trimmingCharacters(in: .whitespacesAndNewlines))” is not a rate. Use a percentage such as -5% or +10%. Not saved."
    }
}

/// Where OpenAI Decisions may pick between equally likely targets (decision_acceleration.scope; the
/// server's "native" is Mac apps). Off is both `enabled: false` and scope "off" (#125).
enum DecisionScope: String, CaseIterable, Identifiable {
    case off
    case browser
    case native
    case both

    var id: String { rawValue }

    var title: String {
        switch self {
        case .off: return "Off"
        case .browser: return "Browser"
        case .native: return "Mac apps"
        case .both: return "Both"
        }
    }

    private var usedFor: String {
        switch self {
        case .off: return ""
        case .browser: return "browser pages"
        case .native: return "Mac apps"
        case .both: return "browser pages and Mac apps"
        }
    }

    /// The status line under the picker. It says "used for" only when the server would really use it:
    /// a scope other than Off and a key that is not missing or rejected.
    static func statusText(scope: DecisionScope, keyStatus: String, checkStatus: String?) -> String {
        let key: String
        switch keyStatus {
        case "missing": key = "No key"
        case "valid": key = "Key verified"
        case "invalid": key = "Key rejected by OpenAI"
        default:
            if let checkStatus, checkStatus != "valid" {
                key = "Key check: \(checkStatus.replacingOccurrences(of: "_", with: " "))"
            } else {
                key = "Key saved, not verified yet"
            }
        }
        if scope == .off { return "Off · \(key) · nothing is sent to OpenAI" }
        switch keyStatus {
        case "missing": return "\(key) · not used until you add one"
        case "invalid": return "\(key) · not used until you replace it"
        case "valid": return "\(key) · used for \(scope.usedFor)"
        default: return "\(key) · used for \(scope.usedFor); press Test to check the key"
        }
    }
}
