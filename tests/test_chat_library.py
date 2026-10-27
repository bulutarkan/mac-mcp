"""Mac MCP Chat's list and review (#122).

Archived chats leave the list and come back; pinned ones sit on top; ⌘K switches between recent chats; a diff's
file opens; a model shows its provider; an agent that is not ready cannot start a chat; message actions show on
keyboard focus; Return and Option-Return are named in the message box.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "menu_app" / "Sources"
LIBRARY = (SOURCES / "ChatLibrary.swift").read_text(encoding="utf-8")
WINDOW = (SOURCES / "ChatWindow.swift").read_text(encoding="utf-8")
SHELL = (SOURCES / "ChatShell.swift").read_text(encoding="utf-8")
TRANSCRIPT = (SOURCES / "ChatTranscript.swift").read_text(encoding="utf-8")

HARNESS = r'''
import Foundation

struct Row: ChatListed {
    let id: String
    let title: String
    let projectName: String
    let updatedAt: Double
    var pinnedAt: Double = 0
    var archivedAt: Double = 0
}

@main
struct ChatLibraryHarness {
    static func main() {
        let chats = [
            Row(id: "a", title: "Launch plan", projectName: "site", updatedAt: 50),
            Row(id: "b", title: "Fix login", projectName: "app", updatedAt: 90, pinnedAt: 20),
            Row(id: "c", title: "Old notes", projectName: "site", updatedAt: 99, archivedAt: 10),
            Row(id: "d", title: "Tests", projectName: "app", updatedAt: 70, pinnedAt: 5),
            Row(id: "e", title: "Pinned but archived", projectName: "app", updatedAt: 10, pinnedAt: 1, archivedAt: 30),
        ]
        // Pinned on top in pin order, archived out of the list (also when pinned), the rest in their projects.
        precondition(ChatLibrary.pinned(chats).map(\.id) == ["d", "b"])
        precondition(ChatLibrary.archived(chats).map(\.id) == ["e", "c"], "most recently archived first")
        precondition(ChatLibrary.listed(chats).map(\.id) == ["a"])
        precondition(ChatLibrary.listed(chats, search: "launch SITE").map(\.id) == ["a"])
        precondition(ChatLibrary.pinned(chats, search: "login").map(\.id) == ["b"])
        precondition(ChatLibrary.archived(chats, search: "notes").map(\.id) == ["c"])

        // The quick switch: newest first, the open chat left out, archived ones only when searched for, last.
        precondition(ChatLibrary.recent(chats, search: "", current: "b").map(\.id) == ["d", "a"])
        precondition(ChatLibrary.recent(chats, search: "site", current: nil).map(\.id) == ["a", "c"])
        precondition(ChatLibrary.recent(chats, search: "", current: nil, limit: 1).map(\.id) == ["b"])
        precondition(ChatLibrary.move(0, by: -1, count: 3) == 2 && ChatLibrary.move(2, by: 1, count: 3) == 0)
        precondition(ChatLibrary.move(0, by: 1, count: 0) == 0)

        // Archiving the open chat opens the next one, else the one before, else none.
        precondition(ChatLibrary.next(after: "b", in: ["a", "b", "c"]) == "c")
        precondition(ChatLibrary.next(after: "c", in: ["a", "b", "c"]) == "b")
        precondition(ChatLibrary.next(after: "a", in: ["a"]) == nil)

        // A diff's file: absolute as it is, relative inside the project.
        precondition(ChatLibrary.filePath("/p/x.swift", project: "/q") == "/p/x.swift")
        precondition(ChatLibrary.filePath("src/x.swift", project: "/q") == "/q/src/x.swift")
        precondition(ChatLibrary.filePath("x.swift", project: "") == "x.swift")

        // A model's provider: the agent's group, else the id's prefix, else nothing.
        precondition(ChatLibrary.provider(group: "Anthropic", value: "opus") == "Anthropic")
        precondition(ChatLibrary.provider(group: "", value: "openrouter/qwen/qwen3") == "openrouter")
        precondition(ChatLibrary.provider(group: "", value: "gpt-5") == "")
        precondition(ChatLibrary.provider(group: "", value: "/odd") == "")

        precondition(ChatLibrary.sendHint(running: false) == "↩ send · ⌥↩ new line")
        precondition(ChatLibrary.sendHint(running: true).contains("⌥↩ new line"))
        print("CHAT_LIBRARY_PASS")
    }
}
'''


class ArchivePinSwitchSourceTests(unittest.TestCase):
    def test_build_compiles_the_library_before_the_window(self) -> None:
        script = (ROOT / "menu_app" / "build_app.sh").read_text(encoding="utf-8")
        library = '"${SCRIPT_DIR}/Sources/ChatLibrary.swift"'
        self.assertIn(library, script)
        self.assertLess(script.index(library), script.index('"${SCRIPT_DIR}/Sources/ChatWindow.swift"'))

    def test_the_server_keeps_archive_and_pin(self) -> None:
        self.assertIn('pinnedAt = (object["pinned_at"] as? NSNumber)?.doubleValue ?? 0', WINDOW)
        self.assertIn('archivedAt = (object["archived_at"] as? NSNumber)?.doubleValue ?? 0', WINDOW)
        self.assertIn('api("dashboard/api/chats/\\(chatID)/organize", change)', WINDOW)
        self.assertIn('organize(chat.id, ["archived": true])', WINDOW)
        self.assertIn('organize(chatID, ["archived": false])', WINDOW)

    def test_the_sidebar_offers_pin_archive_undo_and_restore(self) -> None:
        for copy in ('Button(chat.pinned ? "Unpin" : "Pin")', 'Button("Archive") { model.archive(chat) }',
                     'Button("Undo") { model.unarchive(archived.id) }', 'Button("Unarchive") { model.unarchive(chat.id) }',
                     'SectionTitle(symbol: "pin.fill", title: "Pinned")', 'Text("Archived")'):
            self.assertIn(copy, SHELL)
        self.assertIn("ChatLibrary.listed(project.chats, search: search)", WINDOW)

    def test_command_k_opens_the_switcher(self) -> None:
        self.assertIn('Button("") { switching = true }.keyboardShortcut("k", modifiers: .command)', SHELL)
        self.assertIn(".sheet(isPresented: $switching) { ChatSwitcher(model: model) }", SHELL)
        switcher = SHELL[SHELL.index("private struct ChatSwitcher"):SHELL.index("// MARK: Composer")]
        self.assertIn("NSEvent.removeMonitor(keys)", switcher)
        self.assertIn(".keyboardShortcut(.cancelAction)", switcher)

    def test_no_endless_swiftui_animation(self) -> None:
        for source in (LIBRARY, WINDOW, SHELL, TRANSCRIPT):
            self.assertNotIn(".repeatForever", source)


class ReviewSourceTests(unittest.TestCase):
    def test_a_diffs_file_name_opens_the_file_and_the_whole_diff(self) -> None:
        block = TRANSCRIPT[TRANSCRIPT.index("private struct DiffBlock"):TRANSCRIPT.index("private struct DiffLines")]
        self.assertIn("Button(action: openFile)", block)
        self.assertIn("NSWorkspace.shared.open(URL(fileURLWithPath: path))", block)
        self.assertIn("ChatLibrary.filePath(diff.path, project: project)", block)
        self.assertIn("DiffSheet(diff: diff, path: path, missing: missing, open: openFile)", block)
        self.assertIn('.environment(\\.chatProject, model.chat?.project ?? "")', TRANSCRIPT)

    def test_not_ready_agents_cannot_start_a_chat(self) -> None:
        sheet = WINDOW[WINDOW.index("struct NewChatSheet"):]
        self.assertIn(".disabled(folder == nil || selectedAgent?.ready != true || starting)", sheet)
        self.assertIn('"\\(option.label) (not ready)"', sheet)
        self.assertIn("option.reason", sheet)

    def test_models_show_their_provider(self) -> None:
        picker = WINDOW[WINDOW.index("private struct SearchablePicker"):WINDOW.index("private enum ConfigChip")]
        self.assertIn("ChatLibrary.provider(group: choice.group, value: choice.value)", picker)
        self.assertIn("ForEach(sections, id: \\.name)", picker)

    def test_message_actions_show_on_keyboard_focus(self) -> None:
        message = TRANSCRIPT[TRANSCRIPT.index("private struct UserMessage"):TRANSCRIPT.index("private struct StreamingAnswer")]
        self.assertIn(".opacity(hovering || focusedAction != nil ? 1 : 0)", message)
        self.assertIn(".focused($focusedAction, equals: .edit)", message)
        self.assertIn(".focused($focusedAction, equals: .copy)", message)

    def test_the_message_box_names_return_and_option_return(self) -> None:
        self.assertIn("ChatLibrary.sendHint(running: running)", SHELL)


@unittest.skipUnless(shutil.which("xcrun"), "Swift toolchain is not installed")
class ChatLibraryHarnessTests(unittest.TestCase):
    def test_rules_behave(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            harness = Path(td) / "ChatLibraryHarness.swift"
            binary = Path(td) / "chat-library"
            harness.write_text(textwrap.dedent(HARNESS), encoding="utf-8")
            built = subprocess.run(
                ["xcrun", "swiftc", "-parse-as-library", str(SOURCES / "ChatLibrary.swift"), str(harness),
                 "-o", str(binary)],
                cwd=ROOT, capture_output=True, text=True, timeout=180,
            )
            self.assertEqual(0, built.returncode, built.stderr)
            run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
            self.assertEqual(0, run.returncode, run.stderr)
            self.assertIn("CHAT_LIBRARY_PASS", run.stdout)


if __name__ == "__main__":
    unittest.main()
