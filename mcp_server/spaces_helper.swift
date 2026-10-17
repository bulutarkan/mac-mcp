// Native macOS Spaces (Mission Control desktops) for Mac MCP.
//
// Lets an agent work on a desktop of its own while the person works on theirs, when the
// person asks for that: list displays and their desktops with the windows on each, add
// and remove desktops, and move windows between desktops without switching the person's
// view or focus.
//
// Usage: spaces_helper spaces
//        spaces_helper window-spaces WID [WID...]
//        spaces_helper move SPACE_ID WID [WID...]
//        spaces_helper open SPACE_ID APP [FILE_OR_URL...]   (APP: bundle id, name or .app path)
//        spaces_helper assign SPACE_ID APP   (SPACE_ID 0 lifts the assignment)
//        spaces_helper catch SPACE_ID APP [SECONDS]   (prints {"ready":true}, then moves the app's next new window)
//        spaces_helper create DISPLAY_ID
//        spaces_helper remove SPACE_ID
// Every command prints one JSON object; exit 0 = answered, 2 = bad arguments,
// 5 = a private macOS interface this needs is missing.
//
// Apple has no public Spaces API. Reads use exported SkyLight functions. Moving uses
// the bridged window-management operation yabai (MIT) switched to in v7.1.25, the
// only move that still works with SIP enabled on macOS 15 and 26: the older
// SLSMoveWindowsToManagedSpace and compat-id calls return normally and move nothing.
// Adding and removing desktops needs SIP off for an API, so it presses Mission
// Control's own "add desktop" button and "remove desktop" action through
// Accessibility, opening Mission Control only for as long as that takes.
// Everything is looked up at run time, so a macOS that drops a symbol yields exit 5
// instead of a helper that fails to launch.
import AppKit
import ApplicationServices
import Foundation
import MachO

// The bridged move silently does nothing unless AppKit has set up this process's
// WindowServer connection, and swiftc drops AppKit when no symbol of it is used. The
// shared application is never run or activated, so the helper gets no Dock icon or focus.
_ = NSApplication.shared

let skyLightPath = "/System/Library/PrivateFrameworks/SkyLight.framework/Versions/A/SkyLight"
let skyLight = dlopen(skyLightPath, RTLD_LAZY)
let appServices = dlopen("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices", RTLD_LAZY)

func exported<T>(_ name: String, as type: T.Type, from image: UnsafeMutableRawPointer? = skyLight) -> T? {
    guard let image, let pointer = dlsym(image, name) else { return nil }
    return unsafeBitCast(pointer, to: type)
}

typealias MainConnectionFn = @convention(c) () -> Int32
typealias CopyDisplaySpacesFn = @convention(c) (Int32) -> Unmanaged<CFArray>?
typealias CurrentSpaceFn = @convention(c) (Int32, CFString) -> UInt64
typealias CopySpacesForWindowsFn = @convention(c) (Int32, Int32, CFArray) -> Unmanaged<CFArray>?
typealias WindowIsOrderedInFn = @convention(c) (Int32, UInt32, UnsafeMutablePointer<UInt8>) -> Int32
typealias ProcessAssignToSpaceFn = @convention(c) (Int32, Int32, UInt64) -> Int32
typealias PerformOperationFn = @convention(c) (AnyObject) -> Int64
typealias InitMoveFn = @convention(c) (AnyObject, Selector, NSArray, UInt64) -> AnyObject?
typealias CoreDockSendFn = @convention(c) (CFString, Int32) -> Void

/// A symbol SkyLight keeps out of its export table, found in the loaded image's own symbol table.
func localSymbol(image: String, symbol: String) -> UnsafeMutableRawPointer? {
    for index in 0..<_dyld_image_count() {
        guard let name = _dyld_get_image_name(index), String(cString: name) == image,
              let header = _dyld_get_image_header(index) else { continue }
        let slide = UInt(bitPattern: _dyld_get_image_vmaddr_slide(index))
        var linkedit: segment_command_64?
        var symtab: symtab_command?
        var cursor = UnsafeRawPointer(header).advanced(by: MemoryLayout<mach_header_64>.size)
        for _ in 0..<header.pointee.ncmds {
            let command = cursor.load(as: load_command.self)
            if command.cmd == UInt32(LC_SEGMENT_64) {
                let segment = cursor.load(as: segment_command_64.self)
                let segname = withUnsafeBytes(of: segment.segname) { String(decoding: $0.prefix { $0 != 0 }, as: UTF8.self) }
                if segname == SEG_LINKEDIT { linkedit = segment }
            } else if command.cmd == UInt32(LC_SYMTAB) {
                symtab = cursor.load(as: symtab_command.self)
            }
            cursor = cursor.advanced(by: Int(command.cmdsize))
        }
        guard let linkedit, let symtab else { return nil }
        let base = UInt(linkedit.vmaddr) &- UInt(linkedit.fileoff) &+ slide
        guard let strings = UnsafePointer<CChar>(bitPattern: base &+ UInt(symtab.stroff)),
              let symbols = UnsafePointer<nlist_64>(bitPattern: base &+ UInt(symtab.symoff)) else { return nil }
        for s in 0..<Int(symtab.nsyms) where strcmp(strings + Int(symbols[s].n_un.n_strx), symbol) == 0 {
            return UnsafeMutableRawPointer(bitPattern: UInt(symbols[s].n_value) &+ slide)
        }
        return nil
    }
    return nil
}

func emit(_ object: [String: Any], code: Int32 = 0) -> Never {
    if var data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]) {
        data.append(0x0A)
        FileHandle.standardOutput.write(data)
    }
    exit(code)
}

func fail(_ message: String, _ reason: String, _ extra: [String: Any] = [:]) -> Never {
    emit(["ok": false, "error": message, "reason_code": reason].merging(extra) { a, _ in a })
}

func unavailable(_ what: String) -> Never {
    emit(["ok": false, "error": "\(what) is not available on this macOS", "reason_code": "SPACES_API_UNAVAILABLE"], code: 5)
}

guard let mainConnection = exported("SLSMainConnectionID", as: MainConnectionFn.self),
      let copyDisplaySpaces = exported("SLSCopyManagedDisplaySpaces", as: CopyDisplaySpacesFn.self),
      let currentSpace = exported("SLSManagedDisplayGetCurrentSpace", as: CurrentSpaceFn.self),
      let copySpacesForWindows = exported("SLSCopySpacesForWindows", as: CopySpacesForWindowsFn.self),
      let windowIsOrderedIn = exported("SLSWindowIsOrderedIn", as: WindowIsOrderedInFn.self) else {
    unavailable("Reading desktops")
}
let connection = mainConnection()

// MARK: Reading

struct Space {
    let id: UInt64
    let kind: String
    let visible: Bool
}

struct Display {
    let id: String
    let visibleSpace: UInt64
    let spaces: [Space]
}

func readDisplays() -> [Display] {
    let raw = (copyDisplaySpaces(connection)?.takeRetainedValue() as? [[String: Any]]) ?? []
    return raw.map { display in
        let id = display["Display Identifier"] as? String ?? ""
        let visible = currentSpace(connection, id as CFString)
        let spaces = (display["Spaces"] as? [[String: Any]] ?? []).map { space -> Space in
            let sid = (space["id64"] as? NSNumber)?.uint64Value ?? 0
            // type 0 = an ordinary desktop, 4 = a full-screen app.
            let type = (space["type"] as? NSNumber)?.intValue ?? -1
            return Space(id: sid, kind: type == 0 ? "desktop" : (type == 4 ? "fullscreen" : "other"), visible: sid == visible)
        }
        return Display(id: id, visibleSpace: visible, spaces: spaces)
    }
}

func spacesFor(_ window: UInt32) -> [UInt64] {
    // 0x7 = every space kind (current, other, user and full-screen).
    let list = copySpacesForWindows(connection, 0x7, [NSNumber(value: window)] as CFArray)?.takeRetainedValue() as? [NSNumber]
    return (list ?? []).map { $0.uint64Value }
}

/// Name and kind of a display, keyed by the UUID string SkyLight uses ("Main" when all
/// displays share one set of desktops).
func screenInfo() -> [String: [String: Any]] {
    var out: [String: [String: Any]] = [:]
    for screen in NSScreen.screens {
        guard let number = screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber else { continue }
        let displayID = CGDirectDisplayID(number.uint32Value)
        let info: [String: Any] = ["name": screen.localizedName, "built_in": CGDisplayIsBuiltin(displayID) != 0,
                                   "main": CGDisplayIsMain(displayID) != 0]
        if let uuid = CGDisplayCreateUUIDFromDisplayID(displayID)?.takeRetainedValue() {
            out[CFUUIDCreateString(nil, uuid) as String] = info
        }
        if CGDisplayIsMain(displayID) != 0 { out["Main"] = info }
    }
    return out
}

struct WindowInfo {
    let id: UInt32
    let pid: Int32
    let app: String
    let title: String
    let spaces: [UInt64]
}

/// Whether the window is shown on its desktop. Closed-but-kept, minimized and hidden windows
/// still report a desktop, and off-screen desktops make kCGWindowIsOnscreen false for all.
func orderedIn(_ window: UInt32) -> Bool {
    var value: UInt8 = 0
    return windowIsOrderedIn(connection, window, &value) == 0 && value != 0
}

/// Shown app windows (layer 0, not tiny, not see-through) with the desktops they are on.
func appWindows() -> [WindowInfo] {
    let rows = CGWindowListCopyWindowInfo([.optionAll, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? []
    var out: [WindowInfo] = []
    for row in rows {
        guard (row[kCGWindowLayer as String] as? Int) == 0,
              let id = (row[kCGWindowNumber as String] as? NSNumber)?.uint32Value,
              let pid = (row[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value,
              ((row[kCGWindowAlpha as String] as? NSNumber)?.doubleValue ?? 1) > 0,
              let bounds = row[kCGWindowBounds as String] as? [String: Any],
              ((bounds["Width"] as? NSNumber)?.doubleValue ?? 0) >= 50,
              ((bounds["Height"] as? NSNumber)?.doubleValue ?? 0) >= 50,
              orderedIn(id) else { continue }
        let spaces = spacesFor(id)
        if spaces.isEmpty { continue }
        out.append(WindowInfo(id: id, pid: pid, app: row[kCGWindowOwnerName as String] as? String ?? "",
                              title: row[kCGWindowName as String] as? String ?? "", spaces: spaces))
    }
    return out
}

/// Displays with their desktops and what is open on each. A window on several desktops
/// (an app assigned to all desktops) is listed once per display and does not make a
/// desktop busy, so it cannot hide that a desktop is otherwise free.
func describe() -> [[String: Any]] {
    let screens = screenInfo()
    let windows = appWindows()
    var number = 0
    return readDisplays().map { display in
        let spaces = display.spaces.enumerated().map { offset, space -> [String: Any] in
            if space.kind == "desktop" { number += 1 }
            let own = windows.filter { $0.spaces == [space.id] }
            var item: [String: Any] = [
                "id": space.id, "index": offset + 1, "kind": space.kind, "visible": space.visible,
                "empty": own.isEmpty, "apps": Array(Set(own.map { $0.app })).sorted(),
                "windows": own.map { ["window_id": $0.id, "app": $0.app, "pid": $0.pid, "title": $0.title] },
            ]
            // Mission Control's own label, the one the person sees ("Desktop 3").
            if space.kind == "desktop" { item["label"] = "Desktop \(number)" }
            return item
        }
        let ids = Set(display.spaces.map { $0.id })
        let shared = windows.filter { $0.spaces.count > 1 && !ids.isDisjoint(with: $0.spaces) }
        var out: [String: Any] = [
            "display": display.id, "visible_space": display.visibleSpace, "spaces": spaces,
            "all_desktop_windows": shared.map { ["window_id": $0.id, "app": $0.app, "title": $0.title] },
        ]
        out.merge(screens[display.id] ?? [:]) { a, _ in a }
        return out
    }
}

// MARK: Moving

/// Start moving windows to a desktop; the returned operation must stay alive, with the run
/// loop running, until they arrive.
func requestMove(_ ids: [UInt32], to target: UInt64) -> AnyObject {
    guard let performPointer = localSymbol(
            image: skyLightPath,
            symbol: "__ZL54SLSPerformAsynchronousBridgedWindowManagementOperationP47SLSAsynchronousBridgedWindowManagementOperation"),
          let operationClass = NSClassFromString("SLSBridgedMoveWindowsToManagedSpaceOperation") as? NSObject.Type else {
        unavailable("Moving windows between desktops")
    }
    let initSelector = NSSelectorFromString("initWithWindows:spaceID:")
    guard let allocated = operationClass.perform(NSSelectorFromString("alloc"))?.takeUnretainedValue(),
          let method = class_getMethodImplementation(operationClass, initSelector),
          let operation = unsafeBitCast(method, to: InitMoveFn.self)(allocated, initSelector, ids.map { NSNumber(value: $0) } as NSArray, target) else {
        unavailable("Moving windows between desktops")
    }
    _ = unsafeBitCast(performPointer, to: PerformOperationFn.self)(operation)
    return operation
}

func move(_ ids: [UInt32], to target: UInt64) -> Never {
    desktop(target)
    let operation = requestMove(ids, to: target)
    // The operation is asynchronous: it needs this run loop, and the operation object,
    // alive until it finishes.
    let deadline = Date().addingTimeInterval(3)
    var placed: [UInt32: [UInt64]] = [:]
    withExtendedLifetime(operation) {
        repeat {
            RunLoop.current.run(until: Date().addingTimeInterval(0.05))
            placed = Dictionary(uniqueKeysWithValues: ids.map { ($0, spacesFor($0)) })
        } while Date() < deadline && placed.values.contains { $0 != [target] }
    }
    let moved = placed.values.allSatisfy { $0 == [target] }
    let result: [String: Any] = ["ok": moved, "space_id": target, "windows": ids.map { ["window_id": $0, "spaces": placed[$0] ?? []] }]
    if moved { emit(result) }
    fail("macOS did not move every window", "SPACE_MOVE_NOT_APPLIED", result)
}

// MARK: Opening on a desktop

/// Every process running this executable, read from the kernel so a launch shows up at
/// once (NSWorkspace learns of it only later, on the run loop).
func pids(running executable: String) -> Set<Int32> {
    let capacity = Int(proc_listallpids(nil, 0)) + 64
    var list = [Int32](repeating: 0, count: capacity)
    let count = Int(proc_listallpids(&list, Int32(capacity * MemoryLayout<Int32>.size)))
    var path = [CChar](repeating: 0, count: 4 * Int(MAXPATHLEN))
    var out = Set<Int32>()
    for pid in list.prefix(max(0, count)) where pid > 0 {
        if proc_pidpath(pid, &path, UInt32(path.count)) > 0, String(cString: path) == executable { out.insert(pid) }
    }
    return out
}

/// Open an app, or files with an app, so its new windows appear straight on `target`
/// without showing on any desktop the person can see and without taking focus.
///
/// macOS places a new window on the desktop its process is assigned to, else on the
/// desktop in front. Assigning the app's process to `target` for the moment its window
/// is made, then lifting the assignment, puts the window there directly: moving it after
/// it opened would show it on the person's desktop first, even for a hidden app. A
/// launching app is assigned as soon as its process exists, before it draws a window.
func desktop(_ target: UInt64) {
    guard let space = readDisplays().flatMap({ $0.spaces }).first(where: { $0.id == target }) else {
        fail("no desktop has id \(target)", "SPACE_NOT_FOUND")
    }
    guard space.kind == "desktop" else { fail("space \(target) is a full-screen app, not a desktop", "SPACE_NOT_A_DESKTOP") }
}

func assignFunction() -> ProcessAssignToSpaceFn {
    guard let assign = exported("SLSProcessAssignToSpace", as: ProcessAssignToSpaceFn.self) else {
        unavailable("Opening apps on a desktop")
    }
    return assign
}

/// An app by bundle id, name or .app path, with its resolved executable path.
func resolveApp(_ argument: String) -> (url: URL, bundle: Bundle, executable: String) {
    let workspace = NSWorkspace.shared
    let appURL: URL? = argument.hasPrefix("/") ? URL(fileURLWithPath: argument)
        : workspace.urlForApplication(withBundleIdentifier: argument)
            ?? workspace.fullPath(forApplication: argument).map { URL(fileURLWithPath: $0) }
    guard let appURL, let bundle = Bundle(url: appURL), let executable = bundle.executableURL?.resolvingSymlinksInPath().path else {
        fail("no app named \(argument)", "APP_NOT_FOUND")
    }
    return (appURL, bundle, executable)
}

/// The app in front now. NSWorkspace learns of activations on the run loop, which a
/// helper otherwise never runs, so a read without it can report the app from launch time.
func frontmost() -> NSRunningApplication? {
    RunLoop.current.run(until: Date().addingTimeInterval(0.05))
    return NSWorkspace.shared.frontmostApplication
}

/// The app's windows shown on other desktops. Assigning a process to a desktop moves every
/// window it has there (the Dock's "Assign To" does the same), so an app with any of these
/// must not be assigned: its new window is caught and moved instead.
func shownElsewhere(_ executable: String, target: UInt64) -> [WindowInfo] {
    let running = pids(running: executable)
    return appWindows().filter { running.contains($0.pid) && $0.spaces != [target] }
}

/// Desktop & Dock › Mission Control › "When switching to an application, switch to a Space
/// with open windows for the application" (on unless the person turned it off). While on,
/// a new window of the app in front takes the display to wherever that window is.
func switchesToAppDesktop() -> Bool {
    CFPreferencesAppSynchronize("com.apple.dock" as CFString)
    return (CFPreferencesCopyAppValue("workspaces-auto-swoosh" as CFString, "com.apple.dock" as CFString) as? Bool) ?? true
}

/// Refuse to catch a window of the app the person is using while that setting is on.
func refuseIfFrontAndSwitching(_ executable: String, _ appArgument: String) {
    guard switchesToAppDesktop(), let front = frontmost(), pids(running: executable).contains(front.processIdentifier) else { return }
    fail("\(appArgument) is the app in front, and with \"When switching to an application, switch to a Space with open "
         + "windows for the application\" on, its new window would take the display to the agent's desktop. Retry when "
         + "another app is in front, or use Google Chrome.", "APP_IN_FRONT_WOULD_SWITCH_DESKTOP",
         ["switches_to_app_desktop": true])
}

/// Normal-level windows of these processes by id, shown or not yet. No size filter: an app
/// may create a window small and size it ~60 ms later, by when it can already be on screen.
func windowIDs(of owners: Set<Int32>) -> Set<UInt32> {
    let rows = CGWindowListCopyWindowInfo([.optionAll, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? []
    var out = Set<UInt32>()
    for row in rows {
        guard let pid = (row[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value, owners.contains(pid),
              (row[kCGWindowLayer as String] as? Int) == 0,
              let id = (row[kCGWindowNumber as String] as? NSNumber)?.uint32Value else { continue }
        out.insert(id)
    }
    return out
}

func registerForAccessibility(_ owners: Set<Int32>) {
    // A window that opened straight onto another desktop has no Accessibility element until
    // a client asks for it; asking now lets mac_observe find it there later.
    for pid in owners {
        let appElement = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(appElement, 1.0)
        for name in ["AXMainWindow", "AXFocusedWindow"] {
            var value: CFTypeRef?
            AXUIElementCopyAttributeValue(appElement, name as CFString, &value)
        }
    }
}

/// Make the app open windows (`trigger`), and move each new one to `target` the moment it
/// exists: windows exist ~0.25 s before they are first drawn, and the move takes ~0.06 s,
/// so none is seen on the person's desktop. The person's own windows of the app stay put.
/// If the new window becomes the app's main window, the person's previous one gets that
/// back. Returns the moved window ids.
/// How long after the trigger the first new window was caught, and how many moves had to
/// be asked again (reported with the result).
var caughtAfterMs: Int?
var moveRetries = 0

func catchNewWindows(of executable: String, onto target: UInt64, within seconds: Double, trigger: () -> Bool) -> [UInt32] {
    let owners = pids(running: executable)
    let before = windowIDs(of: owners)
    var previousMain: AXUIElement?
    var previousMainID: UInt32 = 0
    if let pid = owners.first {
        let appElement = AXUIElementCreateApplication(pid)
        var value: CFTypeRef?
        if AXUIElementCopyAttributeValue(appElement, "AXMainWindow" as CFString, &value) == .success, let value,
           CFGetTypeID(value) == AXUIElementGetTypeID() {
            previousMain = (value as! AXUIElement)
            previousMainID = windowIDOf(previousMain!)
        }
    }
    let started = Date()
    guard trigger() else { return [] }
    var moved: [UInt32] = []
    var requestedAt: [UInt32: Date] = [:]
    var operations: [AnyObject] = []
    var lastNew = Date()
    // Listing every process takes milliseconds, enough for a window to be drawn before it
    // moves: the app's processes are re-read only every 50 ms.
    var watched = owners
    var watchedAt = Date()
    let deadline = Date().addingTimeInterval(seconds)
    while Date() < deadline {
        if Date().timeIntervalSince(watchedAt) > 0.05 { watched = pids(running: executable); watchedAt = Date() }
        let fresh = windowIDs(of: watched).subtracting(before).subtracting(moved)
        if !fresh.isEmpty {
            operations.append(requestMove(Array(fresh), to: target))
            moved.append(contentsOf: fresh)
            for id in fresh { requestedAt[id] = Date() }
            lastNew = Date()
            if caughtAfterMs == nil { caughtAfterMs = Int(lastNew.timeIntervalSince(started) * 1000) }
        }
        // A move asked for before the window was first shown can be dropped: a window that
        // turns up on any other desktop is moved again at once instead of staying there.
        for id in moved where Date().timeIntervalSince(requestedAt[id] ?? .distantPast) > 0.05 {
            let spaces = spacesFor(id)
            if !spaces.isEmpty && spaces != [target] {
                operations.append(requestMove([id], to: target))
                requestedAt[id] = Date()
                moveRetries += 1
            }
        }
        if !moved.isEmpty, let previousMain, previousMainID != 0, let pid = owners.first {
            let appElement = AXUIElementCreateApplication(pid)
            var value: CFTypeRef?
            if AXUIElementCopyAttributeValue(appElement, "AXMainWindow" as CFString, &value) == .success, let value,
               CFGetTypeID(value) == AXUIElementGetTypeID(), moved.contains(windowIDOf(value as! AXUIElement)) {
                AXUIElementSetAttributeValue(previousMain, "AXMain" as CFString, kCFBooleanTrue)
            }
        }
        // Done once every caught window has arrived and no other has appeared for a moment.
        if !moved.isEmpty, Date().timeIntervalSince(lastNew) > 0.3, moved.allSatisfy({ spacesFor($0) == [target] }) { break }
        RunLoop.current.run(until: Date().addingTimeInterval(0.001))
    }
    withExtendedLifetime(operations) {}
    return moved
}

typealias GetWindowFn = @convention(c) (AXUIElement, UnsafeMutablePointer<UInt32>) -> AXError
let getWindowFn = exported("_AXUIElementGetWindow", as: GetWindowFn.self, from: appServices)
func windowIDOf(_ element: AXUIElement) -> UInt32 {
    var id: UInt32 = 0
    guard let getWindowFn, getWindowFn(element, &id) == .success else { return 0 }
    return id
}

func appleScriptString(_ text: String) -> String {
    "\"" + text.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"") + "\""
}

/// Open by catching the new window, for an app that already shows windows elsewhere.
func openByCatching(on target: UInt64, appArgument: String, appURL: URL, bundle: Bundle, executable: String, items: [URL]) -> Never {
    refuseIfFrontAndSwitching(executable, appArgument)
    let frontBefore = frontmost()?.processIdentifier
    let trigger: () -> Bool
    if bundle.bundleIdentifier == "com.apple.Safari" {
        // Safari opens an address as a tab in its front window; only its scripting makes a window.
        guard items.count <= 1 else { fail("Safari opens one address per window", "INVALID_ARGUMENT") }
        let properties = items.first.map { " with properties {URL:\(appleScriptString($0.absoluteString))}" } ?? ""
        trigger = {
            let script = Process()
            script.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
            script.arguments = ["-e", "tell application \"Safari\" to make new document\(properties)"]
            script.standardOutput = FileHandle.nullDevice
            script.standardError = FileHandle.nullDevice
            return (try? script.run()) != nil
        }
    } else {
        trigger = {
            let configuration = NSWorkspace.OpenConfiguration()
            configuration.activates = false
            configuration.addsToRecentItems = false
            if items.isEmpty {
                NSWorkspace.shared.openApplication(at: appURL, configuration: configuration)
            } else {
                NSWorkspace.shared.open(items, withApplicationAt: appURL, configuration: configuration)
            }
            return true
        }
    }
    let moved = catchNewWindows(of: executable, onto: target, within: 8, trigger: trigger)
    finishOpen(target: target, appArgument: appArgument, bundle: bundle, executable: executable, moved: moved,
               frontBefore: frontBefore, method: "caught_and_moved")
}

func finishOpen(target: UInt64, appArgument: String, bundle: Bundle, executable: String, moved: [UInt32],
                frontBefore: Int32?, method: String) -> Never {
    let owners = pids(running: executable)
    registerForAccessibility(owners)
    let rows = appWindows().filter { moved.contains($0.id) }
    let result: [String: Any] = [
        "space_id": target, "app": bundle.bundleIdentifier ?? appArgument, "method": method,
        "caught_after_ms": caughtAfterMs ?? NSNull(), "move_retries": moveRetries,
        "windows": rows.map { ["window_id": $0.id, "app": $0.app, "title": $0.title, "spaces": $0.spaces] as [String: Any] },
        "focus_changed": frontBefore != frontmost()?.processIdentifier,
    ]
    guard !moved.isEmpty else { fail("no new window appeared", "NO_NEW_WINDOW", result) }
    if moved.allSatisfy({ spacesFor($0) == [target] }) { emit(result.merging(["ok": true]) { a, _ in a }) }
    fail("a new window opened outside the desktop", "SPACE_OPEN_NOT_APPLIED", result)
}

/// For a caller that makes the app open a window by other means (Chrome's own extension
/// API): print {"ready": true} once watching, then catch and move the new window.
func catchCommand(on target: UInt64, appArgument: String, seconds: Double) -> Never {
    desktop(target)
    let (_, bundle, executable) = resolveApp(appArgument)
    refuseIfFrontAndSwitching(executable, appArgument)
    let frontBefore = frontmost()?.processIdentifier
    let moved = catchNewWindows(of: executable, onto: target, within: seconds) {
        FileHandle.standardOutput.write(Data("{\"ready\":true}\n".utf8))
        return true
    }
    finishOpen(target: target, appArgument: appArgument, bundle: bundle, executable: executable, moved: moved,
               frontBefore: frontBefore, method: "caught_and_moved")
}

func open(on target: UInt64, app appArgument: String, files: [String]) -> Never {
    desktop(target)
    let assign = assignFunction()
    let workspace = NSWorkspace.shared
    let (appURL, bundle, executable) = resolveApp(appArgument)
    // Web addresses open as addresses; anything else is a file path.
    let fileURLs = files.map { $0.contains("://") ? (URL(string: $0) ?? URL(fileURLWithPath: $0)) : URL(fileURLWithPath: $0) }
    if !shownElsewhere(executable, target: target).isEmpty {
        openByCatching(on: target, appArgument: appArgument, appURL: appURL, bundle: bundle, executable: executable, items: fileURLs)
    }
    let frontBefore = frontmost()?.processIdentifier
    let before = Set(appWindows().map { $0.id })
    var assigned = Set<Int32>()
    defer { for pid in assigned { _ = assign(connection, pid, 0) } }
    // Re-assigned on every pass: a just-launched process has no WindowServer connection
    // yet, and an assignment made before it connects is accepted and then lost.
    func assignAll() {
        for pid in pids(running: executable) where assign(connection, pid, target) == 0 { assigned.insert(pid) }
    }
    assignAll()

    let configuration = NSWorkspace.OpenConfiguration()
    configuration.activates = false
    configuration.addsToRecentItems = false
    var launchError: Error?
    let done = DispatchSemaphore(value: 0)
    let completion: (NSRunningApplication?, Error?) -> Void = { _, error in launchError = error; done.signal() }
    if fileURLs.isEmpty {
        workspace.openApplication(at: appURL, configuration: configuration, completionHandler: completion)
    } else {
        workspace.open(fileURLs, withApplicationAt: appURL, configuration: configuration, completionHandler: completion)
    }
    // Catch a launching process within a millisecond or two, well before its first window.
    let deadline = Date().addingTimeInterval(10)
    var opened: [WindowInfo] = []
    var launched = false
    while Date() < deadline {
        assignAll()
        if !launched, done.wait(timeout: .now()) == .success { launched = true }
        if launched {
            if let launchError { fail("macOS could not open it: \(launchError.localizedDescription)", "OPEN_FAILED") }
            opened = appWindows().filter { !before.contains($0.id) && assigned.contains($0.pid) }
            // Give a just-opened window a moment to settle before lifting the assignment.
            if !opened.isEmpty { usleep(150_000); opened = appWindows().filter { !before.contains($0.id) && assigned.contains($0.pid) }; break }
        }
        usleep(1_000)
    }
    for pid in assigned { _ = assign(connection, pid, 0) }
    registerForAccessibility(Set(opened.map { $0.pid }))
    assigned.removeAll()
    let frontAfter = frontmost()?.processIdentifier
    let windows = opened.map { ["window_id": $0.id, "app": $0.app, "title": $0.title, "spaces": $0.spaces] as [String: Any] }
    let result: [String: Any] = [
        "space_id": target, "app": bundle.bundleIdentifier ?? appArgument, "windows": windows,
        "focus_changed": frontBefore != frontAfter, "method": "assigned",
    ]
    guard !opened.isEmpty else { fail("no new window appeared", "NO_NEW_WINDOW", result) }
    let placed = opened.allSatisfy { $0.spaces == [target] }
    if placed { emit(result.merging(["ok": true]) { a, _ in a }) }
    fail("a new window opened outside the desktop", "SPACE_OPEN_NOT_APPLIED", result)
}

/// Assign every running process of an app to a desktop (SPACE_ID) or lift the assignment
/// (0), for a caller that makes the app open a window by other means, such as Chrome's own
/// extension API. The assignment outlives this helper, so the caller must always lift it.
func assignApp(_ appArgument: String, to target: UInt64) -> Never {
    let executable = resolveApp(appArgument).executable
    if target != 0 {
        desktop(target)
        let elsewhere = shownElsewhere(executable, target: target)
        guard elsewhere.isEmpty else {
            fail("\(appArgument) has windows on other desktops; assigning it to desktop \(target) would move them there too",
                 "APP_HAS_WINDOWS_ON_OTHER_DESKTOPS",
                 ["windows": elsewhere.map { ["window_id": $0.id, "app": $0.app, "title": $0.title, "spaces": $0.spaces] }])
        }
    }
    let assign = assignFunction()
    let found = pids(running: executable)
    let failed = found.filter { assign(connection, $0, target) != 0 }
    emit(["ok": !found.isEmpty && failed.isEmpty, "space_id": target, "pids": found.sorted(), "failed": failed.sorted()]
        .merging(found.isEmpty ? ["error": "\(appArgument) is not running", "reason_code": "APP_NOT_RUNNING"] : [:]) { a, _ in a })
}

// MARK: Mission Control

func attribute(_ element: AXUIElement, _ name: String) -> AnyObject? {
    var value: CFTypeRef?
    AXUIElementCopyAttributeValue(element, name as CFString, &value)
    return value
}

func children(_ element: AXUIElement) -> [AXUIElement] { (attribute(element, "AXChildren") as? [AXUIElement]) ?? [] }
func identifier(_ element: AXUIElement) -> String { attribute(element, "AXIdentifier") as? String ?? "" }

/// Mission Control's per-display groups ("mc.display"), in the same order as SkyLight's displays.
func missionControlDisplays(_ dock: AXUIElement) -> [AXUIElement]? {
    guard let mc = children(dock).first(where: { identifier($0) == "mc" }) else { return nil }
    let displays = children(mc).filter { identifier($0) == "mc.display" }
    return displays.isEmpty ? nil : displays
}

func spacesBar(_ display: AXUIElement) -> (list: [AXUIElement], add: AXUIElement?)? {
    guard let bar = children(display).first(where: { identifier($0) == "mc.spaces" }) else { return nil }
    let list = children(bar).first(where: { identifier($0) == "mc.spaces.list" }).map(children) ?? []
    return (list, children(bar).first(where: { identifier($0) == "mc.spaces.add" }))
}

/// Open Mission Control, run `body` on its displays as soon as every desktop bar is filled
/// in (`spacesPerDisplay`, in SkyLight's display order), retrying while Mission Control is
/// still settling, and close it right away: it is on screen only while the press happens.
/// What Mission Control showed: per display, each desktop button's title and actions, and
/// whether it offered "add desktop". Returned with a refusal so a failure explains itself.
func describeMissionControl(_ displays: [AXUIElement]) -> [[String: Any]] {
    displays.map { display in
        guard let bar = spacesBar(display) else { return ["spaces_bar": false] }
        let buttons = bar.list.map { button -> [String: Any] in
            var names: CFArray?
            AXUIElementCopyActionNames(button, &names)
            return ["title": attribute(button, "AXTitle") as? String ?? "", "actions": (names as? [String]) ?? []]
        }
        return ["desktops": buttons, "add_button": bar.add != nil]
    }
}

func withMissionControl(_ spacesPerDisplay: [Int], _ body: ([AXUIElement]) -> Bool) -> (ran: Bool, onScreenMs: Int, seen: [[String: Any]]) {
    guard AXIsProcessTrusted() else { fail("Accessibility permission is not granted", "ACCESSIBILITY_REQUIRED") }
    guard let send = exported("CoreDockSendNotification", as: CoreDockSendFn.self, from: appServices),
          let dockApp = NSRunningApplication.runningApplications(withBundleIdentifier: "com.apple.dock").first else {
        unavailable("Opening Mission Control")
    }
    let dock = AXUIElementCreateApplication(dockApp.processIdentifier)
    AXUIElementSetMessagingTimeout(dock, 1.0)
    if missionControlDisplays(dock) != nil { fail("Mission Control is already open", "MISSION_CONTROL_BUSY") }
    let started = Date()
    send("com.apple.expose.awake" as CFString, 0)
    var ran = false
    var seen: [[String: Any]] = []
    // Mission Control is ready within ~0.1 s; a failure keeps it on screen for at most a second.
    let deadline = started.addingTimeInterval(1)
    while Date() < deadline {
        if let displays = missionControlDisplays(dock) { seen = describeMissionControl(displays) }
        if let displays = missionControlDisplays(dock), displays.count == spacesPerDisplay.count,
           zip(displays, spacesPerDisplay).allSatisfy({ display, count in
               guard let bar = spacesBar(display) else { return false }
               return bar.add != nil && bar.list.count == count
           }),
           body(displays) {
            ran = true
            break
        }
        usleep(5_000)
    }
    // The same notification closes it; wait until it is gone so the caller reads settled desktops.
    if missionControlDisplays(dock) != nil { send("com.apple.expose.awake" as CFString, 0) }
    let closeDeadline = Date().addingTimeInterval(2)
    while Date() < closeDeadline, missionControlDisplays(dock) != nil { usleep(5_000) }
    return (ran, Int(Date().timeIntervalSince(started) * 1000), seen)
}

func create(onDisplay displayID: String) -> Never {
    let before = readDisplays()
    guard let index = before.firstIndex(where: { $0.id == displayID }) else { fail("no display has id \(displayID)", "DISPLAY_NOT_FOUND") }
    let (ran, ms, seen) = withMissionControl(before.map { $0.spaces.count }) { displays in
        guard let add = spacesBar(displays[index])?.add else { return false }
        return AXUIElementPerformAction(add, "AXPress" as CFString) == .success
    }
    guard ran else {
        fail("Mission Control did not offer to add a desktop", "SPACE_CREATE_NOT_APPLIED",
             ["mission_control_ms": ms, "mission_control": seen, "expected_desktops": before.map { $0.spaces.count }])
    }
    let deadline = Date().addingTimeInterval(2)
    var added: [Space] = []
    repeat {
        let old = Set(before[index].spaces.map { $0.id })
        added = readDisplays().first(where: { $0.id == displayID })?.spaces.filter { !old.contains($0.id) } ?? []
        if added.isEmpty { usleep(20_000) }
    } while added.isEmpty && Date() < deadline
    guard let space = added.first else { fail("macOS did not add a desktop", "SPACE_CREATE_NOT_APPLIED", ["mission_control_ms": ms]) }
    emit(["ok": true, "display": displayID, "space_id": space.id, "mission_control_ms": ms])
}

func remove(_ target: UInt64) -> Never {
    let before = readDisplays()
    guard let index = before.firstIndex(where: { $0.spaces.contains { $0.id == target } }),
          let position = before[index].spaces.firstIndex(where: { $0.id == target }) else {
        fail("no desktop has id \(target)", "SPACE_NOT_FOUND")
    }
    let space = before[index].spaces[position]
    guard space.kind == "desktop" else { fail("space \(target) is a full-screen app, not a desktop", "SPACE_NOT_A_DESKTOP") }
    // Removing the desktop the person is looking at would switch their view.
    guard !space.visible else { fail("desktop \(target) is on screen", "SPACE_VISIBLE") }
    // Its windows would land on another desktop, possibly the person's.
    let own = appWindows().filter { $0.spaces == [target] }
    guard own.isEmpty else {
        fail("desktop \(target) still has windows", "SPACE_NOT_EMPTY",
             ["windows": own.map { ["window_id": $0.id, "app": $0.app, "title": $0.title] }])
    }
    let (ran, ms, seen) = withMissionControl(before.map { $0.spaces.count }) { displays in
        guard let list = spacesBar(displays[index])?.list, position < list.count else { return false }
        let button = list[position]
        var names: CFArray?
        AXUIElementCopyActionNames(button, &names)
        guard ((names as? [String]) ?? []).contains("AXRemoveDesktop") else { return false }
        return AXUIElementPerformAction(button, "AXRemoveDesktop" as CFString) == .success
    }
    guard ran else {
        fail("Mission Control did not offer to remove that desktop", "SPACE_REMOVE_NOT_APPLIED",
             ["mission_control_ms": ms, "mission_control": seen, "expected_desktops": before.map { $0.spaces.count },
              "display_index": index, "desktop_position": position + 1])
    }
    let deadline = Date().addingTimeInterval(2)
    var gone = false
    repeat {
        gone = !readDisplays().flatMap { $0.spaces }.contains { $0.id == target }
        if !gone { usleep(20_000) }
    } while !gone && Date() < deadline
    guard gone else { fail("macOS did not remove the desktop", "SPACE_REMOVE_NOT_APPLIED", ["mission_control_ms": ms]) }
    emit(["ok": true, "space_id": target, "mission_control_ms": ms])
}

// MARK: Commands

func windowIDs(_ args: ArraySlice<String>) -> [UInt32]? {
    let ids = args.compactMap { UInt32($0) }
    return ids.isEmpty || ids.count != args.count ? nil : ids
}

let arguments = Array(CommandLine.arguments.dropFirst())
switch arguments.first {
case "spaces":
    let front = frontmost()
    let frontInfo: [String: Any] = [
        "app": front?.localizedName ?? "", "pid": Int(front?.processIdentifier ?? 0), "bundle_id": front?.bundleIdentifier ?? "",
    ]
    emit(["ok": true, "displays": describe(), "frontmost": frontInfo, "switches_to_app_desktop": switchesToAppDesktop()])
case "catch":
    guard arguments.count >= 3, let target = UInt64(arguments[1]) else {
        emit(["ok": false, "error": "usage: catch SPACE_ID APP [SECONDS]"], code: 2)
    }
    catchCommand(on: target, appArgument: arguments[2], seconds: arguments.count > 3 ? (Double(arguments[3]) ?? 8) : 8)
case "assign":
    guard arguments.count == 3, let target = UInt64(arguments[1]) else {
        emit(["ok": false, "error": "usage: assign SPACE_ID APP (SPACE_ID 0 lifts the assignment)"], code: 2)
    }
    assignApp(arguments[2], to: target)
case "window-spaces":
    guard let ids = windowIDs(arguments.dropFirst()) else { emit(["ok": false, "error": "window ids required"], code: 2) }
    emit(["ok": true, "windows": ids.map { ["window_id": $0, "spaces": spacesFor($0)] }])
case "move":
    guard arguments.count >= 3, let target = UInt64(arguments[1]), let ids = windowIDs(arguments.dropFirst(2)) else {
        emit(["ok": false, "error": "usage: move SPACE_ID WID [WID...]"], code: 2)
    }
    move(ids, to: target)
case "open":
    guard arguments.count >= 3, let target = UInt64(arguments[1]) else {
        emit(["ok": false, "error": "usage: open SPACE_ID APP [FILE_OR_URL...]"], code: 2)
    }
    open(on: target, app: arguments[2], files: Array(arguments.dropFirst(3)))
case "create":
    guard arguments.count == 2 else { emit(["ok": false, "error": "usage: create DISPLAY_ID"], code: 2) }
    create(onDisplay: arguments[1])
case "remove":
    guard arguments.count == 2, let target = UInt64(arguments[1]) else { emit(["ok": false, "error": "usage: remove SPACE_ID"], code: 2) }
    remove(target)
default:
    emit(["ok": false, "error": "usage: spaces | window-spaces WID... | move SPACE_ID WID... | open SPACE_ID APP [FILE_OR_URL...] | assign SPACE_ID APP | catch SPACE_ID APP | create DISPLAY_ID | remove SPACE_ID"], code: 2)
}
