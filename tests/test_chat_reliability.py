"""Mac MCP Chat when things go wrong (#120).

A permission question is never answered by a stray Return; two images pasted in the same second stay two
files; a failed send never overwrites newer words; a dropped connection shows as "Reconnecting…" / "Offline"
with Retry, from real stream and poll outcomes; an image that goes as a file link says so on the attachment.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from mcp_server import chat_sessions

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "menu_app" / "Sources"
WINDOW = (SOURCES / "ChatWindow.swift").read_text(encoding="utf-8")
SHELL = (SOURCES / "ChatShell.swift").read_text(encoding="utf-8")
TRANSCRIPT = (SOURCES / "ChatTranscript.swift").read_text(encoding="utf-8")
RELIABILITY = (SOURCES / "ChatReliability.swift").read_text(encoding="utf-8")

HARNESS = r'''
import Foundation

@main
struct ChatReliabilityHarness {
    static func main() throws {
        // The stream is the connection: it drops, the app says so, and only the stream opening makes it live.
        var link = ChatLink()
        precondition(link.state == .live)
        link.polled(ok: false, streaming: true)
        precondition(link.state == .live, "a failed poll while the stream is up changes nothing")
        link.dropped()
        precondition(link.state == .reconnecting && link.failures == 1)
        link.polled(ok: true, streaming: true)
        precondition(link.state == .reconnecting, "a good poll is not the stream back")
        link.polled(ok: false, streaming: true)
        link.dropped()
        link.polled(ok: false, streaming: true)
        precondition(link.state == .reconnecting && link.failures == 2)
        link.dropped()
        precondition(link.state == .reconnecting && link.failures == 3)
        link.polled(ok: false, streaming: true)
        precondition(link.state == .offline, "three tries in a row with no answer is offline")
        link.dropped()
        precondition(link.state == .offline)
        link.polled(ok: true, streaming: true)
        precondition(link.state == .reconnecting, "Mac MCP answers again")
        link.opened()
        precondition(link.state == .live && link.failures == 0)

        link.dropped(); link.dropped(); link.dropped(); link.polled(ok: false, streaming: true)
        precondition(link.state == .offline)
        link.retrying()
        precondition(link.state == .reconnecting && link.failures == 0, "Retry tries at once, counting afresh")

        // Without a stream, polls are the connection.
        var polls = ChatLink()
        polls.polled(ok: false, streaming: false)
        precondition(polls.state == .reconnecting)
        polls.polled(ok: false, streaming: false)
        polls.polled(ok: false, streaming: false)
        precondition(polls.state == .offline)
        polls.polled(ok: true, streaming: false)
        precondition(polls.state == .live && polls.failures == 0)

        // A failed send goes back only into its own chat's empty message box.
        precondition(ChatDraftRestore.fits(failedChat: "a", openChat: "a", draft: "", attachments: 0))
        precondition(ChatDraftRestore.fits(failedChat: "a", openChat: "a", draft: "  \n", attachments: 0))
        precondition(!ChatDraftRestore.fits(failedChat: "a", openChat: "a", draft: "newer words", attachments: 0))
        precondition(!ChatDraftRestore.fits(failedChat: "a", openChat: "a", draft: "", attachments: 1))
        precondition(!ChatDraftRestore.fits(failedChat: "a", openChat: "b", draft: "", attachments: 0))
        precondition(!ChatDraftRestore.fits(failedChat: "a", openChat: nil, draft: "", attachments: 0))

        // Two pastes in the same second: two files, the first one untouched.
        let folder = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
        let stamp = "2026-10-26 at 14.03.22"
        let first = try ChatPaste.save(Data("first".utf8), in: folder, stamp: stamp)
        let second = try ChatPaste.save(Data("second".utf8), in: folder, stamp: stamp)
        let third = try ChatPaste.save(Data("third".utf8), in: folder, stamp: stamp)
        precondition(first.lastPathComponent == "Pasted image 2026-10-26 at 14.03.22.png")
        precondition(second.lastPathComponent == "Pasted image 2026-10-26 at 14.03.22 2.png")
        precondition(third.lastPathComponent == "Pasted image 2026-10-26 at 14.03.22 3.png")
        let firstData = try Data(contentsOf: first), secondData = try Data(contentsOf: second)
        precondition(firstData == Data("first".utf8))
        precondition(secondData == Data("second".utf8))

        // An image that goes as a file link says why; a picture that goes as one says nothing.
        let mb = 1024 * 1024
        precondition(ChatImageNote.note(fileExtension: "png", size: mb, takesImages: true, agent: "Codex") == nil)
        precondition(ChatImageNote.note(fileExtension: "PNG", size: mb, takesImages: nil, agent: "Codex") == nil)
        precondition(ChatImageNote.note(fileExtension: "txt", size: 99 * mb, takesImages: false, agent: "Codex") == nil)
        precondition(ChatImageNote.note(fileExtension: "png", size: 9 * mb, takesImages: true, agent: "Codex")
                     == "Goes as a file link: over 8 MB")
        precondition(ChatImageNote.note(fileExtension: "jpg", size: 8 * mb, takesImages: true, agent: "Codex") == nil)
        precondition(ChatImageNote.note(fileExtension: "heic", size: mb, takesImages: true, agent: "Codex")
                     == "Goes as a file link: agents take PNG, JPEG, GIF or WebP")
        precondition(ChatImageNote.note(fileExtension: "gif", size: mb, takesImages: false, agent: "Copilot")
                     == "Goes as a file link: Copilot doesn't take pictures")
        print("CHAT_RELIABILITY_PASS")
    }
}
'''


class PermissionKeysTests(unittest.TestCase):
    def card(self) -> str:
        return WINDOW[WINDOW.index("struct PermissionCard"):WINDOW.index("struct PlanCard")]

    def test_return_answers_nothing(self) -> None:
        card = self.card()
        self.assertNotIn(".defaultAction", card)
        self.assertNotIn(".keyboardShortcut(.return", card)

    def test_each_choice_has_its_own_command_number(self) -> None:
        card = self.card()
        self.assertIn("KeyboardShortcut(KeyEquivalent(Character(\"\\(number)\")), modifiers: .command)", card)
        self.assertIn("shortcuts && number <= 9", card)
        self.assertIn(".keyboardShortcut(key)", card)
        self.assertIn('Text("⌘\\(number)")', card)
        self.assertIn("choice(option, number: index + 1)", card)
        # Only the first question still waiting in the live turn takes the keys, so two never share one.
        self.assertIn("shortcuts: live && ask.id == turn.asks.first?.id", TRANSCRIPT)
        self.assertIn('PermissionCard(item: step, agent: "", answer: { _ in })', TRANSCRIPT)


class FilePickerResponsivenessTests(unittest.TestCase):
    def test_chat_pickers_do_not_hold_swiftui_accessibility_action_in_a_modal_loop(self) -> None:
        for source, name in ((SHELL, "chooseFiles"), (WINDOW, "chooseFolder")):
            begin = source.index(f"private func {name}()")
            end = source.index("\n    }", begin)
            picker = source[begin:end]
            self.assertNotIn(".runModal()", picker,
                             "runModal holds SwiftUI's AX press/update until the picker closes")
            self.assertIn("DispatchQueue.main.async", picker,
                          "present after SwiftUI's action/update has unwound")
            self.assertIn("panel.begin {", picker)
            self.assertIn("response == .OK", picker)


class FailedSendTests(unittest.TestCase):
    def submit(self) -> str:
        return WINDOW[WINDOW.index("private func submit("):WINDOW.index("func resend(")]

    def test_failed_text_goes_back_only_into_its_own_empty_box(self) -> None:
        submit = self.submit()
        self.assertIn("ChatDraftRestore.fits(failedChat: id, openChat: selectedID, draft: draft, "
                      "attachments: attachments.count)", submit)
        self.assertIn("failedSends.append(ChatFailedSend(chatID: id, text: text, files: files, reason: message))", submit)
        # Another chat's state is never touched by this one's failure.
        self.assertRegex(submit, r"if id == selectedID \{\s*error = message\s*if !wasRunning \{ chat\?\.running = false \}")
        send = WINDOW[WINDOW.index("    func send() {"):WINDOW.index("private func submit(")]
        self.assertNotIn("draft = text", send)

    def test_ask_again_leaves_the_message_box_alone(self) -> None:
        retry = WINDOW[WINDOW.index("    func retry(_ text: String)"):WINDOW.index("    func attach(")]
        self.assertIn("submit(text, [], to: id)", retry)
        self.assertNotIn("draft =", retry)

    def test_the_composer_keeps_failed_messages_to_send_again_or_copy(self) -> None:
        self.assertIn("model.failedSends.filter { $0.chatID == model.selectedID }", SHELL)
        failed = SHELL[SHELL.index("struct FailedMessage"):SHELL.index("struct ConnectionNotice")]
        for copy in ('Text("Not sent:")', 'Text("Try Again")', 'Text("Copy")', '.help("Discard")'):
            self.assertIn(copy, failed)
        self.assertIn("NSPasteboard.general.setString(failed.text, forType: .string)", failed)


class ConnectionStateTests(unittest.TestCase):
    def test_stream_and_polls_move_the_state(self) -> None:
        follow = WINDOW[WINDOW.index("private func follow(_ id: String)"):WINDOW.index("private func pollLoop()")]
        self.assertIn("moveLink { $0.opened() }", follow)
        self.assertIn("moveLink { $0.dropped() }", follow)
        self.assertIn("min(link.failures, 5)", follow)
        poll = WINDOW[WINDOW.index("    func poll() async"):WINDOW.index("private func apply(")]
        self.assertIn('let answered = result.ok || result.object["error"] != nil', poll)
        self.assertIn("moveLink { $0.polled(ok: answered, streaming: eventsRequest != nil) }", poll)
        self.assertIn("if next != link { link = next }", WINDOW)
        retry = WINDOW[WINDOW.index("func retryConnection()"):WINDOW.index("private func follow(")]
        self.assertIn("moveLink { $0.retrying() }", retry)
        self.assertIn("poller?.cancel()", retry)
        self.assertIsNotNone(re.search(r"poller\?\.cancel\(\)\s*link = ChatLink\(\)", WINDOW), "a new chat starts live")

    def test_the_composer_shows_reconnecting_and_offline_with_retry(self) -> None:
        self.assertIn("if model.link.state != .live {", SHELL)
        self.assertIn("ConnectionNotice(state: model.link.state, retry: model.retryConnection)", SHELL)
        notice = SHELL[SHELL.index("struct ConnectionNotice"):]
        notice = notice[:notice.index("\n}\n")]
        for copy in ('Text("Offline")', 'Text("Mac MCP isn\'t answering.")', 'Button("Retry", action: retry)',
                     'Text("Reconnecting…")'):
            self.assertIn(copy, notice)
        self.assertNotIn("ProgressView", notice)

    def test_no_endless_swiftui_animation_in_the_chat(self) -> None:
        for source in (WINDOW, SHELL, TRANSCRIPT, RELIABILITY):
            self.assertNotIn(".repeatForever", source)


class ImageNoteTests(unittest.TestCase):
    def test_the_app_decides_like_the_server(self) -> None:
        types = re.search(r"static let pictureTypes: Set<String> = \[([^\]]*)\]", RELIABILITY).group(1)
        swift_types = {value.strip().strip('"') for value in types.split(",")}
        self.assertEqual(swift_types, {suffix.lstrip(".") for suffix in chat_sessions.IMAGE_TYPES})
        self.assertIn("static let pictureLimit = 8 * 1024 * 1024", RELIABILITY)
        self.assertEqual(chat_sessions.MAX_IMAGE_BYTES, 8 * 1024 * 1024)
        self.assertIn("ChatImageNote.note(fileExtension: url.pathExtension, size: size, "
                      "takesImages: model.chat?.takesImages,", SHELL)
        self.assertIn("PendingAttachment(url: url, note: imageNote(url))", SHELL)
        self.assertIn('takesImages = object["takes_images"] as? Bool', WINDOW)

    def test_pasted_images_never_overwrite_each_other(self) -> None:
        paste = WINDOW[WINDOW.index("func pasteFiles()"):WINDOW.index("func detach(")]
        self.assertIn("file = try ChatPaste.save(png, in: folder, stamp: stamp)", paste)
        self.assertNotIn(".atomic", paste)
        self.assertIn("try data.write(to: file, options: .withoutOverwriting)", RELIABILITY)

    def test_build_compiles_the_rules_before_the_window(self) -> None:
        script = (ROOT / "menu_app" / "build_app.sh").read_text(encoding="utf-8")
        rules = '"${SCRIPT_DIR}/Sources/ChatReliability.swift"'
        self.assertIn(rules, script)
        self.assertLess(script.index(rules), script.index('"${SCRIPT_DIR}/Sources/ChatWindow.swift"'))


@unittest.skipUnless(shutil.which("xcrun"), "Swift toolchain is not installed")
class ChatReliabilityHarnessTests(unittest.TestCase):
    def test_rules_behave(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            harness = Path(td) / "ChatReliabilityHarness.swift"
            binary = Path(td) / "chat-reliability"
            pasted = Path(td) / "pasted"
            pasted.mkdir()
            harness.write_text(textwrap.dedent(HARNESS), encoding="utf-8")
            built = subprocess.run(
                ["xcrun", "swiftc", "-parse-as-library", str(SOURCES / "ChatReliability.swift"), str(harness),
                 "-o", str(binary)],
                cwd=ROOT, capture_output=True, text=True, timeout=180,
            )
            self.assertEqual(0, built.returncode, built.stderr)
            run = subprocess.run([str(binary), str(pasted)], capture_output=True, text=True, timeout=20)
            self.assertEqual(0, run.returncode, run.stderr)
            self.assertIn("CHAT_RELIABILITY_PASS", run.stdout)
            self.assertEqual(3, len(list(pasted.iterdir())))


if __name__ == "__main__":
    unittest.main()
