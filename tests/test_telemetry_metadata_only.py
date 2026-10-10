"""#20: telemetry stores operational metadata, not the content tools read or typed."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import observability
from mcp_server.observability import TelemetryManager, metadata_value

PII = ("Ada Lovelace", "ada@example.com", "+90 555 111 22 33", "Flat 4, Analytical Street")
PAGE = f"Welcome back {PII[0]} ({PII[1]}), phone {PII[2]}, ships to {PII[3]}."


class TelemetryMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "t.sqlite3"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def manager(self, **env: str) -> TelemetryManager:
        with patch.dict(os.environ, env):
            return TelemetryManager(self.db, usage_enabled=False, async_writes=False)

    def record(self, manager: TelemetryManager, tool: str, arguments: dict, result) -> dict:
        event_id = manager.start_call("mcp", tool, arguments)
        manager.finish_call(event_id, result=result)
        return next(row for row in manager.query_events(hours=1) if row["event_id"] == event_id)

    def raw_rows(self) -> str:
        con = sqlite3.connect(self.db)
        try:
            return " ".join(str(v or "") for row in con.execute(
                "SELECT arguments_json, result_json, error FROM tool_events") for v in row)
        finally:
            con.close()

    def test_page_text_shell_output_clipboard_files_and_typed_text_are_not_stored(self) -> None:
        manager = self.manager()
        cases = [
            ("browser_observe", {"tab_handle": "btab_1", "description": "Read the account page"},
             {"ok": True, "url": "https://shop.example/account?email=ada%40example.com", "title": PAGE,
              "elements": [{"element_id": "e1", "role": "textbox", "text": PAGE, "enabled": True}] * 3}),
            ("mac_observe", {"app": "Mail"}, {"ok": True, "nodes": [{"role": "AXStaticText", "label": PAGE}]}),
            ("run_command", {"command": "cat contacts.txt", "cwd": "/Users/me"},
             {"ok": True, "exit_code": 0, "stdout": PAGE, "stderr": ""}),
            ("read_file", {"path": "/Users/me/contacts.txt"}, {"ok": True, "path": "/Users/me/contacts.txt", "content": PAGE}),
            ("clipboard_read", {}, {"ok": True, "text": PAGE}),
            ("browser_act", {"tab_handle": "btab_1", "actions": [{"type": "type", "element_id": "e1", "text": PAGE}]},
             {"ok": True, "actions": [{"type": "type", "ok": True, "value": PAGE, "verification": "value_applied"}]}),
        ]
        for tool, arguments, result in cases:
            with self.subTest(tool=tool):
                row = self.record(manager, tool, arguments, result)
                rendered = json.dumps(row, ensure_ascii=False)
                for secret in PII:
                    self.assertNotIn(secret, rendered)
                self.assertEqual("success", row["status"])
                self.assertTrue(row["result"]["ok"])
                self.assertGreater(row["result_size"], len(PAGE))  # size of what the tool returned
        stored = self.raw_rows()
        for secret in PII:
            self.assertNotIn(secret, stored)

    def test_operational_fields_stay_readable(self) -> None:
        manager = self.manager()
        row = self.record(manager, "browser_observe", {"tab_handle": "btab_1", "description": "Read the page"},
                          {"ok": True, "url": "https://shop.example/a/b?token=x#frag", "count": 3,
                           "elements": [{"element_id": "e1", "text": PAGE}], "reason_code": "TARGET_FOUND"})
        self.assertEqual("https://shop.example/a/b", row["result"]["url"])
        self.assertEqual(3, row["result"]["count"])
        self.assertEqual("e1", row["result"]["elements"][0]["element_id"])
        self.assertEqual(f"[text · {len(PAGE):,} chars]", row["result"]["elements"][0]["text"])
        self.assertEqual("TARGET_FOUND", row["result"]["reason_code"])
        self.assertEqual("Read the page", row["arguments"]["description"])
        command = self.record(manager, "run_command", {"command": "git status --short", "cwd": "/repo"},
                              {"ok": True, "exit_code": 0, "stdout": PAGE})
        self.assertEqual("git status --short", command["arguments"]["command"])
        self.assertEqual(0, command["result"]["exit_code"])
        written = self.record(manager, "write_file", {"path": "/repo/a.txt", "content": PAGE},
                              {"ok": True, "written": ["/repo/a.txt", "/repo/b.txt"]})
        self.assertEqual("/repo/a.txt", written["arguments"]["path"])
        self.assertEqual(["/repo/a.txt", "/repo/b.txt"], written["result"]["written"])
        self.assertNotIn(PII[0], json.dumps(written["arguments"]))

    def test_redaction_markers_and_errors_are_kept(self) -> None:
        self.assertEqual("[BROWSER INPUT REDACTED]", metadata_value("[BROWSER INPUT REDACTED]", key="text"))
        self.assertEqual("[voice transcript not stored]", metadata_value("[voice transcript not stored]"))
        manager = self.manager()
        event_id = manager.start_call("mcp", "browser_act", {"tab_handle": "btab_1"})
        manager.finish_call(event_id, error="ELEMENT_NOT_READY: target is offscreen")
        row = next(r for r in manager.query_events(hours=1) if r["event_id"] == event_id)
        self.assertIn("ELEMENT_NOT_READY", str(row["error"]))

    def test_content_previews_are_an_explicit_opt_in(self) -> None:
        manager = self.manager(**{observability.TELEMETRY_CONTENT_ENV: "preview"})
        self.assertTrue(manager.content_preview)
        row = self.record(manager, "read_file", {"path": "/tmp/x"}, {"ok": True, "content": "hello there"})
        self.assertEqual("hello there", row["result"]["content"])

    def test_existing_content_is_scrubbed_once_and_the_database_is_owner_only(self) -> None:
        legacy = self.manager(**{observability.TELEMETRY_CONTENT_ENV: "preview"})
        self.record(legacy, "browser_observe", {"tab_handle": "btab_1"}, {"ok": True, "title": PAGE})
        con = sqlite3.connect(self.db)
        con.execute("PRAGMA user_version=2")  # a store written before metadata-only telemetry
        con.commit()
        con.close()
        os.chmod(self.db, 0o644)
        self.assertIn(PII[0], self.raw_rows())

        self.manager()
        self.assertNotIn(PII[0], self.raw_rows())
        con = sqlite3.connect(self.db)
        version = con.execute("PRAGMA user_version").fetchone()[0]
        con.close()
        self.assertEqual(observability.TELEMETRY_METADATA_VERSION, version)
        self.assertEqual(0o600, stat.S_IMODE(self.db.stat().st_mode))


if __name__ == "__main__":
    unittest.main()
