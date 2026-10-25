import AppKit
import SwiftUI

// MARK: Syntax colours for code blocks: keywords, strings, comments, numbers and type names in a few common
// languages. A light tokenizer, not a parser: good enough to read code at a glance.

enum SyntaxHighlighter {
    private enum Family { case cLike, python, shell, sql, json, markup, css, yaml, diff, plain }

    private static let keywords: [Family: Set<String>] = [
        .cLike: ["func", "let", "var", "if", "else", "for", "while", "return", "import", "struct", "class", "enum",
                 "protocol", "extension", "guard", "switch", "case", "default", "break", "continue", "in", "do", "try",
                 "catch", "throw", "throws", "async", "await", "public", "private", "fileprivate", "internal", "static",
                 "final", "self", "Self", "super", "init", "nil", "true", "false", "null", "undefined", "function",
                 "const", "new", "this", "export", "from", "interface", "type", "implements", "extends", "package",
                 "fn", "mut", "impl", "pub", "use", "mod", "match", "loop", "where", "go", "defer", "chan", "map",
                 "range", "select", "void", "int", "long", "double", "float", "bool", "boolean", "char", "string",
                 "fun", "val", "when", "object", "override", "typeof", "instanceof", "yield", "of", "def", "end",
                 "module", "require", "unless", "elsif", "begin", "rescue", "lambda", "operator", "namespace"],
        .python: ["def", "class", "return", "if", "elif", "else", "for", "while", "in", "not", "and", "or", "is",
                  "import", "from", "as", "with", "try", "except", "finally", "raise", "pass", "break", "continue",
                  "lambda", "yield", "async", "await", "None", "True", "False", "self", "global", "nonlocal", "assert",
                  "del", "match", "case"],
        .shell: ["if", "then", "else", "elif", "fi", "for", "while", "do", "done", "case", "esac", "in", "function",
                 "return", "export", "local", "echo", "cd", "set", "unset", "source", "exit", "sudo", "true", "false"],
        .sql: ["select", "from", "where", "and", "or", "not", "insert", "into", "values", "update", "set", "delete",
               "create", "table", "index", "drop", "alter", "join", "left", "right", "inner", "outer", "on", "group",
               "by", "order", "limit", "as", "distinct", "having", "union", "null", "is", "like", "in", "primary", "key"],
        .css: ["important", "inherit", "initial", "none", "auto"],
        .yaml: ["true", "false", "null", "yes", "no", "on", "off"],
    ]

    private static func family(_ language: String) -> Family {
        switch language.lowercased() {
        case "swift", "js", "javascript", "jsx", "ts", "typescript", "tsx", "go", "golang", "rust", "rs", "java",
             "kotlin", "kt", "c", "h", "cpp", "c++", "objc", "objective-c", "cs", "csharp", "ruby", "rb", "php", "dart",
             "scala":
            return .cLike
        case "python", "py": return .python
        case "bash", "sh", "zsh", "shell", "console", "fish": return .shell
        case "sql", "sqlite", "postgres", "mysql": return .sql
        case "json", "jsonc": return .json
        case "html", "xml", "svg", "plist", "vue": return .markup
        case "css", "scss", "less": return .css
        case "yaml", "yml", "toml", "ini": return .yaml
        case "diff", "patch": return .diff
        default: return .plain
        }
    }

    private static let keyword = VisualTokens.adaptive(light: 0xAD3DA4, dark: 0xFF7AB2)
    private static let string = VisualTokens.adaptive(light: 0xC41A16, dark: 0xFC6A5D)
    private static let comment = VisualTokens.adaptive(light: 0x707F8C, dark: 0x7F8C98)
    private static let number = VisualTokens.adaptive(light: 0x1C00CF, dark: 0xD9C97C)
    private static let typeName = VisualTokens.adaptive(light: 0x3900A0, dark: 0x5DD8FF)
    private static let key = VisualTokens.adaptive(light: 0x0B6E99, dark: 0x6BDFFF)
    private static let cache = NSCache<NSString, Box>()
    private static var patterns: [Family: NSRegularExpression] = [:]

    private final class Box {
        let value: AttributedString
        init(_ value: AttributedString) { self.value = value }
    }

    static func highlight(_ code: String, language: String) -> AttributedString {
        let family = family(language)
        guard family != .plain, code.utf16.count < 60_000 else { return AttributedString(code) }
        let cacheKey = "\(language)\u{1}\(code)" as NSString
        if let cached = cache.object(forKey: cacheKey) { return cached.value }
        var result = AttributedString()
        if family == .diff {
            for (index, line) in code.components(separatedBy: "\n").enumerated() {
                var piece = AttributedString((index > 0 ? "\n" : "") + line)
                if line.hasPrefix("+") { piece.foregroundColor = VisualTokens.success }
                if line.hasPrefix("-") { piece.foregroundColor = VisualTokens.error }
                if line.hasPrefix("@@") { piece.foregroundColor = comment }
                result += piece
            }
        } else {
            result = tokens(code, family: family)
        }
        cache.setObject(Box(result), forKey: cacheKey)
        return result
    }

    private static func pattern(_ family: Family) -> NSRegularExpression {
        if let cached = patterns[family] { return cached }
        let comment: String
        switch family {
        case .python, .shell, .yaml: comment = #"#[^\n]*"#
        case .sql: comment = #"--[^\n]*|/\*[\s\S]*?\*/"#
        case .markup: comment = #"<!--[\s\S]*?-->"#
        case .css, .json: comment = #"/\*[\s\S]*?\*/"#
        default: comment = #"//[^\n]*|/\*[\s\S]*?\*/"#
        }
        let quoted = #""(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'"#
        let string = family == .python ? #"\"\"\"[\s\S]*?\"\"\"|'''[\s\S]*?'''|"# + quoted
            : quoted + #"|`(?:\\.|[^`\\])*`"#
        let tag = family == .markup ? #"|(?<tag></?[A-Za-z][\w:-]*)"# : ""
        let regex = try! NSRegularExpression(pattern:
            "(?<comment>\(comment))|(?<string>\(string))|(?<number>\\b(?:0x[0-9a-fA-F]+|\\d+(?:\\.\\d+)?)\\b)\(tag)|(?<word>[A-Za-z_$@][\\w$-]*)")
        patterns[family] = regex
        return regex
    }

    private static func tokens(_ code: String, family: Family) -> AttributedString {
        let text = code as NSString
        let words = keywords[family] ?? []
        let caseInsensitive = family == .sql
        var result = AttributedString()
        var cursor = 0
        for match in pattern(family).matches(in: code, range: NSRange(location: 0, length: text.length)) {
            if match.range.location > cursor {
                result += AttributedString(text.substring(with: NSRange(location: cursor, length: match.range.location - cursor)))
            }
            let token = text.substring(with: match.range)
            var piece = AttributedString(token)
            if match.range(withName: "comment").location != NSNotFound {
                piece.foregroundColor = comment
            } else if match.range(withName: "string").location != NSNotFound {
                piece.foregroundColor = isJSONKey(text, after: match.range.location + match.range.length, family) ? key : string
            } else if match.range(withName: "number").location != NSNotFound {
                piece.foregroundColor = number
            } else if family == .markup, match.range(withName: "tag").location != NSNotFound {
                piece.foregroundColor = keyword
            } else if words.contains(caseInsensitive ? token.lowercased() : token) {
                piece.foregroundColor = keyword
            } else if family == .cLike, token.first?.isUppercase == true, token.count > 1 {
                piece.foregroundColor = typeName
            }
            result += piece
            cursor = match.range.location + match.range.length
        }
        if cursor < text.length { result += AttributedString(text.substring(from: cursor)) }
        return result
    }

    /// A JSON string followed by ":" is a key.
    private static func isJSONKey(_ text: NSString, after: Int, _ family: Family) -> Bool {
        guard family == .json else { return false }
        var index = after
        while index < text.length, text.character(at: index) == 32 { index += 1 }
        return index < text.length && text.character(at: index) == 58
    }
}
