import Foundation
import LocalAuthentication
import Security

enum KeychainStore {
    static let service = ProcessInfo.processInfo.environment["MAC_MCP_VOICE_GROQ_KEYCHAIN_SERVICE"] ?? "com.bulutarkan.mac-mcp"
    static let account = ProcessInfo.processInfo.environment["MAC_MCP_VOICE_GROQ_KEYCHAIN_ACCOUNT"] ?? "groq-api-key"
    static let decisionsService = ProcessInfo.processInfo.environment["MAC_MCP_DECISIONS_KEYCHAIN_SERVICE"] ?? "com.bulutarkan.mac-mcp"
    static let decisionsAccount = ProcessInfo.processInfo.environment["MAC_MCP_DECISIONS_KEYCHAIN_ACCOUNT"] ?? "openai-decisions-api-key"

    static func hasGroqKey() -> Bool {
        hasValue(service: service, account: account)
    }

    static func readGroqKey() throws -> String {
        try read(service: service, account: account)
    }

    static func saveGroqKey(_ value: String) throws {
        try save(value, service: service, account: account)
    }

    static func removeGroqKey() throws {
        try remove(service: service, account: account)
    }

    static func hasDecisionsKey() -> Bool {
        hasValue(service: decisionsService, account: decisionsAccount)
    }

    static func saveDecisionsKey(_ value: String) throws {
        try save(value, service: decisionsService, account: decisionsAccount)
    }

    static func removeDecisionsKey() throws {
        try remove(service: decisionsService, account: decisionsAccount)
    }

    /// Whether the item exists, without reading the secret. Reading it needs the keychain's
    /// permission for this exact build, and a rebuilt app would otherwise block at launch on an
    /// access prompt nobody may be there to answer.
    private static func hasValue(service: String, account: String) -> Bool {
        let context = LAContext()
        context.interactionNotAllowed = true
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnAttributes as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
            kSecUseAuthenticationContext as String: context,
        ]
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        // Present but locked behind a prompt still counts as configured.
        return status == errSecSuccess || status == errSecInteractionNotAllowed
    }

    private static func read(service: String, account: String) throws -> String {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        if status == errSecItemNotFound { return "" }
        guard status == errSecSuccess,
              let data = item as? Data,
              let value = String(data: data, encoding: .utf8) else {
            throw NSError(domain: NSOSStatusErrorDomain, code: Int(status))
        }
        return value
    }

    private static func save(_ value: String, service: String, account: String) throws {
        let data = Data(value.utf8)
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        let update: [String: Any] = [kSecValueData as String: data]
        let status = SecItemUpdate(query as CFDictionary, update as CFDictionary)
        if status == errSecItemNotFound {
            var add = query
            add[kSecValueData as String] = data
            let addStatus = SecItemAdd(add as CFDictionary, nil)
            guard addStatus == errSecSuccess else {
                throw NSError(domain: NSOSStatusErrorDomain, code: Int(addStatus))
            }
            return
        }
        guard status == errSecSuccess else {
            throw NSError(domain: NSOSStatusErrorDomain, code: Int(status))
        }
    }

    private static func remove(service: String, account: String) throws {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        let status = SecItemDelete(query as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw NSError(domain: NSOSStatusErrorDomain, code: Int(status))
        }
    }
}
