import CoreGraphics
import Foundation

struct WindowRow: Codable {
    let id: UInt32
    let pid: Int32
    let owner: String
    let title: String
    let layer: Int
    let onScreen: Bool
    let x: Double
    let y: Double
    let width: Double
    let height: Double
}

// Displays in the same global point space as window bounds, AX positions and
// cliclick: origin at the main display's top-left, y down, other displays may
// sit at negative coordinates. scale is backing pixels per point.
struct DisplayRow: Codable {
    let id: UInt32
    let main: Bool
    let x: Double
    let y: Double
    let width: Double
    let height: Double
    let scale: Double
}

struct Output: Codable {
    let windows: [WindowRow]
    let displays: [DisplayRow]
}

let rawRows = CGWindowListCopyWindowInfo([.optionAll], kCGNullWindowID) as? [[String: Any]] ?? []
var rows: [WindowRow] = []
rows.reserveCapacity(rawRows.count)

for item in rawRows {
    guard
        let number = item[kCGWindowNumber as String] as? NSNumber,
        let pid = item[kCGWindowOwnerPID as String] as? NSNumber,
        let owner = item[kCGWindowOwnerName as String] as? String,
        let bounds = item[kCGWindowBounds as String] as? [String: Any]
    else {
        continue
    }

    rows.append(
        WindowRow(
            id: number.uint32Value,
            pid: pid.int32Value,
            owner: owner,
            title: item[kCGWindowName as String] as? String ?? "",
            layer: (item[kCGWindowLayer as String] as? NSNumber)?.intValue ?? 0,
            onScreen: (item[kCGWindowIsOnscreen as String] as? NSNumber)?.boolValue ?? false,
            x: (bounds["X"] as? NSNumber)?.doubleValue ?? 0,
            y: (bounds["Y"] as? NSNumber)?.doubleValue ?? 0,
            width: (bounds["Width"] as? NSNumber)?.doubleValue ?? 0,
            height: (bounds["Height"] as? NSNumber)?.doubleValue ?? 0
        )
    )
}

var displayCount: UInt32 = 0
CGGetActiveDisplayList(0, nil, &displayCount)
var displayIDs = [CGDirectDisplayID](repeating: 0, count: Int(displayCount))
CGGetActiveDisplayList(displayCount, &displayIDs, &displayCount)
var displays: [DisplayRow] = []
for displayID in displayIDs.prefix(Int(displayCount)) {
    let bounds = CGDisplayBounds(displayID)
    var scale = 1.0
    if let mode = CGDisplayCopyDisplayMode(displayID), mode.width > 0 {
        scale = Double(mode.pixelWidth) / Double(mode.width)
    }
    displays.append(DisplayRow(
        id: displayID, main: CGDisplayIsMain(displayID) != 0,
        x: Double(bounds.origin.x), y: Double(bounds.origin.y),
        width: Double(bounds.size.width), height: Double(bounds.size.height), scale: scale
    ))
}

do {
    let encoder = JSONEncoder()
    let data = try encoder.encode(Output(windows: rows, displays: displays))
    FileHandle.standardOutput.write(data)
} catch {
    fputs("Could not encode CoreGraphics window list: \(error)\n", stderr)
    exit(2)
}
