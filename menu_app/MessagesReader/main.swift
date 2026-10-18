// Mac MCP Messages Reader (#261): reads ~/Library/Messages/chat.db for the Messages adapter.
//
// macOS lets only an app the owner added under Privacy & Security › Full Disk Access open that
// database. This helper is that app, so the grant covers Messages alone instead of the Python
// that runs Mac MCP. It opens the one file read-only and runs one statement that SQLite itself
// reports as read-only; anything else is refused. Mac MCP starts it with
// `open -W -n -g <helper> --args --request <json> --result <json>`; the request is
// {"sql": "...", "params": [...], "max_rows": n} and the result {"ok", "columns", "rows"} or
// {"ok": false, "error": "full_disk_access" | "...", "message"}. Blobs come back as base64.
//
// Two SQL functions help the queries: mm_text(text, attributedBody) is a message's plain text (newer
// macOS keeps it only inside the archived attributedBody), and mm_fold(x) folds case, accents and the
// Turkish dotless i, so a search for "cay" finds "Çay".

import Foundation
import SQLite3

private let maxRowsCap = 5000
private let transient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)

/// The string inside an NSArchiver typedstream: it follows "NSString", a "+" marker and its length.
private func archivedText(_ data: Data) -> String? {
    let bytes = [UInt8](data)
    guard let start = bytes.firstRange(of: Array("NSString".utf8))?.upperBound else { return nil }
    guard let plus = bytes[start..<min(start + 16, bytes.count)].firstIndex(of: 0x2B), plus + 1 < bytes.count else {
        return nil
    }
    var index = plus + 1
    var size = Int(bytes[index])
    index += 1
    if size == 0x81, index + 2 <= bytes.count {
        size = Int(bytes[index]) | Int(bytes[index + 1]) << 8
        index += 2
    } else if size == 0x82, index + 4 <= bytes.count {
        size = (0..<4).reduce(0) { $0 | Int(bytes[index + $1]) << (8 * $1) }
        index += 4
    }
    guard size > 0, index + size <= bytes.count else { return nil }
    return String(decoding: bytes[index..<(index + size)], as: UTF8.self)
}

private func fold(_ text: String) -> String {
    text.folding(options: [.caseInsensitive, .diacriticInsensitive], locale: nil)
        .replacingOccurrences(of: "ı", with: "i")
}

private func resultText(_ context: OpaquePointer?, _ text: String?) {
    if let text { sqlite3_result_text(context, text, -1, transient) } else { sqlite3_result_null(context) }
}

private func registerFunctions(_ db: OpaquePointer?) {
    let flags = SQLITE_UTF8 | SQLITE_DETERMINISTIC
    sqlite3_create_function_v2(db, "mm_text", 2, flags, nil, { context, _, values in
        guard let values else { return sqlite3_result_null(context) }
        if sqlite3_value_type(values[0]) == SQLITE_TEXT, let plain = sqlite3_value_text(values[0]) {
            let text = String(cString: plain)
            if !text.isEmpty { return resultText(context, text) }
        }
        let count = Int(sqlite3_value_bytes(values[1]))
        guard sqlite3_value_type(values[1]) == SQLITE_BLOB, count > 0, let blob = sqlite3_value_blob(values[1]) else {
            return sqlite3_result_null(context)
        }
        resultText(context, archivedText(Data(bytes: blob, count: count)))
    }, nil, nil, nil)
    sqlite3_create_function_v2(db, "mm_fold", 1, flags, nil, { context, _, values in
        guard let values, let raw = sqlite3_value_text(values[0]) else { return sqlite3_result_null(context) }
        resultText(context, fold(String(cString: raw)))
    }, nil, nil, nil)
}

private func write(_ object: [String: Any], to path: String) {
    guard let data = try? JSONSerialization.data(withJSONObject: object) else { return }
    let url = URL(fileURLWithPath: path)
    try? data.write(to: url, options: .atomic)
    chmod(path, 0o600)
}

private func fail(_ code: String, _ message: String, to path: String) -> Never {
    write(["ok": false, "error": code, "message": message], to: path)
    exit(code == "full_disk_access" ? 3 : 2)
}

private func argument(_ name: String) -> String? {
    let args = CommandLine.arguments
    guard let index = args.firstIndex(of: name), index + 1 < args.count else { return nil }
    return args[index + 1]
}

guard let requestPath = argument("--request"), let resultPath = argument("--result") else {
    FileHandle.standardError.write("usage: --request <json> --result <json>\n".data(using: .utf8)!)
    exit(64)
}
guard let requestData = FileManager.default.contents(atPath: requestPath),
      let request = (try? JSONSerialization.jsonObject(with: requestData)) as? [String: Any],
      let sql = request["sql"] as? String else {
    fail("bad_request", "The request is not readable.", to: resultPath)
}
let params = request["params"] as? [Any] ?? []
let maxRows = min(max((request["max_rows"] as? Int) ?? 500, 1), maxRowsCap)

// --memory opens an empty in-memory database instead, so tests can run the SQL functions; it
// never names a file.
let inMemory = CommandLine.arguments.contains("--memory")
let database = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/Messages/chat.db").path
if !inMemory && !FileManager.default.fileExists(atPath: database) {
    // Without Full Disk Access the folder cannot even be listed, so a missing file reads the same.
    fail("full_disk_access", "Mac MCP Messages Reader cannot see the Messages database. Add it under "
         + "System Settings › Privacy & Security › Full Disk Access.", to: resultPath)
}

var db: OpaquePointer?
let uri = inMemory ? "file::memory:?mode=ro"
    : "file:" + database.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed)! + "?mode=ro"
let flags = SQLITE_OPEN_READONLY | SQLITE_OPEN_URI | SQLITE_OPEN_NOMUTEX
if sqlite3_open_v2(uri, &db, flags, nil) != SQLITE_OK {
    let message = db.map { String(cString: sqlite3_errmsg($0)) } ?? "cannot open"
    sqlite3_close(db)
    fail(message.contains("authorization") || message.contains("unable to open") ? "full_disk_access" : "open_failed",
         "Mac MCP Messages Reader cannot open the Messages database (\(message)). Add it under "
         + "System Settings › Privacy & Security › Full Disk Access.", to: resultPath)
}
sqlite3_busy_timeout(db, 3000)
registerFunctions(db)

var statement: OpaquePointer?
var tail: UnsafePointer<CChar>?
let sqlBytes = sql.utf8CString
let prepared = sqlBytes.withUnsafeBufferPointer { sqlite3_prepare_v2(db, $0.baseAddress, -1, &statement, &tail) }
let rest = tail.map { String(cString: $0) } ?? ""
if prepared == SQLITE_OK && !rest.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
    fail("one_statement", "Only one statement is allowed.", to: resultPath)
}
if prepared != SQLITE_OK {
    let message = String(cString: sqlite3_errmsg(db))
    if message.contains("authorization") || message.contains("unable to open") {
        fail("full_disk_access", "Mac MCP Messages Reader needs Full Disk Access to read Messages.", to: resultPath)
    }
    fail("bad_query", message, to: resultPath)
}
// Only statements SQLite itself calls read-only, and only one of them.
if sqlite3_stmt_readonly(statement) == 0 {
    fail("not_read_only", "Only reading is allowed.", to: resultPath)
}

for (offset, value) in params.enumerated() {
    let index = Int32(offset + 1)
    switch value {
    case let number as NSNumber where CFGetTypeID(number) == CFBooleanGetTypeID():
        sqlite3_bind_int64(statement, index, number.boolValue ? 1 : 0)
    case let number as NSNumber:
        if CFNumberIsFloatType(number) {
            sqlite3_bind_double(statement, index, number.doubleValue)
        } else {
            sqlite3_bind_int64(statement, index, number.int64Value)
        }
    case let text as String:
        sqlite3_bind_text(statement, index, text, -1, transient)
    case let object as [String: Any]:
        // {"blob": base64} binds bytes (used to look for text inside attributedBody).
        if let encoded = object["blob"] as? String, let data = Data(base64Encoded: encoded) {
            _ = data.withUnsafeBytes { sqlite3_bind_blob(statement, index, $0.baseAddress, Int32(data.count), transient) }
        } else {
            sqlite3_bind_null(statement, index)
        }
    default:
        sqlite3_bind_null(statement, index)
    }
}

let columnCount = sqlite3_column_count(statement)
let columns = (0..<columnCount).map { String(cString: sqlite3_column_name(statement, $0)) }
var rows: [[Any]] = []
var truncated = false
while true {
    let step = sqlite3_step(statement)
    if step == SQLITE_DONE { break }
    if step != SQLITE_ROW {
        fail("query_failed", String(cString: sqlite3_errmsg(db)), to: resultPath)
    }
    if rows.count >= maxRows {
        truncated = true
        break
    }
    var row: [Any] = []
    for column in 0..<columnCount {
        switch sqlite3_column_type(statement, column) {
        case SQLITE_INTEGER: row.append(sqlite3_column_int64(statement, column))
        case SQLITE_FLOAT: row.append(sqlite3_column_double(statement, column))
        case SQLITE_TEXT: row.append(String(cString: sqlite3_column_text(statement, column)))
        case SQLITE_BLOB:
            let count = Int(sqlite3_column_bytes(statement, column))
            if let bytes = sqlite3_column_blob(statement, column), count > 0 {
                row.append(["blob": Data(bytes: bytes, count: count).base64EncodedString()])
            } else {
                row.append(NSNull())
            }
        default: row.append(NSNull())
        }
    }
    rows.append(row)
}
sqlite3_finalize(statement)
sqlite3_close(db)
write(["ok": true, "columns": columns, "rows": rows, "truncated": truncated], to: resultPath)
