import AppKit
import SwiftUI

// MARK: The pieces Settings › Usage is built from (Tools, Delegated Agents and Chat alike): a sentence-sized
// summary, bars per day split by colour, a box of facts with what each means, and rows with a share meter
// that open to their details. Laid out like Screen Time, with only SwiftUI and AppKit underneath.

enum UsageFormat {
    /// "820", "12.4K", "100K", "2.2M", "1.5B".
    static func compact(_ value: Int) -> String {
        let absolute = Double(abs(value))
        let text: String
        if absolute >= 1_000_000_000 { text = String(format: "%.1fB", Double(value) / 1_000_000_000) }
        else if absolute >= 1_000_000 { text = String(format: "%.1fM", Double(value) / 1_000_000) }
        else if absolute >= 1_000 { text = String(format: "%.1fK", Double(value) / 1_000) }
        else { return "\(value)" }
        return text.replacingOccurrences(of: ".0", with: "")
    }

    /// "18,726": written the way the English interface around it writes numbers.
    static func exact(_ value: Int) -> String {
        grouping.string(from: NSNumber(value: value)) ?? "\(value)"
    }

    /// "235.8 MB", "45 KB".
    static func bytes(_ value: Int) -> String {
        let size = Double(max(0, value))
        if size >= 1_000_000_000 { return String(format: "%.1f GB", size / 1_000_000_000) }
        if size >= 1_000_000 { return String(format: "%.1f MB", size / 1_000_000) }
        if size >= 1_000 { return "\(Int((size / 1_000).rounded())) KB" }
        return "\(Int(size)) bytes"
    }

    /// "under 1 s", "5 s or more", "240 ms": a measured time with how sure the bucket is.
    static func duration(_ milliseconds: Int?, relation: String?) -> String {
        guard let milliseconds else { return "—" }
        let seconds = Double(milliseconds) / 1000
        let amount = milliseconds >= 1000
            ? (seconds == seconds.rounded() ? "\(Int(seconds)) s" : String(format: "%.1f s", seconds))
            : "\(milliseconds) ms"
        switch relation {
        case "lt": return "under \(amount)"
        case "gte": return "\(amount) or more"
        default: return amount
        }
    }

    private static let grouping: NumberFormatter = {
        let formatter = NumberFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.numberStyle = .decimal
        formatter.usesGroupingSeparator = true
        formatter.groupingSeparator = ","
        formatter.groupingSize = 3
        return formatter
    }()

    /// "92%", "<1%", or "—" without a total.
    static func share(_ part: Int, of whole: Int) -> String {
        guard whole > 0 else { return "—" }
        let percent = Double(part) / Double(whole) * 100
        if part > 0 && percent < 1 { return "<1%" }
        return "\(Int(percent.rounded()))%"
    }

    /// "1 call", "2,156 calls", "4 skill searches".
    static func count(_ value: Int, _ noun: String, plural: String? = nil) -> String {
        "\(exact(value)) \(value == 1 ? noun : plural ?? noun + "s")"
    }
}

enum UsageColors {
    /// Each agent's colour, the same in every chart and list.
    static func agent(_ id: String) -> Color {
        switch id {
        case "codex": return adaptive(0x1F9E86, 0x3CC8A8)
        case "opencode": return adaptive(0x7B61E8, 0xA08CFF)
        case "claude": return adaptive(0xD9774F, 0xF0956A)
        case "copilot": return adaptive(0x2F6FDB, 0x6EA2FF)
        case "antigravity": return adaptive(0xB8901C, 0xE8C55A)
        case "goose": return adaptive(0x4A9D3F, 0x7BCB6E)
        case "kilo": return adaptive(0xD4508F, 0xF080B5)
        case "kimi": return adaptive(0x1E95B8, 0x5CCBE8)
        case "grok": return adaptive(0x5B5FD6, 0x8F93FF)
        case "pi": return adaptive(0x2E9C8C, 0x62D0C0)
        case "cline": return adaptive(0x6E7480, 0xA9AEB8)
        default: return adaptive(0x7D828C, 0xB4B9C2)
        }
    }

    /// Tool results and arguments: the two halves of what went through Mac MCP tools.
    static let results = adaptive(0x0A7CFF, 0x3D9BFF)
    static let arguments = adaptive(0x8FC0FF, 0x9CC8FF)
    static let warning = adaptive(0xC76A00, 0xFF9F0A)

    static func adaptive(_ light: UInt32, _ dark: UInt32) -> Color {
        func color(_ hex: UInt32) -> NSColor {
            NSColor(srgbRed: CGFloat((hex >> 16) & 0xFF) / 255, green: CGFloat((hex >> 8) & 0xFF) / 255,
                    blue: CGFloat(hex & 0xFF) / 255, alpha: 1)
        }
        return Color(nsColor: NSColor(name: nil) { appearance in
            appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? color(dark) : color(light)
        })
    }
}

/// The maker's logo for an agent (OpenAI for Codex), in a round tile; a quiet symbol when there is none.
struct UsageAgentLogo: View {
    let agent: String
    var size: CGFloat = 28

    private static var cache: [String: NSImage] = [:]

    var body: some View {
        Group {
            if let image = Self.logo(agent == "codex" ? "openai" : agent) {
                Image(nsImage: image).resizable().interpolation(.high).scaledToFit().padding(size * 0.2)
            } else {
                Image(systemName: "cpu").font(.system(size: size * 0.42, weight: .medium)).foregroundStyle(.secondary)
            }
        }
        .frame(width: size, height: size)
        .background(Circle().fill(Color(nsColor: .controlBackgroundColor)))
        .overlay(Circle().strokeBorder(Color.primary.opacity(0.09)))
        .accessibilityHidden(true)
    }

    static func logo(_ name: String) -> NSImage? {
        if let cached = cache[name] { return cached }
        guard let url = Bundle.main.url(forResource: name, withExtension: "png", subdirectory: "Onboarding/logos"),
              let image = NSImage(contentsOf: url) else { return nil }
        cache[name] = image
        return image
    }
}

/// A symbol in the same round tile, for rows that are not agents (tools).
struct UsageSymbolTile: View {
    let symbol: String
    var size: CGFloat = 28

    var body: some View {
        Image(systemName: symbol)
            .font(.system(size: size * 0.42, weight: .medium))
            .foregroundStyle(.secondary)
            .frame(width: size, height: size)
            .background(Circle().fill(Color(nsColor: .controlBackgroundColor)))
            .overlay(Circle().strokeBorder(Color.primary.opacity(0.09)))
            .accessibilityHidden(true)
    }
}

// MARK: Chart

struct UsageSeries: Identifiable {
    let id: String
    let name: String
    let color: Color
}

/// One bar: a day, a week, a month or a running total, split by series.
struct UsageBucket: Identifiable {
    let id: Int
    let title: String
    let axis: String
    let parts: [String: Int]
    var detail: String? = nil
    var total: Int { parts.values.reduce(0, +) }
}

/// Bars split by series with a dashed scale on the right; hovering a bar shows its numbers.
struct UsageBarChart: View {
    let buckets: [UsageBucket]
    let series: [UsageSeries]
    var spacing: CGFloat = 4
    var height: CGFloat = 150
    var label = "Tokens per day"

    @MacMCPState private var hovered: Int?

    private static let gutter: CGFloat = 48

    init(buckets: [UsageBucket], series: [UsageSeries], spacing: CGFloat = 4, height: CGFloat = 150, label: String = "Tokens per day") {
        self.buckets = buckets
        self.series = series
        self.spacing = spacing
        self.height = height
        self.label = label
    }

    var body: some View {
        let top = Self.scaleTop(buckets.map(\.total).max() ?? 0)
        VStack(alignment: .leading, spacing: 6) {
            ZStack(alignment: .topLeading) {
                ForEach([1.0, 0.5], id: \.self) { level in
                    scaleLine(UsageFormat.compact(Int(Double(top) * level)))
                        .offset(y: height * (1 - level))
                }
                HStack(alignment: .bottom, spacing: spacing) {
                    ForEach(buckets) { bucket in
                        bar(bucket, top: top, leading: bucket.id < buckets.count / 2)
                    }
                }
                .frame(height: height, alignment: .bottom)
                .padding(.trailing, Self.gutter)
            }
            .frame(height: height, alignment: .top)
            HStack {
                ForEach(Array(Self.axisPicks(buckets.count).enumerated()), id: \.offset) { index, pick in
                    if index > 0 { Spacer(minLength: 4) }
                    Text(buckets[pick].axis)
                }
            }
            .font(.system(size: 11)).foregroundStyle(.tertiary).monospacedDigit()
            .padding(.trailing, Self.gutter)
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel(label)
    }

    private func scaleLine(_ text: String) -> some View {
        ZStack(alignment: .topTrailing) {
            DashedLine().stroke(style: StrokeStyle(lineWidth: 1, dash: [3, 3]))
                .foregroundStyle(Color.primary.opacity(0.1))
                .frame(height: 1)
            Text(text).font(.system(size: 10.5)).foregroundStyle(.tertiary).monospacedDigit().padding(.top, 3)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }

    @ViewBuilder
    private func bar(_ bucket: UsageBucket, top: Int, leading: Bool) -> some View {
        if bucket.total == 0 {
            Circle().fill(Color.primary.opacity(0.14)).frame(width: 4, height: 4)
                .frame(maxWidth: .infinity)
                .accessibilityHidden(true)
        } else {
            let barHeight = max(3, height * CGFloat(bucket.total) / CGFloat(max(top, 1)))
            VStack(spacing: 0) {
                // The first series sits at the bottom, as the first row of the list below.
                ForEach(series.reversed()) { item in
                    if let value = bucket.parts[item.id], value > 0 {
                        Rectangle().fill(item.color)
                            .frame(height: max(1, barHeight * CGFloat(value) / CGFloat(bucket.total)))
                    }
                }
            }
            .frame(height: barHeight)
            .clipShape(RoundedRectangle(cornerRadius: 3, style: .continuous))
            .brightness(hovered == bucket.id ? 0.06 : 0)
            .frame(maxWidth: .infinity)
            .overlay(alignment: leading ? .topLeading : .topTrailing) {
                if hovered == bucket.id { tooltip(bucket).alignmentGuide(.top) { $0[.bottom] + 8 } }
            }
            .zIndex(hovered == bucket.id ? 1 : 0)
            .contentShape(Rectangle())
            .onHover { inside in
                if inside { hovered = bucket.id } else if hovered == bucket.id { hovered = nil }
            }
            .accessibilityElement()
            .accessibilityLabel("\(bucket.title): \(UsageFormat.compact(bucket.total)) tokens" + (bucket.detail.map { ", " + $0 } ?? ""))
        }
    }

    private func tooltip(_ bucket: UsageBucket) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text("\(bucket.title), \(UsageFormat.compact(bucket.total)) tokens").font(.system(size: 12, weight: .semibold))
            if let detail = bucket.detail { Text(detail).font(.system(size: 12)) }
            ForEach(series) { item in
                if let value = bucket.parts[item.id], value > 0 {
                    HStack(spacing: 6) {
                        RoundedRectangle(cornerRadius: 2).fill(item.color).frame(width: 7, height: 7)
                        Text("\(item.name) \(UsageFormat.compact(value))").font(.system(size: 12))
                    }
                }
            }
        }
        .padding(.horizontal, 9).padding(.vertical, 6)
        .background(RoundedRectangle(cornerRadius: 7, style: .continuous).fill(.regularMaterial))
        .overlay(RoundedRectangle(cornerRadius: 7, style: .continuous).strokeBorder(Color.primary.opacity(0.1)))
        .shadow(color: .black.opacity(0.18), radius: 8, y: 3)
        .fixedSize()
        .allowsHitTesting(false)
    }

    /// The top of the scale: a round number just above the tallest bar.
    static func scaleTop(_ value: Int) -> Int {
        guard value > 0 else { return 100 }
        let magnitude = pow(10, floor(log10(Double(value))))
        for step in [1.0, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10] where step * magnitude >= Double(value) {
            return Int(step * magnitude)
        }
        return Int(10 * magnitude)
    }

    static func axisPicks(_ count: Int) -> [Int] {
        guard count > 5 else { return Array(0..<count) }
        return [0, count / 4, count / 2, count * 3 / 4, count - 1]
    }
}

private struct DashedLine: Shape {
    func path(in rect: CGRect) -> Path {
        var path = Path()
        path.move(to: CGPoint(x: rect.minX, y: rect.midY))
        path.addLine(to: CGPoint(x: rect.maxX, y: rect.midY))
        return path
    }
}

// MARK: Facts and lists

/// A number with what it is and one plain sentence about it.
struct UsageFact: Identifiable {
    let value: String
    let title: String
    var note: String? = nil
    var warning = false
    var help: String? = nil
    var id: String { title }
}

/// Facts side by side with hairlines between them; in a box of their own, or bare inside another box.
struct UsageFactsRow: View {
    let facts: [UsageFact]
    var boxed = true

    init(facts: [UsageFact], boxed: Bool = true) {
        self.facts = facts
        self.boxed = boxed
    }

    var body: some View {
        let row = HStack(alignment: .top, spacing: 0) {
            ForEach(Array(facts.enumerated()), id: \.element.id) { index, fact in
                if index > 0 { Divider() }
                VStack(alignment: .leading, spacing: 2) {
                    Text(fact.value).font(.system(size: 15, weight: .semibold)).monospacedDigit()
                    Text(fact.title).font(.system(size: 12))
                    if let note = fact.note {
                        Text(note).font(.system(size: 11.5))
                            .foregroundStyle(fact.warning ? AnyShapeStyle(UsageColors.warning) : AnyShapeStyle(.secondary))
                            .fixedSize(horizontal: false, vertical: true)
                            .padding(.top, 2)
                    }
                }
                .padding(.horizontal, 14).padding(.vertical, 12)
                .frame(maxWidth: .infinity, alignment: .leading)
                .help(fact.help ?? "")
                .accessibilityElement(children: .combine)
            }
        }
        .fixedSize(horizontal: false, vertical: true)
        if boxed { row.usageListBox() } else { row }
    }
}

extension View {
    /// The quiet rounded box every Usage list sits in.
    func usageListBox() -> some View {
        background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color.primary.opacity(0.035)))
            .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(Color.primary.opacity(0.09)))
    }
}

/// A thin track with a share filled in its colour.
struct UsageShareMeter: View {
    let share: Double
    let color: Color
    var height: CGFloat = 6

    init(share: Double, color: Color, height: CGFloat = 6) {
        self.share = share
        self.color = color
        self.height = height
    }

    var body: some View {
        GeometryReader { proxy in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.primary.opacity(0.1))
                Capsule().fill(color).frame(width: share > 0 ? max(height, proxy.size.width * min(1, share)) : 0)
            }
        }
        .frame(height: height)
        .accessibilityHidden(true)
    }
}

/// A list row: tile, name and a line under it, a share meter, the amount and its percent, and a chevron when
/// it opens to more.
struct UsageRow<Icon: View, Detail: View>: View {
    let title: String
    let subtitle: Text
    let share: Double
    let color: Color
    let amount: String
    let percent: String
    var help: String?
    var open = false
    var toggle: (() -> Void)?
    let icon: Icon
    let detail: Detail

    init(title: String, subtitle: Text, share: Double, color: Color, amount: String, percent: String,
         help: String? = nil, open: Bool = false, toggle: (() -> Void)? = nil,
         @ViewBuilder icon: () -> Icon, @ViewBuilder detail: () -> Detail) {
        self.title = title
        self.subtitle = subtitle
        self.share = share
        self.color = color
        self.amount = amount
        self.percent = percent
        self.help = help
        self.open = open
        self.toggle = toggle
        self.icon = icon()
        self.detail = detail()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if let toggle {
                Button(action: toggle) { line }
                    .buttonStyle(.plain)
                    .accessibilityElement(children: .combine)
                    .accessibilityHint(open ? "Hides the details" : "Shows the details")
            } else {
                line.accessibilityElement(children: .combine)
            }
            if open {
                detail
                    .padding(.leading, 54).padding(.trailing, 14).padding(.bottom, 12)
                    .transition(.opacity)
            }
        }
    }

    private var line: some View {
                HStack(spacing: 12) {
                    icon
                    VStack(alignment: .leading, spacing: 1) {
                        Text(title).font(.system(size: 13, weight: .medium)).lineLimit(1).truncationMode(.middle)
                        subtitle.font(.system(size: 11.5)).foregroundStyle(.secondary).lineLimit(1)
                    }
                    .frame(minWidth: 130, maxWidth: 210, alignment: .leading)
                    UsageShareMeter(share: share, color: color)
                    VStack(alignment: .trailing, spacing: 1) {
                        Text(amount).font(.system(size: 13, weight: .medium)).monospacedDigit()
                        Text(percent).font(.system(size: 11)).foregroundStyle(.secondary).monospacedDigit()
                    }
                    .frame(width: 76, alignment: .trailing)
                    .help(help ?? "")
                    Image(systemName: "chevron.right")
                        .font(.system(size: 9, weight: .bold)).foregroundStyle(.tertiary)
                        .rotationEffect(.degrees(open ? 90 : 0))
                        .opacity(toggle == nil ? 0 : 1)
                        .frame(width: 10)
                }
                .padding(.horizontal, 14).padding(.vertical, 10)
                .contentShape(Rectangle())
    }
}

/// A setting as System Settings lists it: its name and one sentence on the left, its control on the right.
struct UsageSettingRow<Control: View>: View {
    let title: String
    var note: String?
    let control: Control

    init(title: String, note: String? = nil, @ViewBuilder control: () -> Control) {
        self.title = title
        self.note = note
        self.control = control()
    }

    var body: some View {
        HStack(alignment: .center, spacing: 16) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.system(size: 13))
                if let note {
                    Text(note).font(.system(size: 11.5)).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 12)
            control
        }
        .padding(.horizontal, 14).padding(.vertical, 10)
        .accessibilityElement(children: .contain)
    }
}

typealias SettingsRow<Control: View> = UsageSettingRow<Control>

/// The type sizes Settings is set in, the same as Usage: a row title, a note under it, and quieter metadata.
enum SettingsType {
    static let title = Font.system(size: 13, weight: .semibold)
    static let body = Font.system(size: 13)
    static let note = Font.system(size: 11.5)
    static let meta = Font.system(size: 11)
}

/// Every GroupBox in Settings: its title above, in the Usage tabs' type, and its content in the same quiet box.
struct SettingsGroupStyle: GroupBoxStyle {
    func makeBody(configuration: Configuration) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            configuration.label
                .labelStyle(.titleOnly)
                .font(SettingsType.title)
                .foregroundStyle(.primary)
            configuration.content
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 14).padding(.vertical, 12)
                .usageListBox()
        }
    }
}

/// A status the way Connections › Browser shows it: a small dot in its colour and a quiet word.
struct StatusDot: View {
    let text: String
    let color: Color

    var body: some View {
        HStack(spacing: 5) {
            Circle().fill(color).frame(width: 6, height: 6).accessibilityHidden(true)
            Text(text).font(SettingsType.meta).foregroundStyle(.secondary)
        }
        .fixedSize()
    }
}

/// A quiet tag after a row's title ("Local", "Changes things").
struct SettingsBadge: View {
    let text: String

    var body: some View {
        Text(text).font(SettingsType.meta).foregroundStyle(.secondary)
            .padding(.horizontal, 5).padding(.vertical, 1)
            .background(Capsule().fill(Color.primary.opacity(0.08)))
            .fixedSize()
    }
}

/// A small coloured dot before the text, for a status line ("Paused: …").
struct DotLabelStyle: LabelStyle {
    func makeBody(configuration: Configuration) -> some View {
        HStack(spacing: 6) {
            configuration.icon.font(.system(size: 6)).alignmentGuide(.firstTextBaseline) { $0[.bottom] - 1 }
            configuration.title
        }
    }
}
