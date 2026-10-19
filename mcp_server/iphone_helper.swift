// The owner's iPhone, through macOS iPhone Mirroring: see its screen, read its text, and tap, scroll and type.
//
// iPhone Mirroring shows no Accessibility tree and turns only real HID input into touches, and only while it is the
// frontmost app: events posted to its process are ignored. So input brings the mirror forward for the moment it
// takes, checks that the point is on the phone's screen, and puts the pointer and the previous app back. Captures
// come from ScreenCaptureKit and never move anything. Coordinates are phone points: the screen as one image, scaled
// to `--width` (default 390) wide, origin at the top left.
//
// Every command prints one JSON object. Used by iphone.py; compiled on first use and cached by source hash.
import AppKit
import Carbon
import CoreGraphics
import Foundation
import ImageIO
import ScreenCaptureKit
import UniformTypeIdentifiers
import Vision

let mirrorBundleID = "com.apple.ScreenContinuity"
let eventMarker: Int64 = 0x4D43_4950  // "MCIP": Mac MCP's own iPhone input
let lockPath = (NSString(string: "~/.mac-mcp/cache/iphone-input.lock").expandingTildeInPath)

func emit(_ object: [String: Any]) -> Never {
    var object = object
    if openedMirroring { object["opened_mirroring"] = true }
    let data = (try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])) ?? Data("{}".utf8)
    FileHandle.standardOutput.write(data)
    exit(0)
}

func fail(_ code: String, _ message: String, _ extra: [String: Any] = [:]) -> Never {
    var object: [String: Any] = ["ok": false, "reason_code": code, "error": message]
    for (key, value) in extra { object[key] = value }
    emit(object)
}

// MARK: Arguments

var options: [String: String] = [:]
var positional: [String] = []
do {
    var items = Array(CommandLine.arguments.dropFirst())
    while !items.isEmpty {
        let item = items.removeFirst()
        if item.hasPrefix("--") {
            let key = String(item.dropFirst(2))
            options[key] = items.isEmpty || items[0].hasPrefix("--") ? "1" : items.removeFirst()
        } else {
            positional.append(item)
        }
    }
}
let command = positional.first ?? "status"
func number(_ key: String) -> Double? { options[key].flatMap(Double.init) }
let outputWidth = max(160, min(1200, number("width") ?? 390))

// MARK: The mirror window

struct Mirror {
    let app: NSRunningApplication
    let windowID: CGWindowID
    let frame: CGRect  // desktop points, top-left origin
}

func findMirror() -> Mirror? {
    guard let app = NSRunningApplication.runningApplications(withBundleIdentifier: mirrorBundleID).first else { return nil }
    let rows = CGWindowListCopyWindowInfo([.excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? []
    var best: Mirror?
    for row in rows {
        // On screen only: the app also keeps a hidden, larger "Welcome to iPhone Mirroring" window.
        guard (row[kCGWindowOwnerPID as String] as? pid_t) == app.processIdentifier,
              (row[kCGWindowLayer as String] as? Int) == 0,
              (row[kCGWindowIsOnscreen as String] as? Bool) == true,
              let bounds = row[kCGWindowBounds as String] as? [String: Any],
              let frame = CGRect(dictionaryRepresentation: bounds as CFDictionary),
              frame.width > 150, frame.height > 300,
              let number = row[kCGWindowNumber as String] as? CGWindowID else { continue }
        if best == nil || frame.width * frame.height > best!.frame.width * best!.frame.height {
            best = Mirror(app: app, windowID: number, frame: frame)
        }
    }
    return best
}

/// Set when this command had to open iPhone Mirroring first; reported so the agent knows why it took longer.
var openedMirroring = false

/// The mirror, opening iPhone Mirroring first when it is closed and waiting (up to 20 s) until it shows the iPhone
/// or a message such as "iPhone in Use" that only the owner can resolve.
func requireMirror() -> Mirror {
    if let mirror = findMirror() { return mirror }
    guard let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: mirrorBundleID) else {
        fail("IPHONE_MIRRORING_UNAVAILABLE", "iPhone Mirroring is not on this Mac (it needs macOS 15 or later).")
    }
    let configuration = NSWorkspace.OpenConfiguration()
    configuration.activates = false
    NSWorkspace.shared.openApplication(at: url, configuration: configuration) { _, _ in }
    openedMirroring = true
    let deadline = Date().addingTimeInterval(20)
    while Date() < deadline {
        spin(0.25)
        guard let mirror = findMirror() else { continue }
        guard AXIsProcessTrusted() else { return mirror }
        let message = mirrorMessage(mirror)
        if message.isEmpty || !message.joined(separator: " ").lowercased().contains("connecting") {
            usleep(300_000)  // the first frame of the iPhone's screen
            return mirror
        }
    }
    fail("IPHONE_MIRRORING_NOT_OPEN", "iPhone Mirroring did not connect to the iPhone (it must be locked, nearby and on the same Apple Account).")
}

// MARK: Connection

/// While the iPhone is mirrored, the window shows only its screen, which has no Accessibility children; otherwise it
/// shows a message ("Connecting to iPhone 17", "iPhone in Use … Connect") as real text. Read without capturing.
func mirrorMessage(_ mirror: Mirror) -> [String] {
    let app = AXUIElementCreateApplication(mirror.app.processIdentifier)
    var windows: CFTypeRef?
    guard AXUIElementCopyAttributeValue(app, kAXWindowsAttribute as CFString, &windows) == .success,
          let list = windows as? [AXUIElement] else { return [] }
    var texts: [String] = []
    func walk(_ element: AXUIElement, depth: Int) {
        guard depth < 5, texts.count < 12 else { return }
        var role: CFTypeRef?, value: CFTypeRef?, title: CFTypeRef?
        AXUIElementCopyAttributeValue(element, kAXRoleAttribute as CFString, &role)
        AXUIElementCopyAttributeValue(element, kAXValueAttribute as CFString, &value)
        AXUIElementCopyAttributeValue(element, kAXTitleAttribute as CFString, &title)
        if let role = role as? String, role == kAXStaticTextRole as String || role == kAXButtonRole as String {
            for text in [value as? String, title as? String].compactMap({ $0 }) where !text.isEmpty && !texts.contains(text) {
                texts.append(text)
            }
        }
        var children: CFTypeRef?
        if AXUIElementCopyAttributeValue(element, kAXChildrenAttribute as CFString, &children) == .success {
            for child in (children as? [AXUIElement]) ?? [] { walk(child, depth: depth + 1) }
        }
    }
    for window in list {
        var title: CFTypeRef?
        AXUIElementCopyAttributeValue(window, kAXTitleAttribute as CFString, &title)
        if (title as? String)?.hasPrefix("Welcome") == true { continue }
        walk(window, depth: 0)
    }
    return texts
}

/// "Connecting to iPhone 17" names the phone; remembered so the owner sees it once it is connected.
let namePath = (NSString(string: "~/.mac-mcp/cache/iphone-name").expandingTildeInPath)
func rememberName(from texts: [String]) -> String? {
    if let index = texts.firstIndex(where: { $0.lowercased().hasPrefix("connecting to") }) {
        let rest = texts[index].dropFirst("connecting to".count).trimmingCharacters(in: .whitespaces)
        let name = rest.isEmpty && index + 1 < texts.count ? texts[index + 1] : rest
        if !name.isEmpty { try? name.write(toFile: namePath, atomically: true, encoding: .utf8); return name }
    }
    return try? String(contentsOfFile: namePath, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)
}

// MARK: Capture

struct Shot {
    let image: CGImage      // the phone's screen only, at capture resolution
    let screen: CGRect      // where that screen is inside the window, in window points
    let scale: CGFloat      // capture pixels per window point
    let connected: Bool     // false while the window shows a message instead ("iPhone in Use", connecting)
}

func capture(_ mirror: Mirror) -> Shot {
    let semaphore = DispatchSemaphore(value: 0)
    var result: Result<CGImage, Error>?
    Task {
        do {
            let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
            guard let window = content.windows.first(where: { $0.windowID == mirror.windowID }) else {
                throw NSError(domain: "iphone", code: 1, userInfo: [NSLocalizedDescriptionKey: "the mirror window is not shareable"])
            }
            let filter = SCContentFilter(desktopIndependentWindow: window)
            let config = SCStreamConfiguration()
            let scale = CGFloat(filter.pointPixelScale)
            config.width = Int(mirror.frame.width * scale)
            config.height = Int(mirror.frame.height * scale)
            config.showsCursor = false
            config.ignoreShadowsSingleWindow = true
            result = .success(try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: config))
        } catch {
            result = .failure(error)
        }
        semaphore.signal()
    }
    if semaphore.wait(timeout: .now() + 8) == .timedOut {
        fail("IPHONE_CAPTURE_TIMEOUT", "Capturing the iPhone Mirroring window timed out.")
    }
    let full: CGImage
    switch result {
    case .success(let image)?: full = image
    case .failure(let error)?:
        fail("IPHONE_CAPTURE_FAILED", "Could not capture iPhone Mirroring (\(error.localizedDescription)). Mac MCP needs Screen Recording permission.")
    case nil: fail("IPHONE_CAPTURE_FAILED", "Could not capture iPhone Mirroring.")
    }
    let scale = CGFloat(full.width) / mirror.frame.width
    let bounds = screenGeometry(screenBounds(full), image: full, window: mirror.frame.size, scale: scale)
    guard bounds.width > 100, bounds.height > 200, let cropped = full.cropping(to: bounds) else {
        fail("IPHONE_SCREEN_NOT_FOUND", "The iPhone screen is not showing in iPhone Mirroring.")
    }
    // A message ("Connecting to…", "iPhone in Use") is real Accessibility text; the mirrored screen has none.
    let connected = AXIsProcessTrusted() ? mirrorMessage(mirror).isEmpty : bounds.minY / scale >= 24
    return Shot(image: cropped, screen: CGRect(x: bounds.minX / scale, y: bounds.minY / scale,
                                               width: bounds.width / scale, height: bounds.height / scale),
                scale: scale, connected: connected)
}

/// Where the phone's screen sits in the window depends only on the window's size, so it is measured once per size and
/// remembered. A measurement counts only when the frame looks like one (left and right alike, the bottom border like
/// the sides, a title band of a usual height above): a dark page whose edges look like the frame never moves it.
let geometryPath = (NSString(string: "~/.mac-mcp/cache/iphone-geometry.json").expandingTildeInPath)

func screenGeometry(_ measured: CGRect, image: CGImage, window: CGSize, scale: CGFloat) -> CGRect {
    let left = measured.minX, top = measured.minY
    let right = CGFloat(image.width) - measured.maxX, bottom = CGFloat(image.height) - measured.maxY
    let key = "\(Int(window.width))x\(Int(window.height))"
    var saved = (try? JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: geometryPath)))) as? [String: [Double]] ?? [:]
    if let points = saved[key], points.count == 4 {
        let l = CGFloat(points[0]) * scale, t = CGFloat(points[1]) * scale
        let r = CGFloat(points[2]) * scale, b = CGFloat(points[3]) * scale
        return CGRect(x: l, y: t, width: CGFloat(image.width) - l - r, height: CGFloat(image.height) - t - b)
    }
    let band = (top - left) / scale
    let plausible = abs(left - right) <= 2 * scale && abs(bottom - left) <= 3 * scale && band >= 20 && band <= 44
    if plausible {
        saved[key] = [left, top, right, bottom].map { Double($0 / scale) }
        if let data = try? JSONSerialization.data(withJSONObject: saved) { try? data.write(to: URL(fileURLWithPath: geometryPath)) }
        return measured
    }
    // Nothing remembered yet: the frame is the same width on the sides and bottom.
    let side = min(left, right)
    return CGRect(x: side, y: top, width: CGFloat(image.width) - 2 * side, height: CGFloat(image.height) - top - side)
}

/// The window draws the phone's screen inside a dark frame of one color (with the title bar on top). Probing inward
/// from each edge at a few places away from the rounded corners finds where the screen starts; the smallest inset
/// wins, so a dark app touching one probe cannot shrink the screen.
func screenBounds(_ image: CGImage) -> CGRect {
    let width = image.width, height = image.height
    guard let context = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: width * 4,
                                  space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue),
          let data = context.data else { return CGRect(x: 0, y: 0, width: width, height: height) }
    context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
    let pixels = data.bindMemory(to: UInt8.self, capacity: width * height * 4)
    // A bitmap context keeps the image's top row first in memory.
    func pixel(_ x: Int, _ y: Int) -> (Int, Int, Int) {
        let offset = (y * width + x) * 4
        return (Int(pixels[offset]), Int(pixels[offset + 1]), Int(pixels[offset + 2]))
    }
    func alpha(_ x: Int, _ y: Int) -> Int { Int(pixels[(y * width + x) * 4 + 3]) }
    let frame = pixel(1, height / 2)
    // The frame, or the see-through band where the title bar shows on hover.
    func isFrame(_ x: Int, _ y: Int) -> Bool {
        if alpha(x, y) < 250 { return true }
        let p = pixel(x, y)
        return abs(p.0 - frame.0) <= 6 && abs(p.1 - frame.1) <= 6 && abs(p.2 - frame.2) <= 6
    }
    func inset(_ probes: [Int], limit: Int, _ at: (Int, Int) -> (Int, Int)) -> Int {
        probes.map { probe in
            var step = 0
            while step < limit { let (x, y) = at(probe, step); if !isFrame(x, y) { break }; step += 1 }
            return step
        }.min() ?? 0
    }
    let columns = [width / 4, width / 2, width * 3 / 4], rows = [height / 3, height / 2, height * 2 / 3]
    let top = inset(columns, limit: height / 6) { ($0, $1 == 0 ? 1 : $1) }
    let bottom = inset(columns, limit: height / 10) { ($0, height - 1 - $1) }
    let left = inset(rows, limit: width / 8) { ($1, $0) }
    let right = inset(rows, limit: width / 8) { (width - 1 - $1, $0) }
    return CGRect(x: left, y: top, width: width - left - right, height: height - top - bottom)
}

func scaled(_ image: CGImage, width: Double) -> CGImage {
    let height = Int((Double(image.height) * width / Double(image.width)).rounded())
    guard let context = CGContext(data: nil, width: Int(width), height: height, bitsPerComponent: 8, bytesPerRow: 0,
                                  space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) else { return image }
    context.interpolationQuality = .high
    context.draw(image, in: CGRect(x: 0, y: 0, width: Int(width), height: height))
    return context.makeImage() ?? image
}

func jpeg(_ image: CGImage, quality: Double) -> Data {
    let data = NSMutableData()
    guard let destination = CGImageDestinationCreateWithData(data, UTType.jpeg.identifier as CFString, 1, nil) else { return Data() }
    CGImageDestinationAddImage(destination, image, [kCGImageDestinationLossyCompressionQuality: quality] as CFDictionary)
    CGImageDestinationFinalize(destination)
    return data as Data
}

/// A 64-bit difference hash, so "nothing changed" costs no image.
func differenceHash(_ image: CGImage) -> UInt64 {
    let width = 9, height = 8
    var gray = [UInt8](repeating: 0, count: width * height)
    guard let context = CGContext(data: &gray, width: width, height: height, bitsPerComponent: 8, bytesPerRow: width,
                                  space: CGColorSpaceCreateDeviceGray(), bitmapInfo: CGImageAlphaInfo.none.rawValue) else { return 0 }
    context.interpolationQuality = .medium
    // Leave out the status bar: its clock changes every minute.
    let statusBar = Int(Double(image.height) * 0.06)
    if let body = image.cropping(to: CGRect(x: 0, y: statusBar, width: image.width, height: image.height - statusBar)) {
        context.draw(body, in: CGRect(x: 0, y: 0, width: width, height: height))
    }
    var hash: UInt64 = 0
    for y in 0..<height { for x in 0..<(width - 1) { hash = (hash << 1) | (gray[y * width + x] > gray[y * width + x + 1] ? 1 : 0) } }
    return hash
}

func hex(_ value: UInt64) -> String { String(format: "%016llx", value) }

// MARK: Reading text

func recognize(_ image: CGImage, phoneWidth: Double, fast: Bool = false) -> [[String: Any]] {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = fast ? .fast : .accurate
    request.usesLanguageCorrection = !fast
    request.recognitionLanguages = ["tr-TR", "en-US"]
    try? VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
    let phoneHeight = phoneWidth * Double(image.height) / Double(image.width)
    var rows: [[String: Any]] = []
    for observation in request.results ?? [] {
        guard let candidate = observation.topCandidates(1).first else { continue }
        let box = observation.boundingBox  // normalized, bottom-left origin
        let x = box.midX * phoneWidth, y = (1 - box.midY) * phoneHeight
        rows.append(["text": candidate.string, "x": Int(x.rounded()), "y": Int(y.rounded()),
                     "w": Int((box.width * phoneWidth).rounded()), "h": Int((box.height * phoneHeight).rounded()),
                     "confidence": Double(String(format: "%.2f", candidate.confidence)) ?? 0])
    }
    return rows.sorted { (($0["y"] as? Int ?? 0), ($0["x"] as? Int ?? 0)) < (($1["y"] as? Int ?? 0), ($1["x"] as? Int ?? 0)) }
}

func fold(_ text: String) -> String {
    text.folding(options: [.caseInsensitive, .diacriticInsensitive], locale: Locale(identifier: "tr_TR"))
        .trimmingCharacters(in: .whitespacesAndNewlines)
}

func look(_ mirror: Mirror, image wantImage: Bool, ocr wantText: Bool, previous: String?) -> [String: Any] {
    let shot = capture(mirror)
    let phone = scaled(shot.image, width: outputWidth)
    let hash = hex(differenceHash(shot.image))
    var out: [String: Any] = ["ok": true, "connected": shot.connected, "width": phone.width, "height": phone.height,
                              "hash": hash]
    if !shot.connected {
        out["message"] = recognize(shot.image, phoneWidth: outputWidth, fast: true).compactMap { $0["text"] as? String }
            .joined(separator: " ")
    }
    if let previous, previous == hash {
        out["unchanged"] = true
        return out
    }
    if wantImage { out["image_base64"] = jpeg(phone, quality: number("quality") ?? 0.6).base64EncodedString() }
    if wantText { out["text"] = recognize(shot.image, phoneWidth: outputWidth) }
    return out
}

// MARK: Input

struct StepError: Error {
    let code: String
    let message: String
}

/// Phone points to desktop points, checked against the phone's screen as it is right now.
func desktopPoint(_ mirror: Mirror, _ shot: Shot, x: Double, y: Double) throws -> CGPoint {
    let phoneHeight = outputWidth * shot.screen.height / shot.screen.width
    guard x.isFinite, y.isFinite, x >= 0, y >= 0, x <= outputWidth, y <= phoneHeight else {
        throw StepError(code: "IPHONE_POINT_OUTSIDE_SCREEN",
                        message: "(\(Int(x)), \(Int(y))) is off the iPhone screen (it is \(Int(outputWidth)) × \(Int(phoneHeight)) points).")
    }
    return CGPoint(x: mirror.frame.minX + shot.screen.minX + x / outputWidth * shot.screen.width,
                   y: mirror.frame.minY + shot.screen.minY + y / phoneHeight * shot.screen.height)
}

let source = CGEventSource(stateID: .hidSystemState)

func post(_ event: CGEvent?, pause: useconds_t = 12_000) {
    guard let event else { return }
    event.setIntegerValueField(.eventSourceUserData, value: eventMarker)
    event.post(tap: .cghidEventTap)
    usleep(pause)
}

func mouse(_ type: CGEventType, _ point: CGPoint, clicks: Int64 = 1) {
    let event = CGEvent(mouseEventSource: source, mouseType: type, mouseCursorPosition: point, mouseButton: .left)
    event?.setIntegerValueField(.mouseEventClickState, value: clicks)
    post(event)
}

func key(_ code: CGKeyCode, down: Bool, flags: CGEventFlags = []) {
    let event = CGEvent(keyboardEventSource: source, virtualKey: code, keyDown: down)
    event?.flags = flags
    post(event, pause: 8_000)
}

func press(_ code: CGKeyCode, modifiers: [(CGKeyCode, CGEventFlags)] = []) {
    var flags: CGEventFlags = []
    for (modifier, flag) in modifiers { flags.insert(flag); key(modifier, down: true, flags: flags) }
    key(code, down: true, flags: flags)
    key(code, down: false, flags: flags)
    for (modifier, flag) in modifiers.reversed() { flags.remove(flag); key(modifier, down: false, flags: flags) }
}

let command_ = (CGKeyCode(kVK_Command), CGEventFlags.maskCommand)
let shift_ = (CGKeyCode(kVK_Shift), CGEventFlags.maskShift)
let option_ = (CGKeyCode(kVK_Option), CGEventFlags.maskAlternate)

/// Which key (and modifiers) types each character on this Mac's keyboard layout: the mirror forwards key codes,
/// not text, so typing presses the keys a person would.
func keyMap() -> [Character: (CGKeyCode, Bool, Bool)] {
    var map: [Character: (CGKeyCode, Bool, Bool)] = [:]
    guard let layout = TISCopyCurrentKeyboardLayoutInputSource()?.takeRetainedValue(),
          let raw = TISGetInputSourceProperty(layout, kTISPropertyUnicodeKeyLayoutData) else { return map }
    let data = Unmanaged<CFData>.fromOpaque(raw).takeUnretainedValue()
    guard let bytes = CFDataGetBytePtr(data) else { return map }
    let keyboard = UnsafeRawPointer(bytes).assumingMemoryBound(to: UCKeyboardLayout.self)
    for (shift, option) in [(false, false), (true, false), (false, true), (true, true)] {
        for code in 0..<128 {
            var dead: UInt32 = 0
            var chars = [UniChar](repeating: 0, count: 4)
            var length = 0
            let modifiers = UInt32(((shift ? shiftKey : 0) | (option ? optionKey : 0)) >> 8)
            UCKeyTranslate(keyboard, UInt16(code), UInt16(kUCKeyActionDown), modifiers, UInt32(LMGetKbdType()),
                           OptionBits(kUCKeyTranslateNoDeadKeysBit), &dead, 4, &length, &chars)
            guard length == 1, let character = String(utf16CodeUnits: chars, count: 1).first,
                  map[character] == nil, !character.isNewline else { continue }
            map[character] = (CGKeyCode(code), shift, option)
        }
    }
    return map
}

func type(_ text: String) -> [String] {
    let map = keyMap()
    var missing: [String] = []
    for character in text {
        if character == " " { press(CGKeyCode(kVK_Space)); continue }
        if let (code, shift, option) = map[character] {
            var modifiers: [(CGKeyCode, CGEventFlags)] = []
            if shift { modifiers.append(shift_) }
            if option { modifiers.append(option_) }
            press(code, modifiers: modifiers)
            continue
        }
        // Not on this layout (emoji, other scripts): send the character itself.
        var units = Array(String(character).utf16)
        let down = CGEvent(keyboardEventSource: source, virtualKey: CGKeyCode(kVK_Space), keyDown: true)
        down?.keyboardSetUnicodeString(stringLength: units.count, unicodeString: &units)
        let up = CGEvent(keyboardEventSource: source, virtualKey: CGKeyCode(kVK_Space), keyDown: false)
        up?.keyboardSetUnicodeString(stringLength: units.count, unicodeString: &units)
        post(down); post(up)
        missing.append(String(character))
    }
    return missing
}

let namedKeys: [String: Int] = [
    "return": kVK_Return, "enter": kVK_Return, "escape": kVK_Escape, "delete": kVK_Delete, "backspace": kVK_Delete,
    "tab": kVK_Tab, "space": kVK_Space, "left": kVK_LeftArrow, "right": kVK_RightArrow, "up": kVK_UpArrow,
    "down": kVK_DownArrow,
]

/// Brings the mirror forward for a batch of input, then puts the pointer and the owner's app back. One batch at a time.
struct Forward {
    let previous: NSRunningApplication?
    let cursor: CGPoint?
    let wasFront: Bool
    let lock: Int32
}

func bringForward(_ mirror: Mirror) throws -> Forward {
    try? FileManager.default.createDirectory(atPath: (lockPath as NSString).deletingLastPathComponent, withIntermediateDirectories: true)
    let lock = open(lockPath, O_CREAT | O_RDWR, 0o600)
    if lock >= 0 { flock(lock, LOCK_EX) }
    if CGSessionCopyCurrentDictionary().map({ ($0 as NSDictionary)["CGSSessionScreenIsLocked"] as? Bool ?? false }) ?? false {
        if lock >= 0 { flock(lock, LOCK_UN); close(lock) }
        throw StepError(code: "MAC_SCREEN_LOCKED", message: "This Mac's screen is locked; iPhone Mirroring takes no input until it is unlocked.")
    }
    let previous = NSWorkspace.shared.frontmostApplication
    let forward = Forward(previous: previous, cursor: CGEvent(source: nil)?.location,
                          wasFront: previous?.processIdentifier == mirror.app.processIdentifier, lock: lock)
    if !forward.wasFront {
        mirror.app.activate()
        var waited = 0
        while NSWorkspace.shared.frontmostApplication?.processIdentifier != mirror.app.processIdentifier && waited < 75 {
            if waited == 25 {  // activation refused: ask through Accessibility
                AXUIElementSetAttributeValue(AXUIElementCreateApplication(mirror.app.processIdentifier),
                                             kAXFrontmostAttribute as CFString, kCFBooleanTrue)
            }
            spin(0.02); waited += 1
        }
        guard NSWorkspace.shared.frontmostApplication?.processIdentifier == mirror.app.processIdentifier else {
            putBack(forward)
            throw StepError(code: "IPHONE_MIRRORING_NOT_FRONT", message: "iPhone Mirroring could not be brought forward, so nothing was sent.")
        }
        usleep(120_000)
    }
    return forward
}

/// NSWorkspace learns which app is in front through the run loop; a helper with no app of its own turns it while it waits.
func spin(_ seconds: Double) { RunLoop.current.run(until: Date().addingTimeInterval(seconds)) }

func stillForward(_ mirror: Mirror) -> Bool {
    spin(0.005)
    return NSWorkspace.shared.frontmostApplication?.processIdentifier == mirror.app.processIdentifier
}

func putBack(_ forward: Forward) {
    usleep(40_000)
    if let cursor = forward.cursor { CGWarpMouseCursorPosition(cursor); CGAssociateMouseAndMouseCursorPosition(1) }
    if !forward.wasFront, let previous = forward.previous { previous.activate() }
    if forward.lock >= 0 { flock(forward.lock, LOCK_UN); close(forward.lock) }
}

/// Waits until the screen stops changing (or `limit` seconds pass); a screen that never changed returns sooner.
func settle(_ mirror: Mirror, before: UInt64, limit: Double) -> (Shot, Bool) {
    let deadline = Date().addingTimeInterval(limit)
    usleep(120_000)
    var shot = capture(mirror)
    var last = differenceHash(shot.image)
    var changed = last != before
    var still = 0
    while Date() < deadline {
        usleep(110_000)
        shot = capture(mirror)
        let now = differenceHash(shot.image)
        if now != before { changed = true }
        still = now == last ? still + 1 : 0
        last = now
        // Settled: unchanged twice after a change, or nothing moved for a while.
        if (changed && still >= 1) || (!changed && still >= 3) { break }
    }
    return (shot, changed)
}

/// The screen once it differs from `from` (or `limit` passes).
func waitChange(_ mirror: Mirror, from: UInt64, limit: Double) -> Shot {
    let deadline = Date().addingTimeInterval(limit)
    var shot = capture(mirror)
    while differenceHash(shot.image) == from && Date() < deadline {
        usleep(60_000)
        shot = capture(mirror)
    }
    return shot
}

/// The screen once two captures in a row match (after at least `minimum` seconds, at most `limit`).
func waitStill(_ mirror: Mirror, limit: Double, minimum: Double = 0) -> Shot {
    let start = Date()
    usleep(useconds_t(max(0.06, minimum) * 1_000_000))
    var shot = capture(mirror)
    var last = differenceHash(shot.image)
    while Date().timeIntervalSince(start) < limit {
        usleep(70_000)
        shot = capture(mirror)
        let now = differenceHash(shot.image)
        if now == last { break }
        last = now
    }
    return shot
}

func findText(_ shot: Shot, _ wanted: String, index: Int, exact: Bool) -> [String: Any]? {
    let target = fold(wanted)
    let rows = recognize(shot.image, phoneWidth: outputWidth).filter {
        let text = fold($0["text"] as? String ?? "")
        return exact ? text == target : text.contains(target)
    }
    return index < rows.count ? rows[index] : nil
}

func value(_ step: [String: Any], _ key: String) -> Double? {
    if let number = step[key] as? NSNumber { return number.doubleValue }
    if let text = step[key] as? String { return Double(text) }
    return nil
}

func point(_ step: [String: Any], _ mirror: Mirror, _ shot: Shot, x xKey: String = "x", y yKey: String = "y") throws -> CGPoint {
    guard let x = value(step, xKey), let y = value(step, yKey) else {
        throw StepError(code: "IPHONE_ARGUMENT_INVALID", message: "This step needs \(xKey) and \(yKey) in phone points.")
    }
    return try desktopPoint(mirror, shot, x: x, y: y)
}

func tap(_ at: CGPoint, times: Int = 1, hold: useconds_t = 0) {
    mouse(.mouseMoved, at)
    usleep(25_000)
    for tap in 1...times {
        mouse(.leftMouseDown, at, clicks: Int64(tap))
        if hold > 0 { usleep(hold) }
        mouse(.leftMouseUp, at, clicks: Int64(tap))
    }
}

/// One step of a batch. Returns what it did; throws when it could not.
func run(_ step: [String: Any], _ mirror: Mirror, _ shot: inout Shot) throws -> [String: Any] {
    let kind = (step["type"] as? String ?? "").lowercased().replacingOccurrences(of: "-", with: "_")
    var done: [String: Any] = ["type": kind]
    var settleFor = 1.2
    let before = differenceHash(shot.image)
    switch kind {
    case "tap", "double_tap", "long_press":
        let at = try point(step, mirror, shot)
        let hold = kind == "long_press" ? useconds_t(max(300, min(5000, value(step, "duration_ms") ?? 800)) * 1000) : 0
        tap(at, times: kind == "double_tap" ? 2 : 1, hold: hold)
    case "tap_text":
        guard let wanted = step["text"] as? String, !wanted.isEmpty else {
            throw StepError(code: "IPHONE_ARGUMENT_INVALID", message: "tap_text needs text.")
        }
        let index = Int(value(step, "index") ?? 0)
        guard let match = findText(shot, wanted, index: index, exact: step["exact"] as? Bool ?? false),
              let x = match["x"] as? Int, let y = match["y"] as? Int else {
            throw StepError(code: "IPHONE_TEXT_NOT_FOUND", message: "“\(wanted)” is not on the iPhone screen.")
        }
        tap(try desktopPoint(mirror, shot, x: Double(x), y: Double(y)))
        done["tapped"] = ["text": match["text"] ?? wanted, "x": x, "y": y]
    case "swipe":
        let from = try point(step, mirror, shot), to = try point(step, mirror, shot, x: "to_x", y: "to_y")
        let steps = 18
        let pause = useconds_t(max(80, min(2000, value(step, "duration_ms") ?? 300)) * 1000) / useconds_t(steps)
        mouse(.mouseMoved, from)
        mouse(.leftMouseDown, from)
        for index in 1...steps {
            let t = CGFloat(index) / CGFloat(steps)
            let at = CGPoint(x: from.x + (to.x - from.x) * t, y: from.y + (to.y - from.y) * t)
            post(CGEvent(mouseEventSource: source, mouseType: .leftMouseDragged, mouseCursorPosition: at, mouseButton: .left), pause: pause)
        }
        mouse(.leftMouseUp, to)
    case "scroll":
        // A scroll wheel moves lists on iOS; a drag (swipe) moves pages and sliders.
        let phoneHeight = outputWidth * shot.screen.height / shot.screen.width
        let at = try desktopPoint(mirror, shot, x: value(step, "x") ?? outputWidth / 2, y: value(step, "y") ?? phoneHeight / 2)
        let dy = value(step, "dy") ?? -300, dx = value(step, "dx") ?? 0
        mouse(.mouseMoved, at)
        let parts = 10
        for _ in 0..<parts {
            post(CGEvent(scrollWheelEvent2Source: source, units: .pixel, wheelCount: 2,
                         wheel1: Int32(dy / Double(parts)), wheel2: Int32(dx / Double(parts)), wheel3: 0), pause: 16_000)
        }
    case "type":
        guard let text = step["text"] as? String, !text.isEmpty, !text.contains("\n") else {
            throw StepError(code: "IPHONE_ARGUMENT_INVALID", message: "type needs one line of text (a key step with return submits).")
        }
        let asText = type(text)
        if !asText.isEmpty { done["sent_as_text"] = asText }
        if step["submit"] as? Bool == true { press(CGKeyCode(kVK_Return)) }
        settleFor = 0.6
    case "key":
        let name = (step["key"] as? String ?? "").lowercased()
        guard let code = namedKeys[name] else {
            throw StepError(code: "IPHONE_ARGUMENT_INVALID", message: "key is one of: " + namedKeys.keys.sorted().joined(separator: ", "))
        }
        press(CGKeyCode(code))
    case "home", "app_switcher", "spotlight":
        // iPhone Mirroring's View menu: Home Screen ⌘1, App Switcher ⌘2, Spotlight ⌘3.
        let code = kind == "home" ? kVK_ANSI_1 : kind == "app_switcher" ? kVK_ANSI_2 : kVK_ANSI_3
        press(CGKeyCode(code), modifiers: [command_])
    case "open_app":
        guard let name = step["app"] as? String, !name.isEmpty else {
            throw StepError(code: "IPHONE_ARGUMENT_INVALID", message: "open_app needs app.")
        }
        // Each step waits for the screen to answer instead of a fixed time: home, Spotlight open, results shown.
        press(CGKeyCode(kVK_ANSI_1), modifiers: [command_])
        shot = waitStill(mirror, limit: 0.8)
        let home = differenceHash(shot.image)
        press(CGKeyCode(kVK_ANSI_3), modifiers: [command_])
        shot = waitChange(mirror, from: home, limit: 1.2)
        _ = type(name)
        shot = waitStill(mirror, limit: 1.2, minimum: 0.25)
        press(CGKeyCode(kVK_Return))
        settleFor = 2.5
    case "wait":
        usleep(useconds_t(max(0, min(10_000, value(step, "ms") ?? 500)) * 1000))
        shot = capture(mirror)
        return done
    case "wait_for_text":
        guard let wanted = step["text"] as? String, !wanted.isEmpty else {
            throw StepError(code: "IPHONE_ARGUMENT_INVALID", message: "wait_for_text needs text.")
        }
        let deadline = Date().addingTimeInterval(max(0.2, min(30, (value(step, "timeout_ms") ?? 5000) / 1000)))
        let target = fold(wanted)
        repeat {
            shot = capture(mirror)
            if let match = recognize(shot.image, phoneWidth: outputWidth, fast: true)
                .first(where: { fold($0["text"] as? String ?? "").contains(target) }) {
                done["found"] = ["text": match["text"] ?? wanted, "x": match["x"] ?? 0, "y": match["y"] ?? 0]
                return done
            }
            usleep(250_000)
        } while Date() < deadline
        throw StepError(code: "IPHONE_TEXT_NOT_FOUND", message: "“\(wanted)” did not show up on the iPhone.")
    default:
        throw StepError(code: "IPHONE_ARGUMENT_INVALID", message: "Unknown step type “\(kind)”.")
    }
    let (after, changed) = settle(mirror, before: before, limit: value(step, "settle_ms").map { $0 / 1000 } ?? settleFor)
    shot = after
    done["changed"] = changed
    return done
}

func xy(_ index: Int) -> (Double, Double) {
    guard positional.count > index + 1, let x = Double(positional[index]), let y = Double(positional[index + 1]) else {
        fail("IPHONE_ARGUMENT_INVALID", "This action needs x and y in phone points.")
    }
    return (x, y)
}

// MARK: Commands

if !AXIsProcessTrusted() && !["status", "see", "read", "find"].contains(command) {
    fail("ACCESSIBILITY_REQUIRED", "Mac MCP needs Accessibility permission to send input to iPhone Mirroring.")
}

switch command {
case "status":
    guard let mirror = findMirror() else {
        let running = !NSRunningApplication.runningApplications(withBundleIdentifier: mirrorBundleID).isEmpty
        emit(["ok": true, "open": false, "running": running])
    }
    let message = AXIsProcessTrusted() ? mirrorMessage(mirror) : []
    var out: [String: Any] = ["ok": true, "open": true, "running": true, "frontmost": mirror.app.isActive,
                              "connected": message.isEmpty,
                              "window": ["x": mirror.frame.minX, "y": mirror.frame.minY, "width": mirror.frame.width, "height": mirror.frame.height]]
    if !message.isEmpty {
        out["message"] = message.joined(separator: " ")
        let lowered = out["message"] as? String ?? ""
        out["state"] = lowered.lowercased().contains("connecting") ? "connecting" : "message"
    }
    if let name = rememberName(from: message) { out["name"] = name }
    emit(out)

case "see", "read":
    let mirror = requireMirror()
    emit(look(mirror, image: command == "see", ocr: command == "read" || options["ocr"] == "1", previous: options["previous"]))

case "find":
    let mirror = requireMirror()
    let wanted = fold(positional.dropFirst().joined(separator: " "))
    guard !wanted.isEmpty else { fail("IPHONE_ARGUMENT_INVALID", "Say which text to find.") }
    let shot = capture(mirror)
    let rows = recognize(shot.image, phoneWidth: outputWidth).filter { fold($0["text"] as? String ?? "").contains(wanted) }
    emit(["ok": true, "matches": rows])

case "open-mirroring":
    if let app = NSRunningApplication.runningApplications(withBundleIdentifier: mirrorBundleID).first {
        app.activate()
    } else if let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: mirrorBundleID) {
        let semaphore = DispatchSemaphore(value: 0)
        NSWorkspace.shared.openApplication(at: url, configuration: NSWorkspace.OpenConfiguration()) { _, _ in semaphore.signal() }
        _ = semaphore.wait(timeout: .now() + 10)
    } else {
        fail("IPHONE_MIRRORING_UNAVAILABLE", "iPhone Mirroring is not on this Mac (it needs macOS 15 or later).")
    }
    emit(["ok": true])

case "connect":
    // The window's own Connect button, shown after the iPhone was used (the iPhone must be locked again).
    let mirror = requireMirror()
    let shot = capture(mirror)
    if shot.connected { emit(look(mirror, image: options["image"] != "0", ocr: false, previous: nil)) }
    guard let button = recognize(shot.image, phoneWidth: outputWidth).first(where: { fold($0["text"] as? String ?? "") == "connect" }),
          let x = button["x"] as? Int, let y = button["y"] as? Int, let at = try? desktopPoint(mirror, shot, x: Double(x), y: Double(y)) else {
        fail("IPHONE_NOT_CONNECTED", "iPhone Mirroring shows no Connect button; lock the iPhone and try again.")
    }
    let before = differenceHash(shot.image)
    do {
        let forward = try bringForward(mirror)
        tap(at)
        putBack(forward)
    } catch let error as StepError { fail(error.code, error.message) } catch { fail("IPHONE_INPUT_FAILED", "\(error)") }
    _ = settle(mirror, before: before, limit: 6)
    emit(look(mirror, image: options["image"] != "0", ocr: options["ocr"] == "1", previous: nil))

case "act":
    // Steps as JSON on stdin: {"steps": [{"type": "spotlight"}, {"type": "type", "text": "Safari"}, ...]}.
    let mirror = requireMirror()
    guard let data = try? FileHandle.standardInput.readToEnd(),
          let body = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
          let steps = body["steps"] as? [[String: Any]], !steps.isEmpty, steps.count <= 40 else {
        fail("IPHONE_ARGUMENT_INVALID", "Pass 1 to 40 steps.")
    }
    var shot = capture(mirror)
    if !shot.connected {
        let message = mirrorMessage(mirror).joined(separator: " ")
        fail("IPHONE_NOT_CONNECTED", "iPhone Mirroring is not showing the iPhone right now: \(message.isEmpty ? "it is connecting" : message)",
             ["message": message])
    }
    let first = differenceHash(shot.image)
    var results: [[String: Any]] = []
    var failure: [String: Any]?
    let forward: Forward
    do { forward = try bringForward(mirror) } catch let error as StepError { fail(error.code, error.message) } catch { fail("IPHONE_INPUT_FAILED", "\(error)") }
    for (index, step) in steps.enumerated() {
        guard stillForward(mirror) else {
            failure = ["step": index, "reason_code": "IPHONE_FOCUS_LOST", "error": "Another app came to the front, so the remaining steps were not sent."]
            break
        }
        do {
            results.append(try run(step, mirror, &shot))
        } catch let error as StepError {
            failure = ["step": index, "reason_code": error.code, "error": error.message]
            break
        } catch {
            failure = ["step": index, "reason_code": "IPHONE_INPUT_FAILED", "error": "\(error)"]
            break
        }
    }
    putBack(forward)
    var out = look(mirror, image: body["image"] as? Bool ?? true, ocr: body["ocr"] as? Bool ?? false, previous: nil)
    out["steps"] = results
    out["done"] = results.count
    out["changed"] = (out["hash"] as? String) != hex(first)
    if let failure {
        out["ok"] = false
        out["failed"] = failure
        out["reason_code"] = failure["reason_code"]
        out["error"] = "Step \((failure["step"] as? Int ?? 0) + 1) failed: \(failure["error"] ?? "")"
    }
    emit(out)

default:
    fail("IPHONE_ARGUMENT_INVALID", "Unknown command \(command).")
}
