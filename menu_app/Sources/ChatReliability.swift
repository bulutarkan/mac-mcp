import Foundation

// MARK: Mac MCP Chat's small rules for when things go wrong: a dropped connection, a failed send, two pastes
// in one second, a picture that goes as a file link. Plain logic, so a test can run it without the window.

/// How the open chat's live updates are doing. It moves only on real outcomes (the event stream opened or
/// dropped, a poll answered or failed), never on a timer.
enum ChatConnection: Equatable {
    case live
    case reconnecting
    case offline
}

struct ChatLink: Equatable {
    /// Failed tries in a row (the stream, or polls without one) before "Reconnecting…" turns into "Offline".
    static let offlineAfter = 3
    private(set) var state: ChatConnection = .live
    /// Tries that failed since the last good one; it also paces the next try.
    private(set) var failures = 0

    /// The event stream answered: updates arrive live again.
    mutating func opened() {
        failures = 0
        state = .live
    }

    /// The event stream ended or could not open (server restart, sleep, network).
    mutating func dropped() {
        failures += 1
        if state == .live { state = .reconnecting }
    }

    /// A poll's outcome. With a stream, the stream is the connection: a poll only tells, while the stream is down,
    /// whether Mac MCP answers at all (a good one does not make it live). Without one, polls are the connection.
    mutating func polled(ok: Bool, streaming: Bool) {
        if streaming {
            guard state != .live else { return }
            if ok {
                if state == .offline { state = .reconnecting }
            } else if failures >= Self.offlineAfter {
                state = .offline
            }
            return
        }
        if ok {
            failures = 0
            state = .live
        } else {
            failures += 1
            state = failures >= Self.offlineAfter ? .offline : .reconnecting
        }
    }

    /// The owner pressed Retry: try at once, counting afresh.
    mutating func retrying() {
        failures = 0
        state = .reconnecting
    }
}

/// A message whose send failed, kept with the chat it was written in so it is never lost.
struct ChatFailedSend: Identifiable, Hashable {
    let id = UUID()
    let chatID: String
    let text: String
    let files: [URL]
    /// Why it failed, as the server or the app said it.
    let reason: String
}

enum ChatDraftRestore {
    /// Whether a failed send may go back into the message box: only in its own chat, and only while the box is
    /// empty, so nothing the owner typed since is overwritten.
    static func fits(failedChat: String, openChat: String?, draft: String, attachments: Int) -> Bool {
        failedChat == openChat && draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && attachments == 0
    }
}

enum ChatPaste {
    /// "Pasted image 2026-10-26 at 14.03.22.png", like a macOS screenshot; the second one in the same second
    /// is "… 14.03.22 2.png", and so on.
    static func imageName(stamp: String, copy: Int) -> String {
        copy <= 1 ? "Pasted image \(stamp).png" : "Pasted image \(stamp) \(copy).png"
    }

    /// Writes the picture under the first free name (never over another one) and returns where it went.
    static func save(_ data: Data, in folder: URL, stamp: String) throws -> URL {
        for copy in 1...999 {
            let file = folder.appendingPathComponent(imageName(stamp: stamp, copy: copy))
            do {
                try data.write(to: file, options: .withoutOverwriting)
                return file
            } catch let error as CocoaError where error.code == .fileWriteFileExists {
                continue
            }
        }
        throw CocoaError(.fileWriteFileExists)
    }
}

/// When an image goes to the agent as a file link instead of a picture. The server decides the same way
/// (chat_sessions.py: IMAGE_TYPES, MAX_IMAGE_BYTES and the agent's promptCapabilities).
enum ChatImageNote {
    /// What the app shows as a picture.
    static let imageTypes: Set<String> = ["png", "jpg", "jpeg", "gif", "webp", "heic", "heif", "tiff", "tif", "bmp"]
    /// What goes to the agent as a picture.
    static let pictureTypes: Set<String> = ["png", "jpg", "jpeg", "gif", "webp"]
    static let pictureLimit = 8 * 1024 * 1024

    /// A short note for the attachment, or nil when it goes as a picture (or is not an image at all).
    /// `takesImages` is nil until the agent has said, so nothing is claimed before then.
    static func note(fileExtension: String, size: Int, takesImages: Bool?, agent: String) -> String? {
        let type = fileExtension.lowercased()
        guard imageTypes.contains(type) else { return nil }
        if !pictureTypes.contains(type) { return "Goes as a file link: agents take PNG, JPEG, GIF or WebP" }
        if size > pictureLimit { return "Goes as a file link: over 8 MB" }
        if takesImages == false { return "Goes as a file link: \(agent) doesn't take pictures" }
        return nil
    }
}
