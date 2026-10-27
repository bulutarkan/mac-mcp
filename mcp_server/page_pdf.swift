// Renders a page snapshot to a PDF file without showing anything on screen.
//
// Reads one JSON request on stdin: {"html": "...", "base_url": "https://...", "out": "/path.pdf",
// "width": 1280}. The HTML is the DOM the browser tab already shows (taken by Mac MCP), so
// nothing is fetched again except its images and styles, in a private, cookie-free WebKit view
// with page scripts turned off. The helper has no Dock icon, opens no window and never becomes
// the active app, so the person's focus stays where it is. Prints one JSON line:
// {"ok": true, "bytes": N, "width": W, "height": H} or {"ok": false, "error": "..."}.
import AppKit
import WebKit

struct Request: Decodable {
    let html: String
    let base_url: String?
    let out: String
    let width: Double?
    let timeout_s: Double?
}

func finish(_ payload: [String: Any]) -> Never {
    let data = (try? JSONSerialization.data(withJSONObject: payload)) ?? Data("{\"ok\":false}".utf8)
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
    exit(0)
}

final class Renderer: NSObject, WKNavigationDelegate {
    let request: Request
    let view: WKWebView
    var done = false

    init(_ request: Request) {
        self.request = request
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .nonPersistent()
        config.defaultWebpagePreferences.allowsContentJavaScript = false
        config.mediaTypesRequiringUserActionForPlayback = .all
        let width = max(320, min(request.width ?? 1280, 2400))
        view = WKWebView(frame: NSRect(x: 0, y: 0, width: width, height: 900), configuration: config)
        super.init()
        view.navigationDelegate = self
    }

    func start() {
        let base = request.base_url.flatMap { URL(string: $0) }
        view.loadHTMLString(request.html, baseURL: base)
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        // Only the snapshot itself loads as a page; a meta refresh or link never navigates away.
        decisionHandler(navigationAction.navigationType == .other && !done ? .allow : .cancel)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        // Images and fonts settle a moment after the document finishes.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) { self.render() }
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        finish(["ok": false, "error": "load_failed", "message": error.localizedDescription])
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        finish(["ok": false, "error": "load_failed", "message": error.localizedDescription])
    }

    func render() {
        if done { return }
        done = true
        // Scripts are off, so the content size comes from the laid-out view itself.
        view.createPDF(configuration: WKPDFConfiguration()) { result in
            switch result {
            case .success(let data):
                do {
                    try data.write(to: URL(fileURLWithPath: self.request.out), options: [.withoutOverwriting])
                    finish(["ok": true, "bytes": data.count, "width": Int(self.view.frame.width)])
                } catch {
                    finish(["ok": false, "error": "write_failed", "message": error.localizedDescription])
                }
            case .failure(let error):
                finish(["ok": false, "error": "pdf_failed", "message": error.localizedDescription])
            }
        }
    }
}

let input = FileHandle.standardInput.readDataToEndOfFile()
guard let request = try? JSONDecoder().decode(Request.self, from: input) else {
    finish(["ok": false, "error": "invalid_request"])
}
let app = NSApplication.shared
app.setActivationPolicy(.prohibited)
let renderer = Renderer(request)
DispatchQueue.main.asyncAfter(deadline: .now() + max(5, min(request.timeout_s ?? 30, 120))) {
    finish(["ok": false, "error": "timeout"])
}
renderer.start()
app.run()
