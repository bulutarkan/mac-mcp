import AppKit
import SwiftUI

// MARK: Markdown as agents write it: headings, lists, tables, code blocks and quotes, with inline styles.
// A heading needs a space after its #s, so "#111" (a roadmap number) stays text.

enum MarkdownBlock: Hashable {
    case heading(level: Int, text: String)
    case paragraph(String)
    case list(ordered: Bool, items: [MarkdownListItem])
    case code(language: String, text: String)
    case table(header: [String], rows: [[String]])
    case quote(String)
    case rule
}

struct MarkdownListItem: Hashable {
    let indent: Int
    let marker: String
    let text: String
}

enum MarkdownParser {
    private static let heading = try! NSRegularExpression(pattern: #"^(#{1,6})\s+(.+?)\s*#*\s*$"#)
    private static let listItem = try! NSRegularExpression(pattern: #"^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)$"#)
    private static let tableRule = try! NSRegularExpression(pattern: #"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$"#)
    private static let rule = try! NSRegularExpression(pattern: #"^\s*([-*_])(\s*\1){2,}\s*$"#)

    private static let directive = try! NSRegularExpression(pattern: #":([A-Za-z][\w-]*citation[\w-]*)\{([^{}\n]*)\}"#)
    private static let attribute = try! NSRegularExpression(pattern: #"([A-Za-z_]+)\s*=\s*"([^"]*)""#)

    /// Agents' source notes, such as Codex's :codex-file-citation{path="…" line_start="3"}, become links.
    static func citations(_ text: String) -> String {
        guard text.contains("citation{") else { return text }
        var result = text
        let matches = directive.matches(in: text, range: NSRange(text.startIndex..., in: text)).reversed()
        for match in matches {
            guard let whole = Range(match.range, in: result), let body = Range(match.range(at: 2), in: result) else { continue }
            let attributes = String(result[body])
            var values: [String: String] = [:]
            for pair in attribute.matches(in: attributes, range: NSRange(attributes.startIndex..., in: attributes)) {
                if let key = Range(pair.range(at: 1), in: attributes), let value = Range(pair.range(at: 2), in: attributes) {
                    values[String(attributes[key]).lowercased()] = String(attributes[value])
                }
            }
            guard let target = values["path"] ?? values["url"] ?? values["href"] ?? values["source"], !target.isEmpty else {
                result.replaceSubrange(whole, with: "")
                continue
            }
            result.replaceSubrange(whole, with: "[\(citationLabel(target, line: values["line_start"]))](\(citationURL(target)))")
        }
        return result
    }

    private static func citationLabel(_ target: String, line: String?) -> String {
        if let url = URL(string: target), let host = url.host, target.hasPrefix("http") {
            if host == "docs.google.com" {
                if url.path.hasPrefix("/spreadsheets") { return "Google Sheets ↗" }
                if url.path.hasPrefix("/document") { return "Google Docs ↗" }
                if url.path.hasPrefix("/presentation") { return "Google Slides ↗" }
            }
            return host.replacingOccurrences(of: "www.", with: "") + " ↗"
        }
        let name = (target as NSString).lastPathComponent
        return name + (line.map { ":\($0)" } ?? "") + " ↗"
    }

    private static func citationURL(_ target: String) -> String {
        if target.hasPrefix("http://") || target.hasPrefix("https://") || target.hasPrefix("file://") {
            return target.replacingOccurrences(of: " ", with: "%20").replacingOccurrences(of: ")", with: "%29")
        }
        return URL(fileURLWithPath: (target as NSString).expandingTildeInPath).absoluteString
    }

    static func parse(_ source: String) -> [MarkdownBlock] {
        let lines = citations(source).replacingOccurrences(of: "\r\n", with: "\n").components(separatedBy: "\n")
        var blocks: [MarkdownBlock] = []
        var paragraph: [String] = []
        var index = 0

        func flush() {
            let text = paragraph.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
            if !text.isEmpty { blocks.append(.paragraph(text)) }
            paragraph = []
        }

        while index < lines.count {
            let line = lines[index]
            let trimmed = line.trimmingCharacters(in: .whitespaces)

            if trimmed.hasPrefix("```") || trimmed.hasPrefix("~~~") {
                flush()
                let fence = String(trimmed.prefix(3))
                let language = String(trimmed.dropFirst(3)).trimmingCharacters(in: .whitespaces)
                var code: [String] = []
                index += 1
                while index < lines.count, !lines[index].trimmingCharacters(in: .whitespaces).hasPrefix(fence) {
                    code.append(lines[index])
                    index += 1
                }
                blocks.append(.code(language: language, text: code.joined(separator: "\n")))
                index += 1
                continue
            }
            if trimmed.isEmpty {
                flush()
                index += 1
                continue
            }
            if trimmed.contains("|"), index + 1 < lines.count, matches(tableRule, lines[index + 1]) {
                flush()
                let header = cells(line)
                var rows: [[String]] = []
                index += 2
                while index < lines.count, lines[index].contains("|"),
                      !lines[index].trimmingCharacters(in: .whitespaces).isEmpty {
                    var row = cells(lines[index])
                    if row.count < header.count { row += Array(repeating: "", count: header.count - row.count) }
                    rows.append(Array(row.prefix(header.count)))
                    index += 1
                }
                blocks.append(.table(header: header, rows: rows))
                continue
            }
            if let groups = captures(heading, line) {
                flush()
                blocks.append(.heading(level: groups[0].count, text: groups[1]))
                index += 1
                continue
            }
            if matches(rule, line) {
                flush()
                blocks.append(.rule)
                index += 1
                continue
            }
            if trimmed.hasPrefix(">") {
                flush()
                var quoted: [String] = []
                while index < lines.count, lines[index].trimmingCharacters(in: .whitespaces).hasPrefix(">") {
                    quoted.append(String(lines[index].trimmingCharacters(in: .whitespaces).dropFirst())
                        .trimmingCharacters(in: .whitespaces))
                    index += 1
                }
                blocks.append(.quote(quoted.joined(separator: "\n")))
                continue
            }
            if captures(listItem, line) != nil {
                flush()
                var items: [MarkdownListItem] = []
                let ordered = (captures(listItem, line)?[1].first?.isNumber) ?? false
                while index < lines.count {
                    if let groups = captures(listItem, lines[index]) {
                        items.append(MarkdownListItem(indent: groups[0].count / 2, marker: groups[1], text: groups[2]))
                    } else if let last = items.last, !lines[index].trimmingCharacters(in: .whitespaces).isEmpty,
                              lines[index].hasPrefix("  ") {
                        // A wrapped line belongs to the item above it.
                        items[items.count - 1] = MarkdownListItem(indent: last.indent, marker: last.marker,
                                                                  text: last.text + " " + lines[index].trimmingCharacters(in: .whitespaces))
                    } else {
                        break
                    }
                    index += 1
                }
                blocks.append(.list(ordered: ordered, items: items))
                continue
            }
            paragraph.append(line)
            index += 1
        }
        flush()
        return blocks
    }

    private static func cells(_ line: String) -> [String] {
        var trimmed = line.trimmingCharacters(in: .whitespaces)
        if trimmed.hasPrefix("|") { trimmed.removeFirst() }
        if trimmed.hasSuffix("|") { trimmed.removeLast() }
        return trimmed.components(separatedBy: "|").map { $0.trimmingCharacters(in: .whitespaces) }
    }

    private static func matches(_ regex: NSRegularExpression, _ text: String) -> Bool {
        regex.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)) != nil
    }

    private static func captures(_ regex: NSRegularExpression, _ text: String) -> [String]? {
        guard let match = regex.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)) else { return nil }
        return (1..<match.numberOfRanges).map { group in
            Range(match.range(at: group), in: text).map { String(text[$0]) } ?? ""
        }
    }

    /// Inline Markdown (bold, italic, links, `code`), with code tinted so it stands apart from the words.
    static func inline(_ text: String, size: CGFloat = 14, fade: Int = 0) -> AttributedString {
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        var result = (try? AttributedString(markdown: text, options: options)) ?? AttributedString(text)
        for run in result.runs {
            if let intent = run.inlinePresentationIntent, intent.contains(.code) {
                result[run.range].font = .system(size: size - 1, design: .monospaced)
                result[run.range].backgroundColor = ChatStyle.code
                result[run.range].foregroundColor = Color.primary.opacity(0.9)
            }
            if run.link != nil {
                result[run.range].foregroundColor = VisualTokens.accent
            }
        }
        if fade > 0 { fadeTail(&result, count: fade) }
        return result
    }

    /// The words that just arrived are drawn faint and settle in, oldest first.
    private static func fadeTail(_ text: inout AttributedString, count: Int) {
        var index = text.endIndex
        var step = 0
        while step < count, index > text.startIndex {
            let previous = text.characters.index(before: index)
            let strength = 0.18 + 0.82 * Double(step) / Double(max(count, 1))
            text[previous..<index].foregroundColor = Color.primary.opacity(strength)
            index = previous
            step += 1
        }
    }
}

struct MarkdownView: View {
    let text: String
    var size: CGFloat = 14
    /// How many of the last characters are still settling in (a streaming answer); 0 draws all at once.
    var fade: Int = 0

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            let blocks = MarkdownParser.parse(text)
            ForEach(Array(blocks.enumerated()), id: \.offset) { index, block in
                view(for: block, fade: index == blocks.count - 1 ? fade : 0)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .textSelection(.enabled)
    }

    @ViewBuilder private func view(for block: MarkdownBlock, fade: Int) -> some View {
        switch block {
        case .heading(let level, let text):
            Text(MarkdownParser.inline(text, size: size, fade: fade))
                .font(.system(size: level == 1 ? size + 6 : level == 2 ? size + 3 : size + 1, weight: .semibold))
                .padding(.top, level <= 2 ? 6 : 2)
        case .paragraph(let text):
            Text(MarkdownParser.inline(text, size: size, fade: fade)).font(.system(size: size)).lineSpacing(3.5)
                .fixedSize(horizontal: false, vertical: true)
        case .list(let ordered, let items):
            VStack(alignment: .leading, spacing: 5) {
                ForEach(Array(items.enumerated()), id: \.offset) { position, item in
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(ordered ? item.marker : "•").font(.system(size: size)).foregroundStyle(.secondary)
                            .frame(minWidth: ordered ? 18 : 10, alignment: .trailing)
                        Text(MarkdownParser.inline(item.text, size: size, fade: position == items.count - 1 ? fade : 0))
                            .font(.system(size: size)).lineSpacing(3)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .padding(.leading, CGFloat(item.indent) * 18)
                }
            }
        case .code(let language, let text):
            CodeBlockView(language: language, code: text)
        case .table(let header, let rows):
            MarkdownTable(header: header, rows: rows, size: size, fade: fade)
        case .quote(let text):
            HStack(spacing: 10) {
                Capsule().fill(ChatStyle.accent.opacity(0.6)).frame(width: 3)
                Text(MarkdownParser.inline(text, size: size)).font(.system(size: size)).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        case .rule:
            Rectangle().fill(VisualTokens.hairline).frame(height: 1).padding(.vertical, 4)
        }
    }
}

/// Each column gets a width from its longest cell, so a long cell wraps inside its row instead of
/// spilling over the next one; the columns then shrink together to fit the space the answer has.
private struct MarkdownTable: View {
    let header: [String]
    let rows: [[String]]
    let size: CGFloat
    var fade: Int = 0
    @MacMCPState private var available: CGFloat = ChatStyle.readingWidth - 34
    @MacMCPState private var grown: [CGFloat] = []  // widest each column has been: a streaming table never narrows

    init(header: [String], rows: [[String]], size: CGFloat, fade: Int = 0) {
        self.header = header
        self.rows = rows
        self.size = size
        self.fade = fade
    }

    var body: some View {
        let natural = columnWidths
        let widths = Self.fit(Self.steady(natural, grown), into: available)
        ScrollView(.horizontal, showsIndicators: false) {
            VStack(alignment: .leading, spacing: 0) {
                row(header, widths: widths, bold: true, fade: 0).background(ChatStyle.code)
                ForEach(Array(rows.enumerated()), id: \.offset) { index, cells in
                    Rectangle().fill(VisualTokens.hairline).frame(height: 1)
                    row(cells, widths: widths, bold: false, fade: index == rows.count - 1 ? fade : 0)
                        .background(index % 2 == 1 ? ChatStyle.code.opacity(0.45) : Color.clear)
                }
            }
            .fixedSize(horizontal: true, vertical: false)
            .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(VisualTokens.hairline))
            .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        // The width the answer gives the table; it does not depend on the table, so this settles at once.
        .onChange(of: natural) { next in grown = Self.steady(next, grown) }
        .background(GeometryReader { geometry in
            Color.clear
                .onAppear { available = geometry.size.width }
                .onChange(of: geometry.size.width) { width in if abs(width - available) > 1 { available = width } }
        })
    }

    private func row(_ cells: [String], widths: [CGFloat], bold: Bool, fade: Int) -> some View {
        // A streaming table grows in its last filled cell: that is where the new words settle in.
        let growing = fade > 0 ? cells.lastIndex(where: { !$0.isEmpty }) : nil
        return HStack(alignment: .top, spacing: 0) {
            ForEach(Array(widths.enumerated()), id: \.offset) { column, width in
                Text(MarkdownParser.inline(column < cells.count ? cells[column] : "", size: size - 1,
                                           fade: column == growing ? fade : 0))
                    .font(.system(size: size - 1, weight: bold ? .semibold : .regular))
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(width: width - 24, alignment: .leading)
                    .padding(.horizontal, 12).padding(.vertical, bold ? 8 : 7)
            }
        }
    }

    private var columnWidths: [CGFloat] {
        header.indices.map { column in
            let longest = ([header] + rows).map { column < $0.count ? $0[column].count : 0 }.max() ?? 0
            return min(320, max(72, CGFloat(longest) * (size - 1) * 0.56 + 24))
        }
    }

    static func steady(_ natural: [CGFloat], _ grown: [CGFloat]) -> [CGFloat] {
        natural.indices.map { max(natural[$0], $0 < grown.count ? grown[$0] : 0) }
    }

    /// Wide columns give way first; no column drops below 72 pt. A table that still does not fit scrolls.
    static func fit(_ widths: [CGFloat], into available: CGFloat) -> [CGFloat] {
        let floor: CGFloat = 72
        let total = widths.reduce(0, +)
        guard available > 0, total > available else { return widths }
        let room = widths.map { max(0, $0 - floor) }.reduce(0, +)
        guard room > 0 else { return widths }
        let cut = min(1, (total - available) / room)
        return widths.map { $0 - max(0, $0 - floor) * cut }
    }
}

struct CodeBlockView: View {
    let language: String
    let code: String

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text(language.isEmpty ? "code" : language).font(.system(size: 11, weight: .medium)).foregroundStyle(.secondary)
                Spacer()
                CopyButton(text: code, label: "Copy code")
            }
            .padding(.horizontal, 12).padding(.top, 7).padding(.bottom, 3)
            ScrollView(.horizontal, showsIndicators: false) {
                Text(SyntaxHighlighter.highlight(code, language: language))
                    .font(.system(size: 12.5, design: .monospaced)).textSelection(.enabled)
                    .padding(.horizontal, 12).padding(.bottom, 10).padding(.top, 2)
                    .fixedSize(horizontal: true, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(ChatStyle.code))
        .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(VisualTokens.hairline.opacity(0.7)))
    }
}

/// A copy icon that turns into a check for a moment once copied.
struct CopyButton: View {
    let text: String
    var label = "Copy"
    @MacMCPState private var copied = false

    init(text: String, label: String = "Copy") {
        self.text = text
        self.label = label
    }

    var body: some View {
        Button {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(text, forType: .string)
            withAnimation(.easeOut(duration: 0.15)) { copied = true }
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.4) {
                withAnimation(.easeOut(duration: 0.2)) { copied = false }
            }
        } label: {
            Image(systemName: copied ? "checkmark" : "doc.on.doc")
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(copied ? VisualTokens.success : Color.secondary)
                .frame(width: 22, height: 20)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help(copied ? "Copied" : label)
        .accessibilityLabel(label)
    }
}
