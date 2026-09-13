import Foundation
import SafariServices

final class SafariWebExtensionHandler: NSObject, NSExtensionRequestHandling {
    func beginRequest(with context: NSExtensionContext) {
        let response = NSExtensionItem()
        context.completeRequest(returningItems: [response], completionHandler: nil)
    }
}
