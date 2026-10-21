// Mac MCP Photos (#45): reads the Photos library for the Photos adapter, through PhotoKit.
//
// macOS asks the owner once whether this app may see their photos; the grant covers this helper
// alone (it runs from a copy in ~/.mac-mcp/helpers, like Mac MCP Messages Reader). It never opens
// the Photos app and never changes the library: it lists albums, finds items by their metadata and
// copies chosen originals into a folder. Mac MCP starts it with
// `open -W -n -g <helper> --args --request <json> --result <json>`.
//
// Requests: {"action": "access", "prompt": bool}
//           {"action": "list_albums", "limit": n}
//           {"action": "find_photos", "from": epoch?, "to": epoch?, "album": id-or-title?, "media_type":
//            "photo"|"video"?, "favorite": bool?, "name": text?, "limit": n}
//           {"action": "export", "ids": [...], "folder": path}
// Results:  {"ok": true, ...} or {"ok": false, "error": "photos_access" | "...", "message": ...}
// Item locations are never returned: an item only says whether it has one.

import Foundation
import Photos

private let maxLimit = 200
private let scanCap = 20_000
// PhotoKit retries forever when no library can be opened; nothing here may outlive the request's deadline.
private let defaultDeadline = 90.0

private func write(_ object: [String: Any], to path: String) {
    guard let data = try? JSONSerialization.data(withJSONObject: object) else { return }
    try? data.write(to: URL(fileURLWithPath: path), options: .atomic)
    chmod(path, 0o600)
}

private func fail(_ code: String, _ message: String, to path: String) -> Never {
    write(["ok": false, "error": code, "message": message], to: path)
    exit(code == "photos_access" ? 3 : 2)
}

private func iso(_ date: Date?) -> Any {
    guard let date else { return NSNull() }
    let formatter = ISO8601DateFormatter()
    formatter.timeZone = .current
    formatter.formatOptions = [.withInternetDateTime]
    return formatter.string(from: date)
}

private func statusName(_ status: PHAuthorizationStatus) -> String {
    switch status {
    case .authorized: return "granted"
    case .limited: return "limited"
    case .denied: return "denied"
    case .restricted: return "restricted"
    case .notDetermined: return "not_asked"
    @unknown default: return "unknown"
    }
}

/// Waits for a callback while keeping the run loop going (PhotoKit and the permission prompt need it).
private func wait(_ semaphore: DispatchSemaphore, seconds: Double) -> Bool {
    let deadline = Date().addingTimeInterval(seconds)
    while semaphore.wait(timeout: .now() + 0.05) == .timedOut {
        if Date() > deadline { return false }
        RunLoop.current.run(until: Date().addingTimeInterval(0.05))
    }
    return true
}

private func authorization(prompt: Bool) -> PHAuthorizationStatus {
    var status = PHPhotoLibrary.authorizationStatus(for: .readWrite)
    if status == .notDetermined && prompt {
        let done = DispatchSemaphore(value: 0)
        PHPhotoLibrary.requestAuthorization(for: .readWrite) { answer in
            status = answer
            done.signal()
        }
        _ = wait(done, seconds: 120)
    }
    return status
}

/// Whether the library Photos uses is on disk. PhotoKit itself waits a minute and more for one that is not,
/// then answers as if it were empty, so this is checked first: Photos keeps its library as a bookmark.
private func libraryOnDisk() -> Bool {
    if let bookmark = UserDefaults(suiteName: "com.apple.Photos")?.data(forKey: "IPXDefaultLibraryURLBookmark") {
        var stale = false
        guard let url = try? URL(resolvingBookmarkData: bookmark, options: [.withoutUI, .withoutMounting],
                                 relativeTo: nil, bookmarkDataIsStale: &stale) else { return false }
        return FileManager.default.fileExists(atPath: url.path)
    }
    let standard = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Pictures/Photos Library.photoslibrary")
    return FileManager.default.fileExists(atPath: standard.path)
}

private func mediaName(_ type: PHAssetMediaType) -> String {
    switch type {
    case .image: return "photo"
    case .video: return "video"
    case .audio: return "audio"
    default: return "other"
    }
}

private func primaryResource(_ asset: PHAsset) -> PHAssetResource? {
    let resources = PHAssetResource.assetResources(for: asset)
    let preferred: [PHAssetResourceType] = asset.mediaType == .video ? [.video, .fullSizeVideo] : [.photo, .fullSizePhoto]
    for type in preferred {
        if let match = resources.first(where: { $0.type == type }) { return match }
    }
    return resources.first
}

private func item(_ asset: PHAsset) -> [String: Any] {
    var out: [String: Any] = [
        "id": asset.localIdentifier, "media_type": mediaName(asset.mediaType), "created": iso(asset.creationDate),
        "width": asset.pixelWidth, "height": asset.pixelHeight, "favorite": asset.isFavorite,
        "has_location": asset.location != nil,
        "filename": primaryResource(asset)?.originalFilename ?? NSNull(),
    ]
    if asset.mediaType == .video { out["duration_s"] = (asset.duration * 10).rounded() / 10 }
    return out
}

private func albums(limit: Int) -> [[String: Any]] {
    var rows: [[String: Any]] = []
    for (type, kind) in [(PHAssetCollectionType.album, "album"), (PHAssetCollectionType.smartAlbum, "smart")] {
        let found = PHAssetCollection.fetchAssetCollections(with: type, subtype: .any, options: nil)
        found.enumerateObjects { collection, _, stop in
            let count = PHAsset.fetchAssets(in: collection, options: nil).count
            if kind == "smart" && count == 0 { return }  // empty system albums are noise
            rows.append(["id": collection.localIdentifier, "title": collection.localizedTitle ?? "", "kind": kind,
                         "count": count])
            if rows.count >= limit { stop.pointee = true }
        }
        if rows.count >= limit { break }
    }
    return rows
}

private func album(_ wanted: String, resultPath: String) -> PHAssetCollection {
    let byId = PHAssetCollection.fetchAssetCollections(withLocalIdentifiers: [wanted], options: nil)
    if let match = byId.firstObject { return match }
    var matches: [PHAssetCollection] = []
    for type in [PHAssetCollectionType.album, PHAssetCollectionType.smartAlbum] {
        PHAssetCollection.fetchAssetCollections(with: type, subtype: .any, options: nil).enumerateObjects { collection, _, _ in
            if (collection.localizedTitle ?? "").compare(wanted, options: [.caseInsensitive, .diacriticInsensitive]) == .orderedSame {
                matches.append(collection)
            }
        }
    }
    if matches.isEmpty { fail("album_not_found", "No album has that id or name.", to: resultPath) }
    if matches.count > 1 { fail("album_not_unique", "Several albums have that name; pass its id from list_albums.", to: resultPath) }
    return matches[0]
}

private func find(_ request: [String: Any], resultPath: String) -> [String: Any] {
    let limit = min(max((request["limit"] as? Int) ?? 20, 1), maxLimit)
    let options = PHFetchOptions()
    var predicates: [NSPredicate] = []
    if let from = request["from"] as? Double { predicates.append(NSPredicate(format: "creationDate >= %@", Date(timeIntervalSince1970: from) as NSDate)) }
    if let to = request["to"] as? Double { predicates.append(NSPredicate(format: "creationDate <= %@", Date(timeIntervalSince1970: to) as NSDate)) }
    if let media = request["media_type"] as? String {
        let type: PHAssetMediaType = media == "video" ? .video : .image
        predicates.append(NSPredicate(format: "mediaType == %d", type.rawValue))
    }
    if let favorite = request["favorite"] as? Bool { predicates.append(NSPredicate(format: "favorite == %@", NSNumber(value: favorite))) }
    if !predicates.isEmpty { options.predicate = NSCompoundPredicate(andPredicateWithSubpredicates: predicates) }
    options.sortDescriptors = [NSSortDescriptor(key: "creationDate", ascending: false)]
    let result: PHFetchResult<PHAsset>
    if let wanted = request["album"] as? String, !wanted.isEmpty {
        result = PHAsset.fetchAssets(in: album(wanted, resultPath: resultPath), options: options)
    } else {
        result = PHAsset.fetchAssets(with: options)
    }
    let name = ((request["name"] as? String) ?? "").folding(options: [.caseInsensitive, .diacriticInsensitive], locale: nil)
    var rows: [[String: Any]] = []
    var matched = 0
    var scanned = 0
    result.enumerateObjects { asset, _, stop in
        scanned += 1
        if !name.isEmpty {
            let file = (primaryResource(asset)?.originalFilename ?? "").folding(options: [.caseInsensitive, .diacriticInsensitive], locale: nil)
            if !file.contains(name) {
                if scanned >= scanCap { stop.pointee = true }
                return
            }
        }
        matched += 1
        if rows.count < limit { rows.append(item(asset)) }
        if (!name.isEmpty && scanned >= scanCap) || (name.isEmpty && rows.count >= limit) { stop.pointee = true }
    }
    let total = name.isEmpty ? result.count : matched
    var out: [String: Any] = ["ok": true, "count": rows.count, "total": total, "items": rows,
                              "truncated": total > rows.count]
    if !name.isEmpty && scanned >= scanCap && scanned < result.count { out["scan_limited"] = true }
    return out
}

private func uniqueURL(in folder: URL, named name: String) -> URL {
    var candidate = folder.appendingPathComponent(name)
    let stem = (name as NSString).deletingPathExtension
    let ext = (name as NSString).pathExtension
    var index = 2
    while FileManager.default.fileExists(atPath: candidate.path) {
        candidate = folder.appendingPathComponent(ext.isEmpty ? "\(stem) (\(index))" : "\(stem) (\(index)).\(ext)")
        index += 1
    }
    return candidate
}

private func export(_ request: [String: Any], resultPath: String) -> [String: Any] {
    let ids = (request["ids"] as? [String]) ?? []
    guard let folderPath = request["folder"] as? String, !ids.isEmpty else {
        fail("bad_request", "export needs ids and folder.", to: resultPath)
    }
    let folder = URL(fileURLWithPath: folderPath, isDirectory: true)
    try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
    let assets = PHAsset.fetchAssets(withLocalIdentifiers: ids, options: nil)
    var byId: [String: PHAsset] = [:]
    assets.enumerateObjects { asset, _, _ in byId[asset.localIdentifier] = asset }
    var rows: [[String: Any]] = []
    for id in ids {
        guard let asset = byId[id], let resource = primaryResource(asset) else {
            rows.append(["id": id, "ok": false, "error": "not_found"])
            continue
        }
        let target = uniqueURL(in: folder, named: resource.originalFilename)
        let options = PHAssetResourceRequestOptions()
        options.isNetworkAccessAllowed = true  // an original kept in iCloud is downloaded first
        let done = DispatchSemaphore(value: 0)
        var failure: Error?
        PHAssetResourceManager.default().writeData(for: resource, toFile: target, options: options) { error in
            failure = error
            done.signal()
        }
        if !wait(done, seconds: 180) {
            rows.append(["id": id, "ok": false, "error": "timed_out"])
        } else if let failure {
            rows.append(["id": id, "ok": false, "error": "export_failed", "message": failure.localizedDescription])
        } else {
            rows.append(["id": id, "ok": true, "path": target.path])
        }
    }
    return ["ok": true, "items": rows]
}

let arguments = CommandLine.arguments
func argument(_ name: String) -> String? {
    guard let index = arguments.firstIndex(of: name), index + 1 < arguments.count else { return nil }
    return arguments[index + 1]
}
guard let requestPath = argument("--request"), let resultPath = argument("--result") else {
    FileHandle.standardError.write(Data("usage: --request <json> --result <json>\n".utf8))
    exit(64)
}
guard let data = FileManager.default.contents(atPath: requestPath),
      let request = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
      let action = request["action"] as? String else {
    fail("bad_request", "The request is not readable JSON with an action.", to: resultPath)
}
let status = authorization(prompt: action != "access" || (request["prompt"] as? Bool ?? false))
if action == "access" {
    write(["ok": true, "access": statusName(status)], to: resultPath)
    exit(0)
}
// One answer, always: a stuck PhotoKit call ends the helper with a clear reason instead of a silent hang.
let deadline = min(max((request["deadline_s"] as? Double) ?? defaultDeadline, 10), 3600)
DispatchQueue.global().asyncAfter(deadline: .now() + deadline) {
    write(["ok": false, "error": "timed_out", "message": "The Photos library did not answer in time."], to: resultPath)
    exit(4)
}
guard status == .authorized || status == .limited else {
    fail("photos_access", "Mac MCP Photos may not see the Photos library (\(statusName(status))). The owner allows it "
         + "in System Settings › Privacy & Security › Photos.", to: resultPath)
}
if !libraryOnDisk() {
    fail("library_unavailable", "No Photos library is on this Mac (or the one Photos uses is on a disk that is not "
         + "connected). Open Photos once to create or choose the System Photo Library.", to: resultPath)
}
if let reason = PHPhotoLibrary.shared().unavailabilityReason {
    fail("library_unavailable", "The Photos library cannot be opened (\(reason.localizedDescription)). Open Photos once to "
         + "choose or create the System Photo Library.", to: resultPath)
}
var answer: [String: Any]
switch action {
case "list_albums":
    let limit = min(max((request["limit"] as? Int) ?? 50, 1), maxLimit)
    let rows = albums(limit: limit + 1)
    answer = ["ok": true, "count": min(rows.count, limit), "albums": Array(rows.prefix(limit)), "truncated": rows.count > limit]
case "find_photos":
    answer = find(request, resultPath: resultPath)
case "export":
    answer = export(request, resultPath: resultPath)
default:
    fail("bad_request", "Unknown action \(action).", to: resultPath)
}
answer["access"] = statusName(status)
write(answer, to: resultPath)
exit(0)
