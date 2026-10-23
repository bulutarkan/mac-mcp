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

    static func parse(_ source: String) -> [MarkdownBlock] {
        let lines = source.replacingOccurrences(of: "\r\n", with: "\n").components(separatedBy: "\n")
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
    static func inline(_ text: String, size: CGFloat = 14) -> AttributedString {
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
        return result
    }
}

struct MarkdownView: View {
    let text: String
    var size: CGFloat = 14

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(Array(MarkdownParser.parse(text).enumerated()), id: \.offset) { _, block in
                view(for: block)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .textSelection(.enabled)
    }

    @ViewBuilder private func view(for block: MarkdownBlock) -> some View {
        switch block {
        case .heading(let level, let text):
            Text(MarkdownParser.inline(text, size: size))
                .font(.system(size: level == 1 ? size + 6 : level == 2 ? size + 3 : size + 1, weight: .semibold))
                .padding(.top, level <= 2 ? 6 : 2)
        case .paragraph(let text):
            Text(MarkdownParser.inline(text, size: size)).font(.system(size: size)).lineSpacing(3.5)
                .fixedSize(horizontal: false, vertical: true)
        case .list(let ordered, let items):
            VStack(alignment: .leading, spacing: 5) {
                ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(ordered ? item.marker : "•").font(.system(size: size)).foregroundStyle(.secondary)
                            .frame(minWidth: ordered ? 18 : 10, alignment: .trailing)
                        Text(MarkdownParser.inline(item.text, size: size)).font(.system(size: size)).lineSpacing(3)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .padding(.leading, CGFloat(item.indent) * 18)
                }
            }
        case .code(let language, let text):
            CodeBlockView(language: language, code: text)
        case .table(let header, let rows):
            MarkdownTable(header: header, rows: rows, size: size)
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

private struct MarkdownTable: View {
    let header: [String]
    let rows: [[String]]
    let size: CGFloat

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            Grid(alignment: .leading, horizontalSpacing: 0, verticalSpacing: 0) {
                GridRow {
                    ForEach(Array(header.enumerated()), id: \.offset) { _, cell in
                        Text(MarkdownParser.inline(cell, size: size - 1)).font(.system(size: size - 1, weight: .semibold))
                            .padding(.horizontal, 12).padding(.vertical, 8)
                            .frame(maxWidth: 320, alignment: .leading)
                    }
                }
                .background(ChatStyle.code)
                ForEach(Array(rows.enumerated()), id: \.offset) { index, row in
                    Divider().gridCellUnsizedAxes(.horizontal)
                    GridRow {
                        ForEach(Array(row.enumerated()), id: \.offset) { _, cell in
                            Text(MarkdownParser.inline(cell, size: size - 1)).font(.system(size: size - 1))
                                .padding(.horizontal, 12).padding(.vertical, 7)
                                .frame(maxWidth: 320, alignment: .leading)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    .background(index % 2 == 1 ? ChatStyle.code.opacity(0.45) : Color.clear)
                }
            }
            .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(VisualTokens.hairline))
            .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
        }
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
                Text(code).font(.system(size: 12.5, design: .monospaced)).textSelection(.enabled)
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
