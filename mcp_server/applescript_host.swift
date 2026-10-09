// A long-lived AppleScript runner for Mac MCP's browser bridge.
//
// Starting osascript costs ~150-200 ms per call before any Apple Event is sent.
// This process reads one JSON request per line ({"script": "..."}), runs it with
// NSAppleScript, and writes one JSON reply per line:
//   {"ok": true, "result": "..."}  or  {"ok": false, "error": "...", "number": -1728}
// Results are rendered as text the way osascript prints them for the value types
// the bridge uses (text, numbers, booleans, missing value, lists of those).
import Foundation

let missingValue: UInt32 = 0x6d73_6e67  // 'msng'

func render(_ descriptor: NSAppleEventDescriptor?) -> String {
    guard let descriptor else { return "" }
    if descriptor.descriptorType == missingValue || descriptor.typeCodeValue == missingValue {
        return "missing value"
    }
    if descriptor.descriptorType == 0x6c69_7374 {  // 'list'
        guard descriptor.numberOfItems > 0 else { return "" }
        return (1...descriptor.numberOfItems).map { render(descriptor.atIndex($0)) }.joined(separator: ", ")
    }
    if descriptor.descriptorType == 0x7472_7565 { return "true" }   // 'true'
    if descriptor.descriptorType == 0x6661_6c73 { return "false" }  // 'fals'
    if let text = descriptor.stringValue { return text }
    return ""
}

func emit(_ object: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: object),
          let line = String(data: data, encoding: .utf8) else { return }
    FileHandle.standardOutput.write((line + "\n").data(using: .utf8)!)
}

while let line = readLine(strippingNewline: true) {
    guard let data = line.data(using: .utf8),
          let request = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let source = request["script"] as? String else {
        emit(["ok": false, "error": "invalid request", "number": -1])
        continue
    }
    var errorInfo: NSDictionary?
    let result = autoreleasepool { () -> NSAppleEventDescriptor? in
        guard let script = NSAppleScript(source: source) else { return nil }
        return script.executeAndReturnError(&errorInfo)
    }
    if let info = errorInfo {
        let message = info[NSAppleScript.errorMessage] as? String ?? "AppleScript error"
        let number = info[NSAppleScript.errorNumber] as? Int ?? 0
        emit(["ok": false, "error": message, "number": number])
    } else {
        emit(["ok": true, "result": render(result)])
    }
}
