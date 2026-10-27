// Accessibility change watcher for Mac MCP.
//
// Keeps an AXObserver per watched application and counts its notifications,
// so the server can tell whether an app's UI may have changed since a full
// observation without walking the tree again. Each event stores a value from
// one process-wide counter, so a re-registered pid never repeats an old token.
//
// Protocol: one JSON object per line on stdin, one reply per line on stdout.
//   {"op":"watch","pid":N} -> {"ok":true,"watching":true,"gen":G,"session":S}
//   {"op":"wait","pid":N,"since":G,"timeout_ms":T} -> the same reply plus "changed",
//   sent once the app reports a change after G or after T ms; without "since" it answers at once.
// "watch" registers on first use (idempotent) and returns the current counter.
// "watching" is false when the app is gone or refused the core notifications.
// A "wait" holds this helper's request loop, so the server runs a separate helper for waits.
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
// The one pending "wait" (main thread only); a notification for its pid wakes it.
var waiter: (pid: pid_t, semaphore: DispatchSemaphore)?

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
    if let pending = waiter, pending.pid == pid {
        pending.semaphore.signal()
    }
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
        let pid = (request["pid"] as? NSNumber)?.int32Value ?? 0
        let since = (request["since"] as? NSNumber)?.uint64Value
        let timeoutMs = min(max((request["timeout_ms"] as? NSNumber)?.intValue ?? 0, 0), 20_000)
        let semaphore = DispatchSemaphore(value: 0)
        var waits = false
        DispatchQueue.main.sync {
            if (op == "watch" || op == "wait") && pid > 0 {
                response = watch(pid)
                if op == "wait", let since, response["watching"] as? Bool == true,
                   (generations[pid] ?? 0) <= since, timeoutMs > 0 {
                    waiter = (pid, semaphore)
                    waits = true
                }
            }
        }
        if waits {
            _ = semaphore.wait(timeout: .now() + .milliseconds(timeoutMs))
            DispatchQueue.main.sync {
                waiter = nil
                response["gen"] = generations[pid] ?? 0
            }
        }
        if op == "wait", response["watching"] as? Bool == true {
            let gen = (response["gen"] as? UInt64) ?? 0
            response["changed"] = since.map { gen > $0 } ?? true
        }
        reply(response)
    }
    exit(0)
}.start()

RunLoop.main.run()
