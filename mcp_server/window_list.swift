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

do {
    let encoder = JSONEncoder()
    let data = try encoder.encode(rows)
    FileHandle.standardOutput.write(data)
} catch {
    fputs("Could not encode CoreGraphics window list: \(error)\n", stderr)
    exit(2)
}
