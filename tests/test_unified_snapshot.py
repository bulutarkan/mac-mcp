from __future__ import annotations

import json
import threading
import unittest
from unittest.mock import patch

from mcp_server.policy import filter_scoped_result
from mcp_server.policy_scope import ResourceScope
from mcp_server.security import load_settings
from mcp_server import tools_snapshot


class UnifiedSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = load_settings()

    def test_default_snapshot_runs_six_independent_reads_in_parallel(self) -> None:
        section_count = len(tools_snapshot.DEFAULT_SECTIONS)
        rendezvous = threading.Barrier(section_count)
        lock = threading.Lock()
        active = 0
        max_active = 0
        started_sections = set()
        readers = {}

        for section in tools_snapshot.DEFAULT_SECTIONS:
            def make_reader(name: str):
                def reader(settings, **kwargs):
                    nonlocal active, max_active
                    with lock:
                        active += 1
                        max_active = max(max_active, active)
                        started_sections.add(name)
                    try:
                        # Every section must enter before any reader can finish.
                        # This proves actual fan-out without relying on CI wall-clock timing.
                        rendezvous.wait(timeout=5)
                        return {"section": name}
                    finally:
                        with lock:
                            active -= 1
                return reader
            readers[section] = make_reader(section)

        with patch.object(tools_snapshot, "_SECTION_READERS", readers):
            result = tools_snapshot.unified_read_snapshot(self.settings)

        self.assertTrue(result["ok"])
        self.assertFalse(result["partial"])
        self.assertEqual(list(tools_snapshot.DEFAULT_SECTIONS), result["requested_sections"])
        self.assertEqual(section_count, result["parallelism"])
        self.assertEqual(section_count, len(result["sections"]))
        self.assertEqual(set(tools_snapshot.DEFAULT_SECTIONS), started_sections)
        self.assertEqual(section_count, max_active)

    def test_partial_failure_does_not_drop_successful_sections(self) -> None:
        def ok_reader(settings, **kwargs):
            return {"value": 1}

        def bad_reader(settings, **kwargs):
            raise RuntimeError("simulated read failure")

        readers = {name: ok_reader for name in tools_snapshot.DEFAULT_SECTIONS}
        readers["clipboard"] = bad_reader
        with patch.object(tools_snapshot, "_SECTION_READERS", readers):
            result = tools_snapshot.unified_read_snapshot(self.settings)

        self.assertTrue(result["ok"])
        self.assertTrue(result["partial"])
        self.assertEqual(["clipboard"], result["failed_sections"])
        self.assertFalse(result["sections"]["clipboard"]["ok"])
        self.assertTrue(result["sections"]["system"]["ok"])

    def test_all_failed_is_not_ok(self) -> None:
        def bad_reader(settings, **kwargs):
            raise RuntimeError("no read")

        readers = {name: bad_reader for name in tools_snapshot.DEFAULT_SECTIONS}
        with patch.object(tools_snapshot, "_SECTION_READERS", readers):
            result = tools_snapshot.unified_read_snapshot(self.settings)
        self.assertFalse(result["ok"])
        self.assertTrue(result["partial"])
        self.assertEqual(6, len(result["failed_sections"]))

    def test_section_selection_is_deduplicated_and_validated(self) -> None:
        readers = {
            "apps": lambda settings, **kwargs: {"apps": []},
            "system": lambda settings, **kwargs: {"cpu": "x"},
        }
        with patch.object(tools_snapshot, "_SECTION_READERS", {**tools_snapshot._SECTION_READERS, **readers}):
            result = tools_snapshot.unified_read_snapshot(self.settings, sections=["apps", "system", "apps"])
        self.assertEqual(["apps", "system"], result["requested_sections"])
        with self.assertRaises(ValueError):
            tools_snapshot.unified_read_snapshot(self.settings, sections=[])
        with self.assertRaises(ValueError):
            tools_snapshot.unified_read_snapshot(self.settings, sections=["unknown"])

    def test_output_budget_compacts_pathological_sections(self) -> None:
        huge = "x" * 3000
        readers = {
            name: (lambda settings, _name=name, **kwargs: {"rows": [{"name": _name, "value": huge} for _ in range(20)]})
            for name in tools_snapshot.DEFAULT_SECTIONS
        }
        with patch.object(tools_snapshot, "_SECTION_READERS", readers):
            result = tools_snapshot.unified_read_snapshot(self.settings, max_output_bytes=4096)
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
        self.assertLessEqual(len(encoded), 4096)
        self.assertTrue(result["output_truncated"])
        self.assertEqual(len(encoded), result["output_bytes"])

    def test_clipboard_reader_returns_metadata_not_contents(self) -> None:
        with patch.object(tools_snapshot, "_run_osascript", return_value="«class utf8», 37, string, 37"):
            data = tools_snapshot._read_clipboard(self.settings)
        self.assertTrue(data["has_text"])
        self.assertFalse(data["content_included"])
        self.assertNotIn("content", data)
        self.assertEqual(2, data["type_count"])

    def test_snapshot_scope_filters_tabs_and_finder_paths(self) -> None:
        scope = ResourceScope(
            path_roots=("/tmp/allowed",),
            browser_tabs=("btab_allowed",),
        )
        result = {
            "ok": True,
            "sections": {
                "browser_tabs": {"ok": True, "data": {"count": 2, "tabs": [
                    {"tab_handle": "btab_allowed", "title": "Allowed"},
                    {"tab_handle": "btab_secret", "title": "Secret"},
                ]}},
                "selected_context": {"ok": True, "data": {
                    "folder": "/tmp/secret",
                    "selected_count": 2,
                    "selected_paths": ["/tmp/allowed/a.txt", "/tmp/secret/b.txt"],
                }},
            },
        }
        filtered = filter_scoped_result(scope, "mac_snapshot", result)
        tabs = filtered["sections"]["browser_tabs"]["data"]
        selected = filtered["sections"]["selected_context"]["data"]
        self.assertEqual(["btab_allowed"], [row["tab_handle"] for row in tabs["tabs"]])
        self.assertEqual(1, tabs["count"])
        self.assertEqual(["/tmp/allowed/a.txt"], selected["selected_paths"])
        self.assertEqual(1, selected["selected_count"])
        self.assertIsNone(selected["folder"])

    def test_system_parser_drops_network_and_raw_command(self) -> None:
        data = tools_snapshot._parse_system_stdout(
            "=== HOSTNAME ===\nmac.local\n=== UPTIME ===\n12:00 up 1 day, load averages: 1.00 2.00 3.00\n"
            "=== CPU ===\nApple M3\n=== MEMORY ===\nFree: 1 GB\n=== DISK ===\n/dev/x 100G\n"
            "=== BATTERY ===\n90%; AC attached\n=== NETWORK ===\ninet 10.0.0.1\n"
        )
        self.assertEqual([1.0, 2.0, 3.0], data["load_average"])
        self.assertNotIn("network", data)
        self.assertNotIn("command", data)


if __name__ == "__main__":
    unittest.main()
