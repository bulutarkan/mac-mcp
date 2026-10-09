// Accessibility change watcher for Mac MCP.
//
// Keeps an AXObserver per watched application and counts its notifications,
// so the server can tell whether an app's UI may have changed since a full
// observation without walking the tree again. Each event stores a value from
// one process-wide counter, so a re-registered pid never repeats an old token.
//
// Protocol: one JSON object per line on stdin, one reply per line on stdout.
//   {"op":"watch","pid":N} -> {"ok":true,"watching":true,"gen":G,"session":S}
// "watch" registers on first use (idempotent) and returns the current counter.
// "watching" is false when the app is gone or refused the core notifications.
import AppKit
import ApplicationServices
import Foundation

let session = UUID().uuidString.prefix(12).lowercased()
let maxWatched = 32
var counter: UInt64 = 0
var generations: [pid_t: UInt64] = [:]
var observers: [pid_t: AXObserver] = [:]
var lastUsed: [pid_t: UInt64] = [:]
var useClock: UInt64 = 0

// Notifications whose registration must succeed for the watch to be trusted.
let coreNotifications = [
    kAXValueChangedNotification, kAXUIElementDestroyedNotification, kAXCreatedNotification,
    kAXTitleChangedNotification,
]
let extraNotifications = [
    kAXFocusedUIElementChangedNotification, kAXFocusedWindowChangedNotification, kAXMainWindowChangedNotification,
    kAXWindowCreatedNotification, kAXWindowMovedNotification, kAXWindowResizedNotification,
    kAXWindowMiniaturizedNotification, kAXWindowDeminiaturizedNotification, kAXLayoutChangedNotification,
    kAXSelectedChildrenChangedNotification, kAXSelectedChildrenMovedNotification, kAXSelectedRowsChangedNotification,
    kAXSelectedColumnsChangedNotification, kAXSelectedCellsChangedNotification, kAXSelectedTextChangedNotification,
    kAXRowCountChangedNotification, kAXRowExpandedNotification, kAXRowCollapsedNotification,
    kAXMenuOpenedNotification, kAXMenuClosedNotification, kAXSheetCreatedNotification, kAXDrawerCreatedNotification,
    kAXElementBusyChangedNotification, kAXResizedNotification, kAXMovedNotification, kAXUnitsChangedNotification,
    kAXApplicationShownNotification, kAXApplicationHiddenNotification,
]

func bump(_ pid: pid_t) {
    counter += 1
    generations[pid] = counter
}

let callback: AXObserverCallback = { _, _, _, refcon in
    guard let refcon else { return }
    bump(pid_t(Int(bitPattern: refcon)))
}

func unwatch(_ pid: pid_t) {
    if let observer = observers.removeValue(forKey: pid) {
        CFRunLoopRemoveSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(observer), .defaultMode)
    }
    generations.removeValue(forKey: pid)
    lastUsed.removeValue(forKey: pid)
}

func alive(_ pid: pid_t) -> Bool {
    return kill(pid, 0) == 0 || errno == EPERM
}

func watch(_ pid: pid_t) -> [String: Any] {
    useClock += 1
    if observers[pid] != nil {
        if !alive(pid) {
            unwatch(pid)
            return ["ok": true, "watching": false, "reason": "process_exited"]
        }
        lastUsed[pid] = useClock
        return ["ok": true, "watching": true, "gen": generations[pid] ?? 0, "session": String(session)]
    }
    guard alive(pid) else { return ["ok": true, "watching": false, "reason": "process_exited"] }
    if observers.count >= maxWatched, let oldest = lastUsed.min(by: { $0.value < $1.value })?.key {
        unwatch(oldest)
    }
    var created: AXObserver?
    guard AXObserverCreate(pid, callback, &created) == .success, let observer = created else {
        return ["ok": true, "watching": false, "reason": "observer_create_failed"]
    }
    let app = AXUIElementCreateApplication(pid)
    AXUIElementSetMessagingTimeout(app, 1.0)
    let refcon = UnsafeMutableRawPointer(bitPattern: Int(pid))
    for name in coreNotifications {
        let status = AXObserverAddNotification(observer, app, name as CFString, refcon)
        if status != .success && status != .notificationAlreadyRegistered {
            return ["ok": true, "watching": false, "reason": "notification_refused", "status": Int(status.rawValue)]
        }
    }
    for name in extraNotifications {
        _ = AXObserverAddNotification(observer, app, name as CFString, refcon)
    }
    CFRunLoopAddSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(observer), .defaultMode)
    observers[pid] = observer
    lastUsed[pid] = useClock
    // A fresh registration starts a new generation, never an earlier one.
    bump(pid)
    return ["ok": true, "watching": true, "gen": generations[pid] ?? 0, "session": String(session)]
}

func reply(_ object: [String: Any]) {
    var data = (try? JSONSerialization.data(withJSONObject: object)) ?? Data("{\"ok\":false}".utf8)
    data.append(0x0A)
    FileHandle.standardOutput.write(data)
}

guard AXIsProcessTrusted() else {
    FileHandle.standardError.write("Accessibility permission is not granted (-25211)\n".data(using: .utf8)!)
    exit(3)
}

Thread {
    while let line = readLine(strippingNewline: true) {
        guard let data = line.data(using: .utf8),
              let request = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let op = request["op"] as? String else {
            reply(["ok": false, "error": "bad_request"])
            continue
        }
        var response: [String: Any] = ["ok": false, "error": "unknown_op"]
        DispatchQueue.main.sync {
            if op == "watch", let pid = (request["pid"] as? NSNumber)?.int32Value, pid > 0 {
                response = watch(pid)
            }
        }
        reply(response)
    }
    exit(0)
}.start()

RunLoop.main.run()
