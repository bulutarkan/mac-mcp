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
//        ax_observe --mode act --pid N --element ID --op OP ...   (see act())
//        ax_observe --serve
//
// --serve keeps one process for many observations (#185): each stdin line is a JSON
// request {"id": N, "args": [the arguments above]}, each answer one stdout line
// {"id": N, "code": exit code, "out": records, "err": message}. Mac MCP starts it on
// first use and stops it when idle, so an observation no longer pays for a launch.
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

// MARK: Windows on desktops the person is not looking at
//
// AXWindows lists only the windows on the desktop each display shows, so a window an agent
// keeps on a desktop of its own (Mission Control) is invisible to it. Accessibility still
// answers for such a window when asked by its element id, which a remote token names; the
// ids are small integers, so the missing windows are found by trying ids until every one
// the window server reports is matched (the technique yabai and AltTab use).

typealias CreateWithRemoteTokenFn = @convention(c) (CFData) -> Unmanaged<AXUIElement>?
typealias GetWindowFn = @convention(c) (AXUIElement, UnsafeMutablePointer<UInt32>) -> AXError
typealias WindowIsOrderedInFn = @convention(c) (Int32, UInt32, UnsafeMutablePointer<UInt8>) -> Int32
typealias MainConnectionFn = @convention(c) () -> Int32

let axLibrary = dlopen("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices", RTLD_LAZY)
let skyLight = dlopen("/System/Library/PrivateFrameworks/SkyLight.framework/Versions/A/SkyLight", RTLD_LAZY)
let createWithRemoteToken = axLibrary.flatMap { dlsym($0, "_AXUIElementCreateWithRemoteToken") }
    .map { unsafeBitCast($0, to: CreateWithRemoteTokenFn.self) }
let getWindow = axLibrary.flatMap { dlsym($0, "_AXUIElementGetWindow") }.map { unsafeBitCast($0, to: GetWindowFn.self) }
let windowIsOrderedIn = skyLight.flatMap { dlsym($0, "SLSWindowIsOrderedIn") }.map { unsafeBitCast($0, to: WindowIsOrderedInFn.self) }
let mainConnection = skyLight.flatMap { dlsym($0, "SLSMainConnectionID") }.map { unsafeBitCast($0, to: MainConnectionFn.self) }

func windowID(_ window: AXUIElement) -> UInt32 {
    var id: UInt32 = 0
    guard let getWindow, getWindow(window, &id) == .success else { return 0 }
    return id
}

/// Shown, ordinary windows of a process by window-server id: what the person or an agent
/// could look at on some desktop. Closed-but-kept and minimized windows are not shown.
func shownWindowIDs(pid: pid_t) -> Set<UInt32> {
    guard let windowIsOrderedIn, let mainConnection else { return [] }
    let connection = mainConnection()
    let rows = CGWindowListCopyWindowInfo([.optionAll, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? []
    var out = Set<UInt32>()
    for row in rows {
        guard (row[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == pid,
              (row[kCGWindowLayer as String] as? Int) == 0,
              let id = (row[kCGWindowNumber as String] as? NSNumber)?.uint32Value,
              let bounds = row[kCGWindowBounds as String] as? [String: Any],
              ((bounds["Width"] as? NSNumber)?.doubleValue ?? 0) >= 50,
              ((bounds["Height"] as? NSNumber)?.doubleValue ?? 0) >= 50 else { continue }
        var shown: UInt8 = 0
        if windowIsOrderedIn(connection, id, &shown) == 0, shown != 0 { out.insert(id) }
    }
    return out
}

/// Accessibility elements for shown windows of `pid` that AXWindows left out, in window-id order.
func offSpaceWindows(pid: pid_t, known: Set<UInt32>) -> [AXUIElement] {
    guard let createWithRemoteToken else { return [] }
    var missing = shownWindowIDs(pid: pid).subtracting(known)
    if missing.isEmpty { return [] }
    var found: [(UInt32, AXUIElement)] = []
    // The app's main and focused windows are answered wherever they are. Asking also makes
    // the app register a window that opened straight onto another desktop: until some
    // client has touched it, no element id names it and the search below cannot find it.
    let appElement = AXUIElementCreateApplication(pid)
    AXUIElementSetMessagingTimeout(appElement, 1.0)
    for name in ["AXMainWindow", "AXFocusedWindow"] {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(appElement, name as CFString, &value) == .success,
              let value, CFGetTypeID(value) == AXUIElementGetTypeID() else { continue }
        let window = value as! AXUIElement
        let id = windowID(window)
        if missing.remove(id) != nil { found.append((id, window)) }
    }
    var token = Data(count: 0x14)
    token.withUnsafeMutableBytes { bytes in
        bytes.storeBytes(of: UInt32(bitPattern: pid), toByteOffset: 0x0, as: UInt32.self)
        bytes.storeBytes(of: UInt32(0x636f636f), toByteOffset: 0x8, as: UInt32.self)
    }
    for elementID in UInt64(0)..<0x7fff where !missing.isEmpty {
        token.withUnsafeMutableBytes { $0.storeBytes(of: elementID, toByteOffset: 0xc, as: UInt64.self) }
        guard let element = createWithRemoteToken(token as CFData)?.takeRetainedValue() else { continue }
        AXUIElementSetMessagingTimeout(element, 0.5)
        var role: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, "AXRole" as CFString, &role) == .success,
              (role as? String) == "AXWindow" else { continue }
        let id = windowID(element)
        if missing.remove(id) != nil { found.append((id, element)) }
    }
    return found.sorted { $0.0 < $1.0 }.map { $0.1 }
}

let nodeAttributes = [
    "AXRole", "AXSubrole", "AXTitle", "AXDescription", "AXRoleDescription", "AXValue",
    "AXPosition", "AXSize", "AXEnabled", "AXFocused", "AXChildren", "AXIdentifier",
    "AXSelected", "AXPlaceholderValue", "AXHelp",
]

var records: [String] = []
var counter = 0
// Set when the node budget, not depth or the child limit, stopped the walk.
var budgetHit = false

func record(_ fields: [String]) { records.append(fields.joined(separator: fieldSeparator)) }

// A wrapper that only holds one thing (no action, no value) costs no depth: web views and Electron apps nest
// their content under many of them, and max_depth ran out before the page (#82, the ChatGPT app). Its id path
// is kept, so element ids stay stable. rawDepth bounds a chain of wrappers.
// Every web element offers AXShowMenu and AXScrollToVisible; they do not make a wrapper something to act on.
let passThroughRoles: Set<String> = ["AXGroup", "AXScrollArea", "AXSplitGroup", "AXLayoutArea", "AXUnknown"]
let incidentalActions: Set<String> = ["AXShowMenu", "AXScrollToVisible"]
let maxRawDepth = 64

func passesThrough(role: String, value: String, actions: [String], childCount: Int) -> Bool {
    childCount == 1 && value.isEmpty && passThroughRoles.contains(role)
        && actions.allSatisfy { incidentalActions.contains($0) }
}

func walk(_ element: AXUIElement, id: String, parent: String, depth: Int, maxDepth: Int, maxChildren: Int, maxNodes: Int,
          rawDepth: Int = 0) {
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
    var actionList: [String] = []
    if AXUIElementCopyActionNames(element, &actionNames) == .success, let names = actionNames as? [String] {
        actionList = names
    }
    let actions = actionList.joined(separator: ", ")
    let children = (a["AXChildren"] as? [AXUIElement]) ?? []
    record([
        "__NODE__", clean(id), clean(parent), clean(role), clean(subrole), clean(text(a["AXTitle"])),
        clean(description), clean(value), x, y, w, h, bool(a["AXEnabled"]), bool(a["AXFocused"]),
        clean(actions), String(children.count), clean(text(a["AXIdentifier"])),
        // Empty when the element has no AXSelected at all, so "unsupported" differs from "false".
        a["AXSelected"] == nil ? "" : bool(a["AXSelected"]),
        clean(text(a["AXPlaceholderValue"])), clean(text(a["AXHelp"])),
    ])
    let through = passesThrough(role: role, value: value, actions: actionList, childCount: children.count)
    if (depth >= maxDepth && !through) || rawDepth >= maxRawDepth { return }
    for (index, child) in children.enumerated() {
        if index >= maxChildren { break }
        if counter >= maxNodes { budgetHit = true; break }
        walk(child, id: "\(id)/\(index + 1)", parent: id, depth: through ? depth : depth + 1, maxDepth: maxDepth,
             maxChildren: maxChildren, maxNodes: maxNodes, rawDepth: rawDepth + 1)
    }
}

// System Events names a process by its bundle name, else its executable.
func processName(_ app: NSRunningApplication) -> String {
    if let url = app.bundleURL, let name = Bundle(url: url)?.infoDictionary?["CFBundleName"] as? String, !name.isEmpty {
        return name
    }
    return app.executableURL?.lastPathComponent ?? app.localizedName ?? ""
}

func parseArguments(_ args: [String]) -> ([String: String], Bool) {
    var options: [String: String] = [:]
    var frontmost = false
    var argIndex = 0
    while argIndex < args.count {
        let key = args[argIndex]
        if key == "--frontmost" { frontmost = true; argIndex += 1; continue }
        if argIndex + 1 < args.count { options[key] = args[argIndex + 1] }
        argIndex += 2
    }
    return (options, frontmost)
}

/// One observation as (exit code, records, error): 3 = Accessibility not granted,
/// 4 = target app not found or ambiguous, 0 = the records.
func observe(_ options: [String: String], frontmost: Bool) -> (Int32, String, String) {
    guard AXIsProcessTrusted() else {
        return (3, "", "Accessibility permission is not granted (-25211)")
    }
    let app: NSRunningApplication?
    if let pidText = options["--pid"], let pid = Int32(pidText) {
        app = NSRunningApplication(processIdentifier: pid)
    } else if let name = options["--name"] {
        // The same app answers to its bundle name, its displayed name, its executable name
        // (what System Events calls some processes, e.g. MacMCPMenu) and its bundle id.
        let wanted = name.lowercased()
        let matches = NSWorkspace.shared.runningApplications.filter { candidate in
            [processName(candidate), candidate.localizedName, candidate.executableURL?.lastPathComponent,
             candidate.bundleIdentifier].contains { $0?.lowercased() == wanted }
        }
        app = matches.count == 1 ? matches[0] : nil
    } else if frontmost {
        app = NSWorkspace.shared.frontmostApplication
    } else {
        app = nil
    }
    guard let app else {
        return (4, "", "Target application not found or ambiguous (-1728)")
    }

    let windowIndex = Int(options["--window"] ?? "0") ?? 0
    let maxDepth = Int(options["--max-depth"] ?? "5") ?? 5
    let maxChildren = Int(options["--max-children"] ?? "30") ?? 30
    let maxNodes = Int(options["--max-nodes"] ?? "500") ?? 500

    records = []
    counter = 0
    budgetHit = false
    let appElement = AXUIElementCreateApplication(app.processIdentifier)
    AXUIElementSetMessagingTimeout(appElement, 2.0)
    var windowsValue: CFTypeRef?
    AXUIElementCopyAttributeValue(appElement, "AXWindows" as CFString, &windowsValue)
    // System Events lists only real windows (Finder's desktop is an AXScrollArea).
    let onSpace = ((windowsValue as? [AXUIElement]) ?? []).filter { text(attributes($0, ["AXRole"])["AXRole"]) == "AXWindow" }
    // Windows on other desktops come after, so the numbers of on-screen windows (the ones
    // System Events also numbers) never change.
    let offSpace = offSpaceWindows(pid: app.processIdentifier, known: Set(onSpace.map(windowID)))
    let windows = onSpace + offSpace
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
            // Trailing fields older parsers ignore: on the shown desktop, and the window-server id.
            offset < onSpace.count ? "true" : "false", String(windowID(window)),
        ]))
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
    let out = records.joined(separator: recordSeparator)
    records = []
    return (0, out, "")
}

// MARK: Acting on a window no display shows
//
// System Events reaches only windows on a shown desktop, so mac_act sends element-bound
// Accessibility actions for a window on another desktop (an agent's own) here instead:
//   --mode act --pid N --element wW/i/j --op press|action|set_text [--name AXName]
//   [--text T --replace true|false]
// Windows are numbered as in observe() (shown desktop first, then the others), and each
// path step is an AXChildren index, so an element_id from an observation names the same
// element. The answer is JSON: whether it ran, and what changed.

/// Every window of the app in observe()'s order.
func allWindows(_ app: NSRunningApplication) -> [AXUIElement] {
    let appElement = AXUIElementCreateApplication(app.processIdentifier)
    AXUIElementSetMessagingTimeout(appElement, 2.0)
    var windowsValue: CFTypeRef?
    AXUIElementCopyAttributeValue(appElement, "AXWindows" as CFString, &windowsValue)
    let onSpace = ((windowsValue as? [AXUIElement]) ?? []).filter { text(attributes($0, ["AXRole"])["AXRole"]) == "AXWindow" }
    return onSpace + offSpaceWindows(pid: app.processIdentifier, known: Set(onSpace.map(windowID)))
}

func resolve(_ elementID: String, in windows: [AXUIElement]) -> AXUIElement? {
    let parts = elementID.split(separator: "/").map(String.init)
    guard let first = parts.first, first.hasPrefix("w"), let wi = Int(first.dropFirst()), wi >= 1, wi <= windows.count else {
        return nil
    }
    var element = windows[wi - 1]
    for part in parts.dropFirst() {
        guard let index = Int(part), index >= 1 else { return nil }
        let children = (attributes(element, ["AXChildren"])["AXChildren"] as? [AXUIElement]) ?? []
        guard index <= children.count else { return nil }
        element = children[index - 1]
    }
    return element
}

/// A cheap summary of a window's visible structure and values, to tell whether an action did anything.
func fingerprint(_ element: AXUIElement, depth: Int = 0, budget: inout Int) -> String {
    if budget <= 0 || depth > 8 { return "" }
    budget -= 1
    let a = attributes(element, ["AXRole", "AXTitle", "AXValue", "AXSelected", "AXEnabled", "AXChildren"])
    var out = "\(text(a["AXRole"]))|\(text(a["AXTitle"]))|\(text(a["AXValue"]).prefix(200))|\(bool(a["AXSelected"]))|\(bool(a["AXEnabled"]))"
    let children = (a["AXChildren"] as? [AXUIElement]) ?? []
    out += "[\(children.count)]"
    for child in children.prefix(40) { out += "(" + fingerprint(child, depth: depth + 1, budget: &budget) + ")" }
    return out
}

func windowFingerprint(_ app: NSRunningApplication, _ elementID: String) -> String {
    guard let windowPart = elementID.split(separator: "/").first, let window = resolve(String(windowPart), in: allWindows(app)) else {
        return ""
    }
    var budget = 400
    return fingerprint(window, budget: &budget)
}

func act(_ options: [String: String]) -> (Int32, String, String) {
    guard AXIsProcessTrusted() else { return (3, "", "Accessibility permission is not granted (-25211)") }
    guard let pidText = options["--pid"], let pid = Int32(pidText), let app = NSRunningApplication(processIdentifier: pid) else {
        return (4, "", "Target application not found (-1728)")
    }
    let elementID = options["--element"] ?? ""
    let op = options["--op"] ?? ""
    func answer(_ object: [String: Any]) -> (Int32, String, String) {
        let data = (try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])) ?? Data("{}".utf8)
        return (0, String(decoding: data, as: UTF8.self), "")
    }
    guard let element = resolve(elementID, in: allWindows(app)) else {
        return answer(["ok": false, "reason_code": "STALE_ELEMENT_PATH", "error": "\(elementID) no longer exists; observe again"])
    }
    AXUIElementSetMessagingTimeout(element, 5.0)
    // A path names a position, and positions shift when the app rearranges its window (a
    // calculator's AC becoming C moves every key): act only on the element that was observed.
    let current = attributes(element, ["AXRole", "AXTitle", "AXDescription", "AXRoleDescription"])
    var currentDescription = text(current["AXDescription"])
    if currentDescription.isEmpty { currentDescription = text(current["AXRoleDescription"]) }
    let expected: [(String, String, String)] = [
        ("--expect-role", "role", text(current["AXRole"])),
        ("--expect-title", "title", clean(text(current["AXTitle"]))),
        ("--expect-description", "description", clean(currentDescription)),
    ]
    for (option, label, actual) in expected {
        if let wanted = options[option], wanted != actual {
            return answer(["ok": false, "reason_code": "STALE_ELEMENT_PATH",
                           "error": "\(elementID) is now a different element (\(label) '\(actual)', observed '\(wanted)'); observe again"])
        }
    }
    switch op {
    case "press", "action":
        let name = op == "press" ? "AXPress" : (options["--name"] ?? "")
        var names: CFArray?
        AXUIElementCopyActionNames(element, &names)
        guard ((names as? [String]) ?? []).contains(name) else {
            return answer(["ok": false, "reason_code": "ACCESSIBILITY_ACTION_UNAVAILABLE",
                           "error": "\(elementID) does not offer \(name)", "actions": (names as? [String]) ?? []])
        }
        let before = windowFingerprint(app, elementID)
        let status = AXUIElementPerformAction(element, name as CFString)
        guard status == .success else {
            return answer(["ok": false, "reason_code": "ACCESSIBILITY_ACTION_FAILED", "error": "\(name) failed (\(status.rawValue))"])
        }
        // Let the app react, then compare; a press that closes or replaces the window also counts.
        var changed = false
        let deadline = Date().addingTimeInterval(1.0)
        repeat {
            usleep(80_000)
            changed = windowFingerprint(app, elementID) != before
        } while !changed && Date() < deadline
        return answer(["ok": true, "performed": name, "effect_observed": changed])
    case "set_text":
        let replace = (options["--replace"] ?? "true") == "true"
        let attribute = replace ? "AXValue" : "AXSelectedText"
        var settable = DarwinBoolean(false)
        guard AXUIElementIsAttributeSettable(element, attribute as CFString, &settable) == .success, settable.boolValue else {
            return answer(["ok": false, "reason_code": "SEMANTIC_ATTRIBUTE_UNAVAILABLE", "error": "\(attribute) cannot be set on \(elementID)"])
        }
        let wanted = options["--text"] ?? ""
        let before = text(attributes(element, ["AXValue"])["AXValue"])
        let status = AXUIElementSetAttributeValue(element, attribute as CFString, wanted as CFString)
        guard status == .success else {
            return answer(["ok": false, "reason_code": "SEMANTIC_TEXT_WRITE_FAILED", "error": "\(attribute) write failed (\(status.rawValue))"])
        }
        let after = text(attributes(element, ["AXValue"])["AXValue"])
        // Replacing must leave exactly the text; inserting must leave it somewhere in the value.
        let verified = replace ? after == wanted : (after != before && after.contains(wanted))
        return answer(["ok": verified, "attribute": attribute, "effect_observed": after != before, "verified": verified]
            .merging(verified ? [:] : ["reason_code": "ACTION_NO_EFFECT", "error": "the text did not read back as written"]) { a, _ in a })
    default:
        return (2, "", "unknown op \(op)")
    }
}

/// --serve: one JSON request per stdin line, one JSON answer per stdout line, until stdin closes.
func serve() -> Never {
    while let line = readLine(strippingNewline: true) {
        // Nothing drains autoreleased objects on this thread on its own: without a pool per
        // request, every answer (~100 KB for 500 nodes) would stay in memory.
        autoreleasepool { answer(line) }
    }
    exit(0)
}

func answer(_ line: String) {
    var response: [String: Any] = ["id": NSNull(), "code": 2, "out": "", "err": "bad request"]
    if let data = line.data(using: .utf8),
       let request = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
       let args = request["args"] as? [String] {
        response["id"] = request["id"] ?? NSNull()
        // NSWorkspace updates its running and frontmost apps on the run loop; a process
        // that never runs it would keep the list from its launch. Let pending updates in.
        RunLoop.current.run(mode: .default, before: Date())
        let (options, frontmost) = parseArguments(args)
        let (code, out, err) = options["--mode"] == "act" ? act(options) : observe(options, frontmost: frontmost)
        response["code"] = Int(code)
        response["out"] = out
        response["err"] = err
    }
    if var bytes = try? JSONSerialization.data(withJSONObject: response) {
        bytes.append(0x0A)
        FileHandle.standardOutput.write(bytes)
    }
}

let arguments = Array(CommandLine.arguments.dropFirst())
if arguments.first == "--serve" { serve() }
let (cliOptions, cliFrontmost) = parseArguments(arguments)
let (exitCode, output, errorText) = cliOptions["--mode"] == "act" ? act(cliOptions) : observe(cliOptions, frontmost: cliFrontmost)
if exitCode != 0 {
    FileHandle.standardError.write((errorText + "\n").data(using: .utf8)!)
    exit(exitCode)
}
FileHandle.standardOutput.write(output.data(using: .utf8)!)
