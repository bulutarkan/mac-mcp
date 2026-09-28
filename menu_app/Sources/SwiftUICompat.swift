import SwiftUI

// Xcode 27/macOS 27 SDK also exposes the State attribute spelling as a Swift macro. Some standalone
// Command Line Tools releases do not ship the SwiftUIMacros host plugin needed
// to expand that spelling. A distinct alias deliberately selects SwiftUI's
// long-standing State property wrapper, which is sufficient for Mac MCP's
// simple local view state and remains compatible with older SDKs.
typealias MacMCPState<Value> = SwiftUI.State<Value>
