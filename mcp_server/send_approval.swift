// Native confirmation panel shown before Mac MCP sends an email or a message.
//
// Reads one JSON request on stdin (so message text never appears in the process
// list), shows a floating panel with who it goes to and what it says, and prints
// one JSON line: {"decision": "send" | "cancel" | "timeout"}. Nothing is sent by
// this helper; the server sends only after reading "send". The panel does not
// take focus when it appears, so a keystroke meant for another app can never
// press a button; Send has no keyboard shortcut and Escape means Cancel.
import AppKit
import SwiftUI

struct Field: Decodable, Hashable {
    let label: String
    let value: String
}

struct Request: Decodable {
    let title: String
    let subtitle: String
    let appBundleId: String
    let fields: [Field]
    let body: String
    let bodyLabel: String
    let footnote: String
    let timeoutSeconds: Int
}

func finish(_ decision: String) -> Never {
    FileHandle.standardOutput.write("{\"decision\":\"\(decision)\"}\n".data(using: .utf8)!)
    exit(0)
}

func appIcon(_ bundleId: String) -> NSImage {
    if let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleId) {
        return NSWorkspace.shared.icon(forFile: url.path)
    }
    return NSImage(systemSymbolName: "paperplane.fill", accessibilityDescription: nil) ?? NSImage()
}

struct ApprovalView: View {
    let request: Request
    let icon: NSImage
    @State private var remaining: Int

    init(request: Request, icon: NSImage) {
        self.request = request
        self.icon = icon
        _remaining = State(initialValue: max(5, request.timeoutSeconds))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .center, spacing: 14) {
                Image(nsImage: icon).resizable().frame(width: 52, height: 52)
                VStack(alignment: .leading, spacing: 3) {
                    Text(request.title).font(.system(size: 17, weight: .semibold))
                    Text(request.subtitle).font(.callout).foregroundColor(.secondary)
                }
                Spacer(minLength: 0)
            }

            Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 12, verticalSpacing: 7) {
                ForEach(request.fields, id: \.self) { field in
                    GridRow {
                        Text(field.label).foregroundColor(.secondary).gridColumnAlignment(.trailing)
                        Text(field.value).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            .font(.system(size: 13))

            VStack(alignment: .leading, spacing: 6) {
                Text(request.bodyLabel).font(.caption).foregroundColor(.secondary)
                ScrollView {
                    Text(request.body.isEmpty ? "(empty)" : request.body)
                        .font(.system(size: 13))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(10)
                }
                .frame(minHeight: 70, maxHeight: 190)
                .background(RoundedRectangle(cornerRadius: 8).fill(Color(nsColor: .textBackgroundColor)))
                .overlay(RoundedRectangle(cornerRadius: 8).stroke(Color(nsColor: .separatorColor)))
            }

            Text(request.footnote).font(.caption).foregroundColor(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            HStack {
                Text("Closes without sending in \(remaining)s").font(.caption).foregroundColor(.secondary)
                Spacer()
                Button("Cancel") { finish("cancel") }
                    .keyboardShortcut(.cancelAction)
                // No keyboard shortcut: sending always takes a deliberate click.
                Button("Send") { finish("send") }
                    .buttonStyle(.borderedProminent)
            }
        }
        .padding(20)
        .frame(width: 500)
        .onReceive(Timer.publish(every: 1, on: .main, in: .common).autoconnect()) { _ in
            remaining -= 1
            if remaining <= 0 { finish("timeout") }
        }
    }
}

final class PanelDelegate: NSObject, NSWindowDelegate {
    func windowWillClose(_ notification: Notification) { finish("cancel") }
}

let input = FileHandle.standardInput.readDataToEndOfFile()
let decoder = JSONDecoder()
decoder.keyDecodingStrategy = .convertFromSnakeCase
guard let request = try? decoder.decode(Request.self, from: input) else {
    FileHandle.standardError.write("invalid approval request\n".data(using: .utf8)!)
    exit(2)
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let panel = NSPanel(
    contentRect: NSRect(x: 0, y: 0, width: 500, height: 420),
    styleMask: [.titled, .closable, .nonactivatingPanel, .fullSizeContentView],
    backing: .buffered, defer: false
)
panel.title = "Mac MCP"
panel.titlebarAppearsTransparent = true
panel.level = .floating
panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
panel.isReleasedWhenClosed = false
panel.becomesKeyOnlyIfNeeded = true
panel.standardWindowButton(.miniaturizeButton)?.isHidden = true
panel.standardWindowButton(.zoomButton)?.isHidden = true
let delegate = PanelDelegate()
panel.delegate = delegate
let hosting = NSHostingView(rootView: ApprovalView(request: request, icon: appIcon(request.appBundleId)))
panel.contentView = hosting
panel.setContentSize(hosting.fittingSize)
panel.center()
panel.orderFrontRegardless()
app.run()
