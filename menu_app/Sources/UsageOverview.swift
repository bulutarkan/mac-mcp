import SwiftUI

/// One headline figure under a small label, for the Usage overview's stat row.
struct UsageStat: View {
    let title: String
    let value: String
    var help: String? = nil
    var tint: Color? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(value)
                .font(.callout.weight(.semibold))
                .monospacedDigit()
                .foregroundStyle(tint ?? .primary)
                .lineLimit(1)
                .minimumScaleFactor(0.8)
            Text(title)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(1)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .help(help ?? "")
        .accessibilityElement(children: .combine)
    }
}

/// Two parts of one whole as a single bar, with each part named underneath.
struct UsageSplitBar: View {
    let first: (label: String, value: Int)
    let second: (label: String, value: Int)
    let format: (Int) -> String

    var body: some View {
        let total = max(1, first.value + second.value)
        VStack(alignment: .leading, spacing: 6) {
            GeometryReader { proxy in
                let gap: CGFloat = 2
                let usable = max(0, proxy.size.width - gap)
                let firstWidth = first.value + second.value == 0
                    ? usable / 2
                    : usable * CGFloat(first.value) / CGFloat(total)
                HStack(spacing: gap) {
                    Capsule(style: .continuous)
                        .fill(Color.accentColor.gradient)
                        .frame(width: max(first.value > 0 ? 4 : 0, firstWidth))
                    Capsule(style: .continuous)
                        .fill(Color.accentColor.opacity(0.45))
                }
            }
            .frame(height: 6)
            .accessibilityHidden(true)
            HStack(spacing: 14) {
                legend(first.label, first.value, Color.accentColor)
                legend(second.label, second.value, Color.accentColor.opacity(0.45))
                Spacer(minLength: 0)
            }
        }
    }

    private func legend(_ label: String, _ value: Int, _ color: Color) -> some View {
        HStack(spacing: 5) {
            Circle().fill(color).frame(width: 7, height: 7)
            Text(label).foregroundStyle(.secondary)
            Text(format(value)).monospacedDigit()
        }
        .font(.caption)
        .accessibilityElement(children: .combine)
    }
}

/// The tools that moved the most payload: the first few, the rest on request.
struct UsageTopToolsList: View {
    let tools: [UsageTool]
    let format: (Int) -> String

    @MacMCPState private var showAll = false

    private static let folded = 5
    private static let failureMark = 0.05

    init(tools: [UsageTool], format: @escaping (Int) -> String) {
        self.tools = tools
        self.format = format
    }

    var body: some View {
        let maxTokens = max(1, tools.map(\.totalTokens).max() ?? 1)
        let shown = showAll ? tools : Array(tools.prefix(Self.folded))
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(shown.enumerated()), id: \.element.id) { index, tool in
                HStack(spacing: 12) {
                    Text(tool.tool)
                        .font(.system(.caption, design: .monospaced).weight(.medium))
                        .lineLimit(1)
                        .truncationMode(.middle)
                        .frame(width: 150, alignment: .leading)
                    GeometryReader { proxy in
                        ZStack(alignment: .leading) {
                            Capsule().fill(Color.primary.opacity(0.07))
                            Capsule()
                                .fill(Color.accentColor.opacity(0.8))
                                .frame(width: max(3, proxy.size.width * CGFloat(tool.totalTokens) / CGFloat(maxTokens)))
                        }
                    }
                    .frame(height: 5)
                    .accessibilityHidden(true)
                    Text(format(tool.totalTokens))
                        .font(.caption.weight(.semibold).monospacedDigit())
                        .frame(width: 52, alignment: .trailing)
                    Text("\(format(tool.calls)) calls")
                        .font(.caption2.monospacedDigit())
                        .foregroundStyle(.secondary)
                        .frame(width: 70, alignment: .trailing)
                    Group {
                        // Most tools fail now and then; only a noticeable failure rate earns the mark.
                        if tool.calls > 0, Double(tool.errorCount) / Double(tool.calls) >= Self.failureMark {
                            Image(systemName: "exclamationmark.circle.fill")
                                .foregroundStyle(.orange)
                                .help("\(tool.errorCount) of \(tool.calls) calls failed")
                        } else {
                            Color.clear
                        }
                    }
                    .font(.caption)
                    .frame(width: 14)
                }
                .padding(.vertical, 7)
                .accessibilityElement(children: .combine)
                .accessibilityLabel(
                    "\(tool.tool), \(format(tool.totalTokens)) tokens, \(tool.calls) calls"
                        + (tool.errorCount > 0 ? ", \(tool.errorCount) failed" : "")
                )
                if index < shown.count - 1 { Divider() }
            }
            if tools.count > Self.folded {
                Button(showAll ? "Show fewer" : "Show all \(tools.count)") {
                    withAnimation(.easeInOut(duration: 0.2)) { showAll.toggle() }
                }
                .buttonStyle(.link)
                .font(.caption)
                .padding(.top, 6)
            }
        }
    }
}

/// A card that shows one summary line and opens to its settings, folded by default.
struct UsageCollapsibleCard<Content: View>: View {
    let title: String
    let symbol: String
    let summary: String
    @ViewBuilder let content: () -> Content

    @MacMCPState private var expanded = false

    init(title: String, symbol: String, summary: String, @ViewBuilder content: @escaping () -> Content) {
        self.title = title
        self.symbol = symbol
        self.summary = summary
        self.content = content
    }

    var body: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 0) {
                Button {
                    withAnimation(.easeInOut(duration: 0.2)) { expanded.toggle() }
                } label: {
                    HStack(spacing: 10) {
                        Text(summary)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                        Spacer(minLength: 8)
                        Image(systemName: "chevron.right")
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(.tertiary)
                            .rotationEffect(.degrees(expanded ? 90 : 0))
                    }
                    .padding(.vertical, 3)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("\(title): \(summary)")
                .accessibilityHint(expanded ? "Hide settings" : "Show settings")

                if expanded {
                    VStack(alignment: .leading, spacing: 14) {
                        content()
                    }
                    .padding(.top, 12)
                    .transition(.opacity)
                }
            }
        } label: {
            Label(title, systemImage: symbol)
        }
    }
}
