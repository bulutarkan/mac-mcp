// Native Accessibility observation for Mac MCP.
//
// The AppleScript observer reads every attribute of every node with its own
// System Events Apple Event, which takes seconds on large windows. This helper
// walks the same tree with the Accessibility API, fetching each node's
// attributes in one AXUIElementCopyMultipleAttributeValues call, and prints the
// exact record format the Python parser already reads (__META__, __WINDOW__,
// __NODE__ records separated by ASCII 30/31).
//
// Usage: ax_observe (--pid N | --name NAME | --frontmost) --window W
//                   --max-depth D --max-children C --max-nodes N
import AppKit
import ApplicationServices
import Foundation

let fieldSeparator = "\u{1F}"
let recordSeparator = "\u{1E}"

func clean(_ text: String) -> String {
    var out = ""
    out.reserveCapacity(min(text.count, 4000))
    for scalar in text.unicodeScalars {
        switch scalar {
        case "\r", "\n", "\t", "\u{1F}", "\u{1E}": out.unicodeScalars.append(" ")
        default: out.unicodeScalars.append(scalar)
        }
    }
    return out.count > 4000 ? String(out.prefix(4000)) : out
}

func number(_ value: Double) -> String {
    // System Events reports geometry truncated toward zero.
    guard value.isFinite else { return "" }
    return String(Int(value))
}

func text(_ value: AnyObject?) -> String {
    guard let value else { return "" }
    if let string = value as? String { return string }
    if let attributed = value as? NSAttributedString { return attributed.string }
    if CFGetTypeID(value) == CFBooleanGetTypeID() { return (value as! Bool) ? "true" : "false" }
    if let num = value as? NSNumber {
        if CFNumberIsFloatType(num) {
            let d = num.doubleValue
            return d == d.rounded() && abs(d) < 1e15 ? String(format: "%.1f", d) : String(d)
        }
        return num.stringValue
    }
    if let url = value as? URL { return url.absoluteString }
    return ""
}

func bool(_ value: AnyObject?) -> String {
    guard let value, CFGetTypeID(value) == CFBooleanGetTypeID() else { return "false" }
    return (value as! Bool) ? "true" : "false"
}

func point(_ value: AnyObject?) -> (String, String) {
    guard let value, CFGetTypeID(value) == AXValueGetTypeID() else { return ("", "") }
    var p = CGPoint.zero
    guard AXValueGetValue(value as! AXValue, .cgPoint, &p) else { return ("", "") }
    return (number(Double(p.x)), number(Double(p.y)))
}

func size(_ value: AnyObject?) -> (String, String) {
    guard let value, CFGetTypeID(value) == AXValueGetTypeID() else { return ("", "") }
    var s = CGSize.zero
    guard AXValueGetValue(value as! AXValue, .cgSize, &s) else { return ("", "") }
    return (number(Double(s.width)), number(Double(s.height)))
}

func attributes(_ element: AXUIElement, _ names: [String]) -> [String: AnyObject] {
    var values: CFArray?
    let status = AXUIElementCopyMultipleAttributeValues(element, names as CFArray, AXCopyMultipleAttributeOptions(rawValue: 0), &values)
    var out: [String: AnyObject] = [:]
    guard status == .success, let array = values as? [AnyObject] else { return out }
    for (index, name) in names.enumerated() where index < array.count {
        let item = array[index]
        // Missing attributes come back as AXValue-wrapped AXError placeholders.
        if CFGetTypeID(item) == AXValueGetTypeID(), AXValueGetType(item as! AXValue) == .axError { continue }
        out[name] = item
    }
    return out
}

let nodeAttributes = [
    "AXRole", "AXSubrole", "AXTitle", "AXDescription", "AXRoleDescription", "AXValue",
    "AXPosition", "AXSize", "AXEnabled", "AXFocused", "AXChildren", "AXIdentifier",
]

var records: [String] = []
var counter = 0
// Set when the node budget, not depth or the child limit, stopped the walk.
var budgetHit = false

func record(_ fields: [String]) { records.append(fields.joined(separator: fieldSeparator)) }

func walk(_ element: AXUIElement, id: String, parent: String, depth: Int, maxDepth: Int, maxChildren: Int, maxNodes: Int) {
    if counter >= maxNodes { budgetHit = true; return }
    counter += 1
    let a = attributes(element, nodeAttributes)
    let role = text(a["AXRole"]), subrole = text(a["AXSubrole"])
    var value = text(a["AXValue"])
    if role.contains("SecureText") || subrole.contains("Secure") { value = "[redacted]" }
    // System Events' "description" falls back to the role description.
    var description = text(a["AXDescription"])
    if description.isEmpty { description = text(a["AXRoleDescription"]) }
    let (x, y) = point(a["AXPosition"])
    let (w, h) = size(a["AXSize"])
    var actionNames: CFArray?
    var actions = ""
    if AXUIElementCopyActionNames(element, &actionNames) == .success, let names = actionNames as? [String] {
        actions = names.joined(separator: ", ")
    }
    let children = (a["AXChildren"] as? [AXUIElement]) ?? []
    record([
        "__NODE__", clean(id), clean(parent), clean(role), clean(subrole), clean(text(a["AXTitle"])),
        clean(description), clean(value), x, y, w, h, bool(a["AXEnabled"]), bool(a["AXFocused"]),
        clean(actions), String(children.count), clean(text(a["AXIdentifier"])),
    ])
    if depth >= maxDepth { return }
    for (index, child) in children.enumerated() {
        if index >= maxChildren { break }
        if counter >= maxNodes { budgetHit = true; break }
        walk(child, id: "\(id)/\(index + 1)", parent: id, depth: depth + 1, maxDepth: maxDepth, maxChildren: maxChildren, maxNodes: maxNodes)
    }
}

var options: [String: String] = [:]
var frontmost = false
var argIndex = 1
let args = CommandLine.arguments
while argIndex < args.count {
    let key = args[argIndex]
    if key == "--frontmost" { frontmost = true; argIndex += 1; continue }
    if argIndex + 1 < args.count { options[key] = args[argIndex + 1] }
    argIndex += 2
}

guard AXIsProcessTrusted() else {
    FileHandle.standardError.write("Accessibility permission is not granted (-25211)\n".data(using: .utf8)!)
    exit(3)
}

let running = NSWorkspace.shared.runningApplications
let app: NSRunningApplication?
if let pidText = options["--pid"], let pid = Int32(pidText) {
    app = NSRunningApplication(processIdentifier: pid)
} else if let name = options["--name"] {
    let matches = running.filter { processName($0) == name }
    app = matches.count == 1 ? matches[0] : nil
} else if frontmost {
    app = NSWorkspace.shared.frontmostApplication
} else {
    app = nil
}
guard let app else {
    FileHandle.standardError.write("Target application not found or ambiguous (-1728)\n".data(using: .utf8)!)
    exit(4)
}

let windowIndex = Int(options["--window"] ?? "0") ?? 0
let maxDepth = Int(options["--max-depth"] ?? "5") ?? 5
let maxChildren = Int(options["--max-children"] ?? "30") ?? 30
let maxNodes = Int(options["--max-nodes"] ?? "500") ?? 500

let appElement = AXUIElementCreateApplication(app.processIdentifier)
AXUIElementSetMessagingTimeout(appElement, 2.0)
var windowsValue: CFTypeRef?
AXUIElementCopyAttributeValue(appElement, "AXWindows" as CFString, &windowsValue)
// System Events lists only real windows (Finder's desktop is an AXScrollArea).
let windows = ((windowsValue as? [AXUIElement]) ?? []).filter { text(attributes($0, ["AXRole"])["AXRole"]) == "AXWindow" }
var names: [String] = []
var windowRecords: [(Int, AXUIElement, [String])] = []
for (offset, window) in windows.enumerated() {
    let wi = offset + 1
    let a = attributes(window, ["AXTitle", "AXDocument", "AXIdentifier", "AXPosition", "AXSize", "AXSubrole", "AXFocused", "AXMain"])
    let title = text(a["AXTitle"])
    if !title.isEmpty { names.append(title) }
    let (x, y) = point(a["AXPosition"])
    let (w, h) = size(a["AXSize"])
    windowRecords.append((wi, window, [
        "__WINDOW__", String(wi), clean(title), clean(text(a["AXDocument"])), clean(text(a["AXIdentifier"])),
        x, y, w, h, clean(text(a["AXSubrole"])), bool(a["AXFocused"]), bool(a["AXMain"]),
    ]))
}
// System Events names a process by its bundle name, else its executable.
func processName(_ app: NSRunningApplication) -> String {
    if let url = app.bundleURL, let name = Bundle(url: url)?.infoDictionary?["CFBundleName"] as? String, !name.isEmpty {
        return name
    }
    return app.executableURL?.lastPathComponent ?? app.localizedName ?? ""
}

record([
    "__META__", clean(processName(app)), app.isActive ? "true" : "false", String(windows.count),
    clean(names.joined(separator: " || ")), String(app.processIdentifier), clean(app.bundleIdentifier ?? ""),
])
// Each walked window gets an equal share of what is left, so a large first
// window cannot starve the rest; a small one leaves its unused share to later ones.
var usedNodes = 0
var remainingWindows = windowRecords.filter { windowIndex == 0 || $0.0 == windowIndex }.count
for (wi, window, fields) in windowRecords {
    record(fields)
    if windowIndex == 0 || wi == windowIndex {
        let budget = max(0, (maxNodes - usedNodes) / max(1, remainingWindows))
        counter = 0
        budgetHit = budget == 0
        if budget > 0 {
            walk(window, id: "w\(wi)", parent: "", depth: 0, maxDepth: maxDepth, maxChildren: maxChildren, maxNodes: budget)
        }
        usedNodes += counter
        remainingWindows -= 1
        record(["__BUDGET__", String(wi), String(counter), String(budget), budgetHit ? "true" : "false"])
    }
}
FileHandle.standardOutput.write(records.joined(separator: recordSeparator).data(using: .utf8)!)
