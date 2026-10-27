import AppKit
import Combine
import SwiftUI
import UniformTypeIdentifiers

/// ask_user in Mac MCP's own window instead of a two-step macOS dialog.
///
/// The server registers the question and nudges the app over /dashboard/events; the app claims it
/// and shows it where live activity lives: grown out of the notch island when the island is the
/// chosen style, otherwise as a card at the top right (also when live activity is off). The owner
/// types an answer or picks a suggestion, and may add files; those are copied into the question's
/// upload folder, and the server hands pictures to the agent as base64. A confirmation (ask_confirmation,
/// and Mac MCP's own approvals for sending or uploading) shows only its two buttons, and nothing is
/// confirmed unless its confirm button is pressed. Questions from several agents wait in a queue,
/// oldest first; a question's time starts when it comes to the front. It never takes focus by itself:
/// a click on it does.

// MARK: - Data

struct OwnerPrompt: Identifiable, Equatable {
    let id: String
    let question: String
    let sender: String
    let agentID: String?
    let suggestions: [String]
    let allowAttachments: Bool
    let uploadDir: URL?
    let expiresAt: Date
    let timeoutSeconds: Int
    /// A Yes/No question: only its two buttons, no text field and no files.
    let isConfirmation: Bool
    let confirmLabel: String
    let denyLabel: String

    init?(_ row: [String: Any]) {
        guard let id = row["prompt_id"] as? String, !id.isEmpty,
              let question = row["question"] as? String,
              let expires = row["expires_at"] as? Double else { return nil }
        self.id = id
        self.question = question
        let sender = (row["sender"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        self.sender = sender.isEmpty || sender == "AI" ? "An agent" : sender
        agentID = row["agent_id"] as? String
        suggestions = row["suggestions"] as? [String] ?? []
        uploadDir = (row["upload_dir"] as? String).map { URL(fileURLWithPath: $0, isDirectory: true) }
        allowAttachments = (row["allow_attachments"] as? Bool ?? false) && uploadDir != nil
        expiresAt = Date(timeIntervalSince1970: expires)
        timeoutSeconds = max(1, row["timeout_s"] as? Int ?? 60)
        isConfirmation = row["kind"] as? String == "confirm"
        confirmLabel = (row["confirm_label"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? "Yes"
        denyLabel = (row["deny_label"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? "No"
    }
}

struct OwnerPromptAttachment: Identifiable, Equatable {
    let id = UUID()
    let url: URL
    let isImage: Bool

    var name: String { url.lastPathComponent }
}

// MARK: - Model and controller

@MainActor
final class OwnerPromptModel: ObservableObject {
    @Published var queue: [OwnerPrompt] = []
    @Published var draft = ""
    @Published var attachments: [OwnerPromptAttachment] = []
    @Published var problem: String?
    @Published var sending = false
    @Published var style: OwnerPromptPlacement.Style = .card
    @Published var appeared = false
    @Published var now = Date()
    @Published var notch = NotchGeometry.detect(NotchGeometry.targetScreen())

    var current: OwnerPrompt? { queue.first }
}

@MainActor
final class OwnerPromptController {
    static let shared = OwnerPromptController()

    let model = OwnerPromptModel()
    private weak var state: AppState?
    private var window: OwnerPromptPanel?
    private var host: OwnerPromptHostingView?
    private var claimed: Set<String> = []
    private var refreshing = false
    private var refreshAgain = false
    private var ticker: Timer?
    private var subscriptions: Set<AnyCancellable> = []
    private var contentHeight: CGFloat = 200

    func attach(_ state: AppState) {
        self.state = state
        state.onOwnerPromptsChanged = { [weak self] in
            Task { await self?.refresh() }
        }
        state.settings.$showLiveIsland.removeDuplicates().sink { [weak self] on in
            guard let self else { return }
            let style = OwnerPromptPlacement.style(showLiveIsland: on)
            guard style != self.model.style else { return }
            self.model.style = style
            LiveIslandController.shared.setPromptCovering(style == .island && self.window?.isVisible == true)
            self.layout()
        }.store(in: &subscriptions)
    }

    /// Re-read the open questions; claim the new ones so the server does not fall back to a dialog.
    func refresh() async {
        guard let state else { return }
        if refreshing { refreshAgain = true; return }
        refreshing = true
        defer { refreshing = false }
        repeat {
            refreshAgain = false
            let result = await state.taskAPI("dashboard/api/owner-prompts")
            guard result.ok else { return }
            let rows = (result.object["prompts"] as? [[String: Any]] ?? []).compactMap(OwnerPrompt.init)
            var shown: [OwnerPrompt] = []
            for prompt in rows {
                if !claimed.contains(prompt.id) {
                    let claim = await state.taskAPI("dashboard/api/owner-prompts/claim", body: ["prompt_id": prompt.id])
                    guard claim.ok else { continue }
                    claimed.insert(prompt.id)
                }
                shown.append(prompt)
            }
            claimed.formIntersection(rows.map(\.id))
            apply(shown)
        } while refreshAgain
    }

    private func apply(_ prompts: [OwnerPrompt]) {
        let before = model.current?.id
        if prompts != model.queue { model.queue = prompts }
        if model.current?.id != before { resetDraft() }
        if prompts.isEmpty { hide() } else { show() }
    }

    private func resetDraft() {
        model.draft = ""
        model.attachments = []
        model.problem = nil
        model.sending = false
    }

    // MARK: Answers

    func send(_ text: String? = nil) {
        guard let state, let prompt = model.current, !model.sending else { return }
        let answer = text ?? model.draft
        let files = text == nil ? model.attachments : []
        guard !answer.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !files.isEmpty else { return }
        model.sending = true
        model.problem = nil
        Task {
            let result = await state.taskAPI("dashboard/api/owner-prompts/answer", body: [
                "prompt_id": prompt.id, "action": "send", "text": answer,
                "attachments": files.map { ["path": $0.url.path] },
            ])
            model.sending = false
            let error = result.object["error"] as? String
            if result.ok || error == "prompt_not_found" {
                finish(prompt.id)
            } else {
                model.problem = Self.problemText(error)
            }
        }
    }

    /// The answer to a confirmation: only `confirm: true` approves.
    func decide(confirm: Bool) {
        guard let state, let prompt = model.current, prompt.isConfirmation, !model.sending else { return }
        model.sending = true
        model.problem = nil
        Task {
            let result = await state.taskAPI("dashboard/api/owner-prompts/answer", body: [
                "prompt_id": prompt.id, "action": confirm ? "confirm" : "deny",
            ])
            model.sending = false
            if result.ok || result.object["error"] as? String == "prompt_not_found" {
                finish(prompt.id)
            } else {
                model.problem = Self.problemText(result.object["error"] as? String)
            }
        }
    }

    func skip() {
        guard let state, let prompt = model.current else { return }
        // Files added for an answer that is not sent leave with the question (the server removes its folder).
        finish(prompt.id)
        Task { _ = await state.taskAPI("dashboard/api/owner-prompts/answer", body: ["prompt_id": prompt.id, "action": "skip"]) }
    }

    private func finish(_ id: String) {
        claimed.remove(id)
        apply(model.queue.filter { $0.id != id })
    }

    private static func problemText(_ error: String?) -> String {
        switch error {
        case "invalid_attachment": return "A file could not be attached. Remove it and try again."
        case "attachment_too_large": return "Each file can be up to 50 MB."
        case "too_many_attachments": return "Up to \(OwnerPromptPlacement.maxAttachments) files."
        case "answer_too_long": return "The answer is too long."
        default: return "Mac MCP did not take the answer. Try again."
        }
    }

    // MARK: Files

    func chooseFiles() {
        guard let panel = window else { return }
        let picker = NSOpenPanel()
        picker.allowsMultipleSelection = true
        picker.canChooseDirectories = false
        picker.canChooseFiles = true
        picker.prompt = "Attach"
        picker.message = "Choose files to send with your answer"
        // The question floats over the menu bar; let the picker come in front of it meanwhile.
        panel.level = .floating
        NSApp.activate(ignoringOtherApps: true)
        picker.begin { [weak self] response in
            Task { @MainActor in
                panel.level = OwnerPromptPanel.level
                if response == .OK { self?.addFiles(picker.urls) }
            }
        }
    }

    func addFiles(_ urls: [URL]) {
        guard let prompt = model.current, prompt.allowAttachments, let folder = prompt.uploadDir else { return }
        for url in urls {
            guard model.attachments.count < OwnerPromptPlacement.maxAttachments else {
                model.problem = "Up to \(OwnerPromptPlacement.maxAttachments) files."
                break
            }
            let values = try? url.resourceValues(forKeys: [.isRegularFileKey, .fileSizeKey, .contentTypeKey])
            guard values?.isRegularFile == true else {
                model.problem = "Folders can’t be attached; choose files."
                continue
            }
            if (values?.fileSize ?? 0) > OwnerPromptPlacement.maxAttachmentBytes {
                model.problem = "\(url.lastPathComponent) is larger than 50 MB."
                continue
            }
            let name = OwnerPromptPlacement.uniqueName(url.lastPathComponent, taken: Set(model.attachments.map(\.name)))
            let copy = folder.appendingPathComponent(name)
            do {
                try FileManager.default.copyItem(at: url, to: copy)
            } catch {
                model.problem = "\(url.lastPathComponent) could not be copied."
                continue
            }
            let isImage = values?.contentType?.conforms(to: .image) ?? UTType(filenameExtension: url.pathExtension)?.conforms(to: .image) ?? false
            model.attachments.append(OwnerPromptAttachment(url: copy, isImage: isImage))
            model.problem = nil
        }
    }

    /// A picture dropped without a file behind it (from a browser, say) is saved as PNG.
    func addImage(_ image: NSImage) {
        guard let prompt = model.current, prompt.allowAttachments, let folder = prompt.uploadDir,
              let tiff = image.tiffRepresentation, let bitmap = NSBitmapImageRep(data: tiff),
              let png = bitmap.representation(using: .png, properties: [:]) else { return }
        let name = OwnerPromptPlacement.uniqueName("Image.png", taken: Set(model.attachments.map(\.name)))
        let url = folder.appendingPathComponent(name)
        guard (try? png.write(to: url)) != nil else { return }
        model.attachments.append(OwnerPromptAttachment(url: url, isImage: true))
    }

    func remove(_ attachment: OwnerPromptAttachment) {
        try? FileManager.default.removeItem(at: attachment.url)
        model.attachments.removeAll { $0.id == attachment.id }
    }

    func handleDrop(_ providers: [NSItemProvider]) -> Bool {
        guard model.current?.allowAttachments == true else { return false }
        var handled = false
        for provider in providers {
            if provider.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier) {
                handled = true
                _ = provider.loadObject(ofClass: URL.self) { url, _ in
                    guard let url else { return }
                    Task { @MainActor in self.addFiles([url]) }
                }
            } else if provider.canLoadObject(ofClass: NSImage.self) {
                handled = true
                _ = provider.loadObject(ofClass: NSImage.self) { image, _ in
                    guard let image = image as? NSImage else { return }
                    Task { @MainActor in self.addImage(image) }
                }
            }
        }
        return handled
    }

    // MARK: Who asks

    func agent(for prompt: OwnerPrompt) -> AgentInfo? {
        guard let id = prompt.agentID, !id.isEmpty else { return nil }
        return state?.agents.first { $0.agentID == id }
    }

    func logo(for prompt: OwnerPrompt) -> NSImage? {
        OwnerPromptPlacement.providerID(sender: prompt.sender, agentProvider: agent(for: prompt)?.provider)
            .flatMap(IslandIcon.logo)
    }

    func subtitle(for prompt: OwnerPrompt) -> String {
        guard let agent = agent(for: prompt) else { return "asks you" }
        return "\(LiveIslandController.providerName(agent.provider)) · \(LiveIslandController.agentName(agent))"
    }

    // MARK: Window

    private func show() {
        let panel = ensureWindow()
        model.style = OwnerPromptPlacement.style(showLiveIsland: state?.settings.showLiveIsland == true)
        model.notch = NotchGeometry.detect(NotchGeometry.targetScreen())
        LiveIslandController.shared.setPromptCovering(model.style == .island)
        layout()
        startTicker()
        guard !panel.isVisible else { return }
        model.appeared = false
        panel.alphaValue = 1
        panel.orderFrontRegardless()
        DispatchQueue.main.async {
            withAnimation(IslandMotion.open) { self.model.appeared = true }
        }
    }

    private func hide() {
        ticker?.invalidate()
        ticker = nil
        LiveIslandController.shared.setPromptCovering(false)
        guard let panel = window, panel.isVisible else { return }
        withAnimation(IslandMotion.close) { model.appeared = false }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.28) {
            guard self.model.queue.isEmpty else { return }
            panel.orderOut(nil)
        }
    }

    private func startTicker() {
        guard ticker == nil else { return }
        ticker = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self else { return }
                self.model.now = Date()
                // The server closes a question at its time; catch up if that event was missed.
                if let prompt = self.model.current, prompt.expiresAt.timeIntervalSinceNow < -3 {
                    await self.refresh()
                }
            }
        }
    }

    func contentHeightChanged(_ height: CGFloat) {
        guard height > 0, abs(height - contentHeight) > 0.5 else { return }
        contentHeight = height
        layout()
    }

    private func layout() {
        guard let panel = window, let screen = NotchGeometry.targetScreen() ?? NSScreen.main else { return }
        let frame = OwnerPromptPlacement.frame(style: model.style, contentHeight: contentHeight,
                                               screen: screen.frame, visible: screen.visibleFrame)
        if panel.frame != frame { panel.setFrame(frame, display: true) }
        host?.shapeInsets = model.style == .island
            ? NSEdgeInsets(top: 0, left: OwnerPromptPlacement.margin, bottom: OwnerPromptPlacement.margin, right: OwnerPromptPlacement.margin)
            : NSEdgeInsets(top: OwnerPromptPlacement.margin, left: OwnerPromptPlacement.margin,
                           bottom: OwnerPromptPlacement.margin, right: OwnerPromptPlacement.margin)
    }

    private func ensureWindow() -> OwnerPromptPanel {
        if let window { return window }
        let panel = OwnerPromptPanel(contentRect: NSRect(x: 0, y: 0, width: 400, height: 200),
                                     styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.level = OwnerPromptPanel.level
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle]
        panel.isMovable = false
        panel.hidesOnDeactivate = false
        panel.becomesKeyOnlyIfNeeded = true
        let host = OwnerPromptHostingView(rootView: OwnerPromptRoot(model: model, controller: self))
        panel.contentView = host
        self.host = host
        window = panel
        NotificationCenter.default.addObserver(forName: NSApplication.didChangeScreenParametersNotification, object: nil,
                                               queue: .main) { [weak self] _ in
            Task { @MainActor in
                self?.model.notch = NotchGeometry.detect(NotchGeometry.targetScreen())
                self?.layout()
            }
        }
        return panel
    }
}

final class OwnerPromptPanel: NSPanel {
    /// Above the notch island (main menu + 2), so the question grows out of it.
    static let level = NSWindow.Level(rawValue: NSWindow.Level.mainMenu.rawValue + 3)
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

final class OwnerPromptHostingView: NSHostingView<OwnerPromptRoot> {
    /// The transparent room around the shape lets clicks through to what is beneath.
    var shapeInsets = NSEdgeInsetsZero

    override func hitTest(_ point: NSPoint) -> NSView? {
        let local = convert(point, from: superview)
        let shape = NSRect(x: shapeInsets.left, y: shapeInsets.bottom,
                           width: bounds.width - shapeInsets.left - shapeInsets.right,
                           height: bounds.height - shapeInsets.top - shapeInsets.bottom)
        return shape.contains(local) ? super.hitTest(point) : nil
    }

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
}

// MARK: - Views

/// The question window's colours, from the app-wide tokens: champagne for what the owner is
/// asked to do, amber for a question about to run out of time.
private enum PromptPalette {
    static let gold = VisualTokens.OnDark.brand
    static let goldInk = VisualTokens.onBrand
    static let attention = VisualTokens.OnDark.warning
}

private struct PromptHeightKey: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = max(value, nextValue()) }
}

struct OwnerPromptRoot: View {
    @ObservedObject var model: OwnerPromptModel
    let controller: OwnerPromptController
    /// With Reduce Motion the question only fades in: the island does not grow and the card does not slide.
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(spacing: 0) {
            if let prompt = model.current {
                switch model.style {
                case .island:
                    islandChrome(prompt)
                        .padding(.horizontal, OwnerPromptPlacement.margin)
                case .card:
                    cardChrome(prompt)
                        .padding(OwnerPromptPlacement.margin)
                }
            }
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .onPreferenceChange(PromptHeightKey.self) { controller.contentHeightChanged($0) }
    }

    private func measured<Content: View>(_ content: Content) -> some View {
        content.fixedSize(horizontal: false, vertical: true)
            .background(GeometryReader { Color.clear.preference(key: PromptHeightKey.self, value: $0.size.height) })
    }

    /// The island grown down from the notch: the same black silhouette, its mark and a ring in the wings.
    private func islandChrome(_ prompt: OwnerPrompt) -> some View {
        measured(
            VStack(spacing: 0) {
                HStack(spacing: 0) {
                    Image(nsImage: BrandArt.image("BrandMark")).resizable().interpolation(.high)
                        .frame(width: 20, height: 20).frame(width: 44)
                    Spacer(minLength: model.notch.width)
                    Circle().fill(PromptPalette.attention).frame(width: 8, height: 8)
                        .shadow(color: PromptPalette.attention.opacity(0.6), radius: 4).frame(width: 44)
                }
                .frame(height: model.notch.height)
                OwnerPromptContent(model: model, controller: controller, prompt: prompt, dark: true)
                    .padding(.horizontal, 22)
                    .padding(.top, 10)
                    .padding(.bottom, 16)
                    .opacity(model.appeared ? 1 : 0)
            }
            .frame(width: OwnerPromptPlacement.islandWidth)
            .background(
                IslandSilhouette(bottomRadius: model.appeared ? 30 : 10)
                    .fill(Color.black)
                    .shadow(color: .black.opacity(0.45), radius: 16, y: 6)
            )
        )
        .environment(\.colorScheme, .dark)
        .scaleEffect(x: model.appeared || reduceMotion ? 1 : 0.42, y: model.appeared || reduceMotion ? 1 : 0.18, anchor: .top)
        .opacity(reduceMotion && !model.appeared ? 0 : 1)
    }

    /// The card at the top right: glass, in the system's light or dark look.
    private func cardChrome(_ prompt: OwnerPrompt) -> some View {
        measured(
            OwnerPromptContent(model: model, controller: controller, prompt: prompt, dark: false)
                .padding(14)
                .frame(width: OwnerPromptPlacement.cardWidth)
                .background(VisualEffectBackground().clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous)))
                .overlay(RoundedRectangle(cornerRadius: 20, style: .continuous).strokeBorder(Color.primary.opacity(0.1)))
                .overlay(alignment: .top) {
                    LinearGradient(colors: [.clear, PromptPalette.gold.opacity(0.7), .clear], startPoint: .leading, endPoint: .trailing)
                        .frame(height: 1).padding(.horizontal, 24)
                }
                .shadow(color: .black.opacity(0.28), radius: 14, y: 6)
        )
        .offset(x: model.appeared || reduceMotion ? 0 : 16)
        .opacity(model.appeared ? 1 : 0)
    }
}

private struct VisualEffectBackground: NSViewRepresentable {
    func makeNSView(context: Context) -> NSVisualEffectView {
        let view = NSVisualEffectView()
        view.material = .popover
        view.blendingMode = .behindWindow
        view.state = .active
        return view
    }

    func updateNSView(_ view: NSVisualEffectView, context: Context) {}
}

private struct OwnerPromptContent: View {
    @ObservedObject var model: OwnerPromptModel
    let controller: OwnerPromptController
    let prompt: OwnerPrompt
    /// The island is always black; the card follows the system look.
    let dark: Bool
    @FocusState private var fieldFocused: Bool
    @MacMCPState private var dropTargeted = false

    private var primary: Color { dark ? .white : .primary }
    private var secondary: Color { dark ? .white.opacity(0.55) : .secondary }
    private var fieldFill: Color { dark ? Color(white: 0.09) : Color.primary.opacity(0.05) }
    private var fieldStroke: Color { dark ? Color(white: 0.17) : Color.primary.opacity(0.12) }
    private var canSend: Bool {
        !model.sending && (!model.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !model.attachments.isEmpty)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            header
            question
            if prompt.isConfirmation {
                decisionButtons
            } else {
                if !prompt.suggestions.isEmpty { suggestions }
                if !model.attachments.isEmpty { attachmentList }
                input
            }
            if let problem = model.problem {
                Text(problem).font(.system(size: 11.5)).foregroundStyle(Color(red: 1, green: 0.42, blue: 0.42))
            }
            footer
        }
        .onDrop(of: [UTType.fileURL, UTType.image], isTargeted: $dropTargeted) { controller.handleDrop($0) }
        .overlay {
            if dropTargeted && prompt.allowAttachments {
                RoundedRectangle(cornerRadius: 16, style: .continuous)
                    .strokeBorder(PromptPalette.gold, style: StrokeStyle(lineWidth: 1.5, dash: [5, 4]))
                    .padding(-6)
            }
        }
        .onExitCommand { controller.skip() }
    }

    private var header: some View {
        HStack(spacing: 10) {
            avatar
            VStack(alignment: .leading, spacing: 1) {
                Text(prompt.sender).font(.system(size: 13, weight: .semibold)).foregroundStyle(primary).lineLimit(1)
                Text(waitingLine).font(.system(size: 11.5)).foregroundStyle(secondary).lineLimit(1)
            }
            Spacer(minLength: 8)
            CountdownRing(prompt: prompt, now: model.now, dark: dark)
        }
    }

    private var footerHint: String {
        if prompt.isConfirmation { return "Nothing happens unless you choose “\(prompt.confirmLabel)”" }
        return prompt.allowAttachments ? "Return to send · ⌥Return for a new line · drop files to attach"
                                       : "Return to send · ⌥Return for a new line"
    }

    private var waitingLine: String {
        let more = model.queue.count - 1
        let subtitle = controller.subtitle(for: prompt)
        return more > 0 ? "\(subtitle) · \(more) more waiting" : subtitle
    }

    @ViewBuilder private var avatar: some View {
        if let logo = controller.logo(for: prompt) {
            Image(nsImage: logo).resizable().interpolation(.high).frame(width: 28, height: 28)
                .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
        } else {
            RoundedRectangle(cornerRadius: 8, style: .continuous).fill(Color(white: 0.12))
                .frame(width: 28, height: 28)
                .overlay(Image(nsImage: BrandArt.image("BrandMark")).resizable().interpolation(.high).padding(5))
        }
    }

    @ViewBuilder private var question: some View {
        let text = Text(prompt.question)
            .font(.system(size: dark ? 16 : 14.5, weight: .regular))
            .foregroundStyle(primary)
            .lineSpacing(2)
            .textSelection(.enabled)
            .frame(maxWidth: .infinity, alignment: .leading)
        if prompt.question.count > 420 {
            ScrollView { text }.frame(height: 190)
        } else {
            text.fixedSize(horizontal: false, vertical: true)
        }
    }

    private var suggestions: some View {
        PromptFlow(spacing: 7) {
            ForEach(prompt.suggestions, id: \.self) { suggestion in
                Button { controller.send(suggestion) } label: {
                    Text(suggestion).font(.system(size: 12.5, weight: .medium)).foregroundStyle(primary)
                        .padding(.horizontal, 11).padding(.vertical, 5)
                        .background(Capsule().fill(fieldFill))
                        .overlay(Capsule().strokeBorder(fieldStroke))
                }
                .buttonStyle(.plain)
                .disabled(model.sending)
                .help("Send “\(suggestion)”")
            }
        }
    }

    /// Deny on the left, confirm on the right in gold; neither is pressed by Return, so a stray key never approves.
    private var decisionButtons: some View {
        HStack(spacing: 8) {
            Button { controller.decide(confirm: false) } label: {
                Text(prompt.denyLabel).font(.system(size: 13, weight: .semibold)).foregroundStyle(primary)
                    .lineLimit(1).frame(maxWidth: .infinity).padding(.vertical, 8)
                    .background(RoundedRectangle(cornerRadius: 11, style: .continuous).fill(fieldFill))
                    .overlay(RoundedRectangle(cornerRadius: 11, style: .continuous).strokeBorder(fieldStroke))
            }
            .buttonStyle(.plain)
            .disabled(model.sending)
            .help(prompt.denyLabel)
            Button { controller.decide(confirm: true) } label: {
                ZStack {
                    if model.sending {
                        ProgressView().controlSize(.small).scaleEffect(0.7)
                    } else {
                        Text(prompt.confirmLabel).font(.system(size: 13, weight: .semibold))
                            .foregroundStyle(PromptPalette.goldInk).lineLimit(1)
                    }
                }
                .frame(maxWidth: .infinity).padding(.vertical, 8)
                .background(RoundedRectangle(cornerRadius: 11, style: .continuous).fill(PromptPalette.gold))
            }
            .buttonStyle(.plain)
            .disabled(model.sending)
            .help(prompt.confirmLabel)
        }
    }

    private var attachmentList: some View {
        PromptFlow(spacing: 6) {
            ForEach(model.attachments) { attachment in
                HStack(spacing: 6) {
                    AttachmentThumbnail(attachment: attachment)
                    Text(attachment.name).font(.system(size: 11.5)).foregroundStyle(primary)
                        .lineLimit(1).truncationMode(.middle).frame(maxWidth: 150, alignment: .leading)
                    Button { controller.remove(attachment) } label: {
                        Image(systemName: "xmark").font(.system(size: 9, weight: .bold)).foregroundStyle(secondary)
                    }
                    .buttonStyle(.plain)
                    .help("Remove")
                }
                .padding(.leading, 4).padding(.trailing, 8).padding(.vertical, 4)
                .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(fieldFill))
                .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous).strokeBorder(fieldStroke))
            }
        }
    }

    private var input: some View {
        HStack(alignment: .bottom, spacing: 8) {
            TextField("Type your answer…", text: $model.draft, axis: .vertical)
                .textFieldStyle(.plain)
                .font(.system(size: 13.5))
                .foregroundStyle(primary)
                .lineLimit(1...6)
                .focused($fieldFocused)
                .onSubmit { controller.send() }
                .padding(.vertical, 6)
            if prompt.allowAttachments {
                Button { controller.chooseFiles() } label: {
                    Image(systemName: "paperclip").font(.system(size: 14, weight: .medium)).foregroundStyle(secondary)
                        .frame(width: 28, height: 28)
                }
                .buttonStyle(.plain)
                .help("Attach files or pictures (you can also drop them here)")
            }
            Button { controller.send() } label: {
                ZStack {
                    RoundedRectangle(cornerRadius: 9, style: .continuous)
                        .fill(canSend ? PromptPalette.gold : fieldStroke)
                    if model.sending {
                        ProgressView().controlSize(.small).scaleEffect(0.7)
                    } else {
                        Image(systemName: "arrow.right").font(.system(size: 13, weight: .bold))
                            .foregroundStyle(canSend ? PromptPalette.goldInk : secondary)
                    }
                }
                .frame(width: 30, height: 30)
            }
            .buttonStyle(.plain)
            .disabled(!canSend)
            .help("Send (Return)")
        }
        .padding(.leading, 12).padding(.trailing, 5).padding(.vertical, 4)
        .background(RoundedRectangle(cornerRadius: 13, style: .continuous).fill(fieldFill))
        .overlay(RoundedRectangle(cornerRadius: 13, style: .continuous)
            .strokeBorder(fieldFocused ? PromptPalette.gold.opacity(0.6) : fieldStroke))
        .contentShape(Rectangle())
        .onTapGesture { fieldFocused = true }
    }

    private var footer: some View {
        HStack(spacing: 6) {
            Text(footerHint)
                .font(.system(size: 11)).foregroundStyle(secondary.opacity(0.85)).lineLimit(1)
            Spacer(minLength: 6)
            Button("Skip") { controller.skip() }
                .buttonStyle(.plain)
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(secondary)
                .help("Skip this question (Esc)")
        }
    }
}

private struct AttachmentThumbnail: View {
    let attachment: OwnerPromptAttachment

    var body: some View {
        let image = attachment.isImage ? NSImage(contentsOf: attachment.url) : nil
        Group {
            if let image {
                Image(nsImage: image).resizable().aspectRatio(contentMode: .fill)
            } else {
                Image(nsImage: NSWorkspace.shared.icon(forFile: attachment.url.path)).resizable()
            }
        }
        .frame(width: 22, height: 22)
        .clipShape(RoundedRectangle(cornerRadius: 5, style: .continuous))
    }
}

/// Time left, as a gold ring that empties and the seconds inside it.
private struct CountdownRing: View {
    let prompt: OwnerPrompt
    let now: Date
    let dark: Bool

    var body: some View {
        let remaining = max(0, Int(prompt.expiresAt.timeIntervalSince(now).rounded()))
        let fraction = min(1, Double(remaining) / Double(prompt.timeoutSeconds))
        ZStack {
            Circle().stroke(dark ? Color.white.opacity(0.14) : Color.primary.opacity(0.12), lineWidth: 2.5)
            Circle().trim(from: 0, to: fraction)
                .stroke(remaining <= 10 ? PromptPalette.attention : PromptPalette.gold,
                        style: StrokeStyle(lineWidth: 2.5, lineCap: .round))
                .rotationEffect(.degrees(-90))
                .animation(.linear(duration: 1), value: fraction)
            Text(OwnerPromptPlacement.remainingText(remaining))
                .font(.system(size: remaining >= 60 ? 8 : 9.5, weight: .semibold, design: .monospaced))
                .foregroundStyle(dark ? Color.white.opacity(0.75) : Color.secondary)
        }
        .frame(width: 30, height: 30)
        .accessibilityLabel("\(remaining) seconds left")
    }
}

/// Chips that wrap onto the next line (also the provider bench in Settings › Agents › Teams).
struct PromptFlow: Layout {
    var spacing: CGFloat = 6

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let rows = arrange(width: proposal.width ?? .infinity, subviews: subviews)
        let height = rows.last.map { $0.y + $0.height } ?? 0
        let width = rows.map(\.width).max() ?? 0
        return CGSize(width: proposal.width ?? width, height: height)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        for row in arrange(width: bounds.width, subviews: subviews) {
            var x = bounds.minX
            for index in row.indices {
                let size = subviews[index].sizeThatFits(.unspecified)
                subviews[index].place(at: CGPoint(x: x, y: bounds.minY + row.y), proposal: ProposedViewSize(size))
                x += size.width + spacing
            }
        }
    }

    private struct Row { var indices: [Int] = []; var y: CGFloat = 0; var width: CGFloat = 0; var height: CGFloat = 0 }

    private func arrange(width: CGFloat, subviews: Subviews) -> [Row] {
        var rows: [Row] = [Row()]
        for index in subviews.indices {
            let size = subviews[index].sizeThatFits(.unspecified)
            if !rows[rows.count - 1].indices.isEmpty, rows[rows.count - 1].width + spacing + size.width > width {
                let last = rows[rows.count - 1]
                rows.append(Row(y: last.y + last.height + spacing))
            }
            var row = rows[rows.count - 1]
            row.width += (row.indices.isEmpty ? 0 : spacing) + size.width
            row.height = max(row.height, size.height)
            row.indices.append(index)
            rows[rows.count - 1] = row
        }
        return rows.filter { !$0.indices.isEmpty }
    }
}
