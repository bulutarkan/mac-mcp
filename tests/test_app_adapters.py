from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp_server.app_adapters import (
    _RS,
    _US,
    mac_app,
    normalize_app_name,
    supported_actions,
)
from mcp_server.computer_plan import execute_computer_plan
from mcp_server.policy import evaluate_profile, resolve_risk


SETTINGS = SimpleNamespace()


class AdapterRegistryTests(unittest.TestCase):
    def test_six_first_party_apps_and_aliases(self) -> None:
        self.assertEqual("Finder", normalize_app_name("finder"))
        self.assertEqual("Notes", normalize_app_name("NOTES"))
        self.assertEqual("Mail", normalize_app_name("mail"))
        self.assertEqual("Calendar", normalize_app_name("calendar"))
        self.assertEqual("Preview", normalize_app_name("preview"))
        self.assertEqual("System Settings", normalize_app_name("settings"))
        self.assertEqual("System Settings", normalize_app_name("System Preferences"))

    def test_capabilities_are_semantic_and_keep_generic_fallback(self) -> None:
        for app in ("Finder", "Notes", "Mail", "Calendar", "Preview", "System Settings"):
            with self.subTest(app=app):
                result = mac_app(SETTINGS, app=app, action="capabilities")
                self.assertTrue(result["ok"])
                self.assertTrue(result["supported"])
                self.assertEqual(list(supported_actions(app)), result["actions"])
                self.assertEqual("mac_observe", result["generic_fallback"]["observe"])
                self.assertEqual("mac_act", result["generic_fallback"]["act"])
                self.assertFalse(result["generic_fallback"]["automatic"])

    def test_unknown_app_returns_explicit_non_automatic_ax_fallback(self) -> None:
        result = mac_app(SETTINGS, app="TextEdit", action="open_document")
        self.assertFalse(result["ok"])
        self.assertFalse(result["supported"])
        self.assertEqual("APP_ADAPTER_UNSUPPORTED", result["reason_code"])
        self.assertEqual("mac_observe", result["fallback"]["observe"]["tool"])
        self.assertEqual("mac_act", result["fallback"]["act_tool"])
        self.assertFalse(result["fallback"]["automatic"])

    def test_unknown_action_on_known_app_returns_same_safe_fallback(self) -> None:
        result = mac_app(SETTINGS, app="Finder", action="delete_everything")
        self.assertFalse(result["ok"])
        self.assertFalse(result["supported"])
        self.assertEqual("APP_ADAPTER_UNSUPPORTED", result["reason_code"])
        self.assertFalse(result["fallback"]["automatic"])

    def test_dynamic_policy_is_read_only_for_reads_and_full_for_ui_actions(self) -> None:
        _, read = resolve_risk("mac_app", {"app": "Notes", "action": "find_notes"})
        _, action = resolve_risk("mac_app", {"app": "Notes", "action": "open_note"})
        self.assertEqual({"read", "native_accessibility"}, {x.value for x in read.capabilities})
        self.assertEqual("read_only", read.requested_access_mode.value)
        self.assertIn("ui_action", {x.value for x in action.capabilities})
        self.assertIn("process_control", {x.value for x in action.capabilities})
        self.assertIsNone(action.requested_access_mode)
        self.assertTrue(evaluate_profile("standard", action).allowed)
        self.assertFalse(evaluate_profile("read_only", action).allowed)
        self.assertFalse(action.destructive)

    def test_computer_plan_accepts_mac_app_as_nested_step(self) -> None:
        async def run() -> None:
            calls = []

            async def caller(tool, arguments):
                calls.append((tool, arguments))
                return {"ok": True, "app": arguments["app"], "action": arguments["action"]}

            result = await execute_computer_plan(
                caller,
                steps=[
                    {
                        "id": "semantic",
                        "tool": "mac_app",
                        "arguments": {"app": "Finder", "action": "selection"},
                    }
                ],
            )
            self.assertTrue(result["ok"])
            self.assertEqual("mac_app", calls[0][0])

        asyncio.run(run())


class FinderAdapterTests(unittest.TestCase):
    def test_selection_reads_ax_selected_rows(self) -> None:
        raw = f"OK{_US}/tmp/{_US}example.txt{_RS}second.txt"
        with patch("mcp_server.app_adapters._run", return_value=raw):
            result = mac_app(SETTINGS, app="Finder", action="selection", limit=5)
        self.assertTrue(result["ok"])
        self.assertEqual([str((Path("/tmp") / "example.txt").resolve()), str((Path("/tmp") / "second.txt").resolve())], result["selected_paths"])
        self.assertEqual(1, result["semantic_tool_calls"])

    def test_selection_unsupported_view_returns_safe_fallback(self) -> None:
        with patch("mcp_server.app_adapters._run", return_value=f"UNSUPPORTED_VIEW{_US}icon view{_US}/tmp/"):
            result = mac_app(SETTINGS, app="Finder", action="selection")
        self.assertFalse(result["ok"])
        self.assertEqual("FINDER_STATE_UNSUPPORTED", result["reason_code"])
        self.assertFalse(result["fallback"]["automatic"])

    def test_select_file_uses_dedicated_window_and_axselected_verification(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "fixture.txt"
            path.write_text("fixture")
            calls = []

            def fake_run(script, **kwargs):
                calls.append(script)
                if "make new Finder window" in script:
                    return f"1656{_US}{path.parent.resolve()}"
                return f"OK{_US}{path.parent.resolve()}"

            with patch("mcp_server.app_adapters._run", side_effect=fake_run),                  patch("mcp_server.app_adapters._focus_begin", return_value=None),                  patch("mcp_server.app_adapters._focus_finish", return_value={"status": "preserved"}):
                result = mac_app(
                    SETTINGS, app="Finder", action="select_file",
                    path=str(path), preserve_focus=True,
                )
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        self.assertEqual("AXSelected", result["verification"])
        self.assertEqual(1656, result["finder_window_id"])
        self.assertIn("make new Finder window", calls[0])
        self.assertIn('value of attribute "AXSelected"', calls[1])
        self.assertEqual(1, result["semantic_tool_calls"])

    def test_select_file_window_change_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "fixture.txt"
            path.write_text("fixture")
            with patch(
                "mcp_server.app_adapters._run",
                side_effect=[f"1656{_US}{path.parent.resolve()}", "WINDOW_CHANGED"],
            ), patch("mcp_server.app_adapters._focus_begin", return_value=None):
                result = mac_app(SETTINGS, app="Finder", action="select_file", path=str(path))
        self.assertFalse(result["ok"])
        self.assertEqual("FINDER_WINDOW_CHANGED", result["reason_code"])
        self.assertFalse(result["fallback"]["automatic"])

    def test_select_file_missing_path_fails_closed(self) -> None:
        result = mac_app(
            SETTINGS, app="Finder", action="select_file",
            path="/tmp/mac-mcp-definitely-missing-task33",
        )
        self.assertFalse(result["ok"])
        self.assertEqual("APP_ADAPTER_ARGUMENT_INVALID", result["reason_code"])


class NotesAdapterTests(unittest.TestCase):
    def test_find_notes_returns_only_metadata(self) -> None:
        raw = (
            f"note-1{_US}Task 33 Fixture{_US}Tests"
            f"{_RS}note-2{_US}Task 33 Other{_US}Archive"
        )
        captured = {}
        with patch("mcp_server.app_adapters._run", side_effect=lambda script, **kw: captured.setdefault("script", script) and raw):
            result = mac_app(
                SETTINGS, app="Notes", action="find_notes",
                query="Task 33", exact=False, limit=5,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(2, result["count"])
        self.assertEqual("note-1", result["notes"][0]["id"])
        self.assertNotIn("body", result["notes"][0])
        self.assertIn('name contains "Task 33"', captured["script"])

    def test_open_note_uses_stable_id_and_verifies_selection(self) -> None:
        raw = f"OK{_US}note-1{_US}Task 33 Fixture"
        with patch("mcp_server.app_adapters._run", return_value=raw),              patch("mcp_server.app_adapters._focus_begin", return_value={"pid": 1}),              patch("mcp_server.app_adapters._focus_finish", return_value={"status": "restored", "restored": True}):
            result = mac_app(
                SETTINGS, app="Notes", action="open_note",
                item_id="note-1",
            )
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        self.assertEqual("note-1", result["id"])
        self.assertTrue(result["focus"]["restored"])

    def test_open_note_ambiguous_query_fails_closed(self) -> None:
        with patch("mcp_server.app_adapters._run", return_value=f"COUNT{_US}2"),              patch("mcp_server.app_adapters._focus_begin", return_value=None):
            result = mac_app(
                SETTINGS, app="Notes", action="open_note",
                query="Duplicate",
            )
        self.assertFalse(result["ok"])
        self.assertEqual("NOTES_NOTE_NOT_UNIQUE", result["reason_code"])
        self.assertEqual(2, result["match_count"])

    def test_find_notes_requires_query(self) -> None:
        result = mac_app(SETTINGS, app="Notes", action="find_notes", query="")
        self.assertFalse(result["ok"])
        self.assertEqual("APP_ADAPTER_ARGUMENT_INVALID", result["reason_code"])


class MailAdapterTests(unittest.TestCase):
    def test_find_messages_is_bounded_to_metadata(self) -> None:
        raw = f"msg-1{_US}Invoice{_US}sender@example.com{_US}Friday"
        captured = {}
        def fake_run(script, **kwargs):
            captured["script"] = script
            return raw
        with patch("mcp_server.app_adapters._run", side_effect=fake_run):
            result = mac_app(
                SETTINGS, app="Mail", action="find_messages",
                query="Invoice", sender="example.com", mailbox="inbox",
            )
        self.assertTrue(result["ok"])
        self.assertEqual(1, result["count"])
        self.assertEqual("msg-1", result["messages"][0]["message_id"])
        self.assertNotIn("content", result["messages"][0])
        self.assertIn("subject contains", captured["script"])
        self.assertIn("sender contains", captured["script"])

    def test_find_messages_requires_subject_or_sender_filter(self) -> None:
        result = mac_app(SETTINGS, app="Mail", action="find_messages")
        self.assertFalse(result["ok"])
        self.assertEqual("APP_ADAPTER_ARGUMENT_INVALID", result["reason_code"])

    def test_invalid_mailbox_is_rejected_before_applescript(self) -> None:
        with patch("mcp_server.app_adapters._run") as run:
            result = mac_app(
                SETTINGS, app="Mail", action="find_messages",
                query="x", mailbox="all mail",
            )
        self.assertFalse(result["ok"])
        self.assertEqual("APP_ADAPTER_ARGUMENT_INVALID", result["reason_code"])
        run.assert_not_called()

    def test_open_message_uses_message_id(self) -> None:
        raw = f"OK{_US}msg-1{_US}Invoice"
        with patch("mcp_server.app_adapters._run", return_value=raw),              patch("mcp_server.app_adapters._focus_begin", return_value=None),              patch("mcp_server.app_adapters._focus_finish", return_value={"status": "preserved"}):
            result = mac_app(
                SETTINGS, app="Mail", action="open_message",
                item_id="msg-1", mailbox="inbox",
            )
        self.assertTrue(result["ok"])
        self.assertEqual("msg-1", result["message_id"])
        self.assertEqual("application_command_accepted", result["verification"])


class CalendarAdapterTests(unittest.TestCase):
    def test_find_events_is_date_bounded_and_returns_uid(self) -> None:
        raw = f"uid-1{_US}Task 33 Fixture{_US}Test Calendar{_US}Friday{_US}Friday"
        captured = {}
        def fake_run(script, **kwargs):
            captured["script"] = script
            return raw
        with patch("mcp_server.app_adapters._run", side_effect=fake_run):
            result = mac_app(
                SETTINGS, app="Calendar", action="find_events",
                query="Task 33", date_from="2026-09-01", date_to="2026-09-30",
            )
        self.assertTrue(result["ok"])
        self.assertEqual("uid-1", result["events"][0]["uid"])
        self.assertIn("start date >= fromDate", captured["script"])
        self.assertIn('summary contains "Task 33"', captured["script"])

    def test_find_events_rejects_reversed_bounds(self) -> None:
        result = mac_app(
            SETTINGS, app="Calendar", action="find_events",
            query="Task", date_from="2026-10-01", date_to="2026-09-01",
        )
        self.assertFalse(result["ok"])
        self.assertEqual("APP_ADAPTER_ARGUMENT_INVALID", result["reason_code"])

    def test_open_event_uses_uid(self) -> None:
        raw = f"OK{_US}uid-1{_US}Task 33 Fixture"
        with patch("mcp_server.app_adapters._run", return_value=raw),              patch("mcp_server.app_adapters._focus_begin", return_value=None),              patch("mcp_server.app_adapters._focus_finish", return_value={"status": "preserved"}):
            result = mac_app(SETTINGS, app="Calendar", action="open_event", item_id="uid-1")
        self.assertTrue(result["ok"])
        self.assertEqual("uid-1", result["uid"])
        self.assertEqual("application_command_accepted", result["verification"])

    def test_find_events_requires_query(self) -> None:
        result = mac_app(SETTINGS, app="Calendar", action="find_events", query="")
        self.assertFalse(result["ok"])
        self.assertEqual("APP_ADAPTER_ARGUMENT_INVALID", result["reason_code"])


class PreviewAdapterTests(unittest.TestCase):
    def test_list_documents_returns_name_and_path(self) -> None:
        raw = f"fixture.pdf{_US}/tmp/fixture.pdf"
        with patch("mcp_server.app_adapters._run", return_value=raw):
            result = mac_app(SETTINGS, app="Preview", action="list_documents")
        self.assertTrue(result["ok"])
        self.assertEqual([{"name": "fixture.pdf", "path": "/tmp/fixture.pdf"}], result["documents"])

    def test_open_document_uses_background_open_and_verifies_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "fixture.pdf"
            path.write_bytes(b"%PDF-1.4\n%%EOF\n")
            completed = SimpleNamespace(returncode=0, stdout="", stderr="")
            with patch("mcp_server.app_adapters.subprocess.run", return_value=completed) as run,                  patch("mcp_server.app_adapters._preview_paths", return_value=[str(path.resolve())]),                  patch("mcp_server.app_adapters._focus_begin", return_value=None),                  patch("mcp_server.app_adapters._focus_finish", return_value={"status": "preserved"}):
                result = mac_app(
                    SETTINGS, app="Preview", action="open_document",
                    path=str(path), preserve_focus=True,
                )
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        self.assertIn("-g", run.call_args.args[0])
        self.assertEqual(str(path.resolve()), result["path"])

    def test_open_document_missing_path_fails(self) -> None:
        result = mac_app(
            SETTINGS, app="Preview", action="open_document",
            path="/tmp/mac-mcp-task33-no-document.pdf",
        )
        self.assertFalse(result["ok"])
        self.assertEqual("APP_ADAPTER_ARGUMENT_INVALID", result["reason_code"])

    def test_preview_limit_is_bounded(self) -> None:
        result = mac_app(SETTINGS, app="Preview", action="list_documents", limit=100)
        self.assertFalse(result["ok"])
        self.assertEqual("APP_ADAPTER_ARGUMENT_INVALID", result["reason_code"])


class SettingsAdapterTests(unittest.TestCase):
    def test_list_panes_returns_stable_ids(self) -> None:
        raw = f"com.apple.Accessibility-Settings.extension{_US}Accessibility"
        with patch("mcp_server.app_adapters._run", return_value=raw):
            result = mac_app(SETTINGS, app="Settings", action="list_panes")
        self.assertTrue(result["ok"])
        self.assertEqual("System Settings", result["app"])
        self.assertEqual("com.apple.Accessibility-Settings.extension", result["panes"][0]["id"])

    def test_open_pane_by_id_verifies_current_pane(self) -> None:
        raw = f"OK{_US}com.apple.Accessibility-Settings.extension{_US}Accessibility"
        with patch("mcp_server.app_adapters._run", return_value=raw),              patch("mcp_server.app_adapters._focus_begin", return_value=None),              patch("mcp_server.app_adapters._focus_finish", return_value={"status": "preserved"}):
            result = mac_app(
                SETTINGS, app="System Settings", action="open_pane",
                item_id="com.apple.Accessibility-Settings.extension",
            )
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])

    def test_open_pane_allows_empty_localized_name(self) -> None:
        raw = f"OK{_US}com.apple.systempreferences.GeneralSettings{_US}__MAC_MCP_NONE__"
        with patch("mcp_server.app_adapters._run", return_value=raw),              patch("mcp_server.app_adapters._focus_begin", return_value=None),              patch("mcp_server.app_adapters._focus_finish", return_value={"status": "preserved"}):
            result = mac_app(
                SETTINGS, app="System Settings", action="open_pane",
                item_id="com.apple.systempreferences.GeneralSettings",
            )
        self.assertTrue(result["ok"])
        self.assertEqual("", result["name"])

    def test_open_pane_ambiguous_name_fails_closed(self) -> None:
        with patch("mcp_server.app_adapters._run", return_value=f"COUNT{_US}2"),              patch("mcp_server.app_adapters._focus_begin", return_value=None):
            result = mac_app(
                SETTINGS, app="Settings", action="open_pane",
                query="Privacy", exact=False,
            )
        self.assertFalse(result["ok"])
        self.assertEqual("SETTINGS_PANE_NOT_UNIQUE", result["reason_code"])

    def test_open_pane_requires_identifier_or_query(self) -> None:
        result = mac_app(SETTINGS, app="Settings", action="open_pane")
        self.assertFalse(result["ok"])
        self.assertEqual("APP_ADAPTER_ARGUMENT_INVALID", result["reason_code"])


if __name__ == "__main__":
    unittest.main()
