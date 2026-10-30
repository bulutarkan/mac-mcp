"""Preview, Numbers, Keynote, Pages and Music adapters (#131)."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp_server import app_adapters_docs as docs
from mcp_server import app_adapters_music as music
from mcp_server.app_adapter_core import AppAdapterError, _run_js
from mcp_server.app_adapters import mac_app, normalize_app_name, owner_overview, supported_actions
from mcp_server.policy import resolve_risk
from mcp_server.process_supervision import OsascriptResult

SETTINGS = SimpleNamespace()


def _answer(payload):
    return OsascriptResult(0, json.dumps(payload), "")


class RegistryTests(unittest.TestCase):
    def test_new_apps_and_aliases(self) -> None:
        for alias, name in (("numbers", "Numbers"), ("Keynote", "Keynote"), ("pages", "Pages"), ("music", "Music"),
                            ("Apple Music", "Music"), ("itunes", "Music")):
            self.assertEqual(name, normalize_app_name(alias))
        self.assertIn("read_document", supported_actions("Preview"))
        self.assertEqual(("list_documents", "open_document", "read_table"), supported_actions("Numbers"))

    def test_capabilities_list_actions(self) -> None:
        for app in ("Numbers", "Keynote", "Pages", "Music"):
            with self.subTest(app=app):
                result = mac_app(SETTINGS, app=app, action="capabilities")
                self.assertTrue(result["ok"])
                self.assertEqual(list(supported_actions(app)), result["actions"])

    def test_owner_overview_groups_reads_and_player_changes(self) -> None:
        rows = {row["name"]: row for row in owner_overview()}
        self.assertEqual(["See what is playing", "Find playlists", "Find tracks"], rows["Music"]["reads"])
        self.assertIn("Skip to the next song", rows["Music"]["changes"])
        self.assertIn("Read table", rows["Numbers"]["reads"])
        self.assertIn("Open document", rows["Numbers"]["opens"])

    def test_reads_are_read_only_and_playback_is_a_local_write(self) -> None:
        for action in ("read_document", "read_table", "read_slides", "now_playing", "find_playlists", "find_tracks"):
            with self.subTest(action=action):
                _, risk = resolve_risk("mac_app", {"app": "Music", "action": action})
                self.assertEqual("read_only", risk.requested_access_mode.value)
        for action in ("play", "pause", "next_track", "previous_track"):
            with self.subTest(action=action):
                _, risk = resolve_risk("mac_app", {"app": "Music", "action": action})
                self.assertFalse(risk.destructive)
                self.assertIn("local_write", {cap.value for cap in risk.capabilities})


class RunJsTests(unittest.TestCase):
    def test_runs_jxa_with_argv(self) -> None:
        with patch("mcp_server.app_adapter_core.run_osascript", return_value=_answer({"x": 1})) as run:
            self.assertEqual({"x": 1}, _run_js("function run(argv) {}", ["a", 2]))
        self.assertEqual(["a", "2"], run.call_args.kwargs["args"])
        self.assertEqual("JavaScript", run.call_args.kwargs["language"])

    def test_error_payload_becomes_reason_code(self) -> None:
        with patch("mcp_server.app_adapter_core.run_osascript",
                   return_value=_answer({"error": "APP_NOT_RUNNING", "message": "closed", "retryable": False})):
            with self.assertRaises(AppAdapterError) as caught:
                _run_js("x", [])
        self.assertEqual("APP_NOT_RUNNING", caught.exception.code)
        self.assertFalse(caught.exception.extra["retryable"])

    def test_timeout_is_retryable(self) -> None:
        with patch("mcp_server.app_adapter_core.run_osascript", return_value=OsascriptResult(None, "", "", True)):
            with self.assertRaises(AppAdapterError) as caught:
                _run_js("x", [], code="NUMBERS_READ_FAILED")
        self.assertEqual("NUMBERS_READ_FAILED", caught.exception.code)
        self.assertTrue(caught.exception.extra["retryable"])

    def test_unreadable_answer(self) -> None:
        with patch("mcp_server.app_adapter_core.run_osascript", return_value=OsascriptResult(0, "nope", "")):
            with self.assertRaises(AppAdapterError) as caught:
                _run_js("x", [])
        self.assertEqual("APP_ADAPTER_RESPONSE_INVALID", caught.exception.code)


@unittest.skipUnless(shutil.which("osacompile"), "needs macOS osacompile")
class ScriptSyntaxTests(unittest.TestCase):
    """Pages and Keynote may be missing on a test Mac, so at least every script must compile."""

    def test_every_jxa_script_compiles(self) -> None:
        scripts = {name: value for name, value in vars(docs).items() if name.endswith("_JS") and name != "_IWORK_JS"}
        scripts["preview_read"] = docs._preview_read_js()
        scripts.update({name: value for name, value in vars(music).items() if name.endswith("_JS")})
        with tempfile.TemporaryDirectory() as tmp:
            for name, source in scripts.items():
                with self.subTest(script=name):
                    out = subprocess.run(["osacompile", "-l", "JavaScript", "-o", f"{tmp}/s.scpt", "-e", source],
                                         capture_output=True, text=True, timeout=30)
                    self.assertEqual(0, out.returncode, out.stderr)


class PreviewReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.pdf = Path(self.tmp.name) / "a.pdf"
        self.pdf.write_bytes(b"%PDF-1.4\n")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_pages_cursor_and_ocr_note(self) -> None:
        payload = {"kind": "pdf", "page_count": 5, "title": "Fatura",
                   "pages": [{"page": 2, "text": "a", "ocr": False, "notes": []},
                             {"page": 3, "text": "b", "ocr": True, "notes": [{"type": "Text", "text": "n"}]}]}
        with patch("mcp_server.app_adapters_docs._run_js", return_value=payload) as run:
            result = mac_app(SETTINGS, app="Preview", action="read_document", path=str(self.pdf), cursor="2", limit=2)
        self.assertTrue(result["ok"], result)
        self.assertEqual(["pdf", "2", "2"], run.call_args.args[1][1:4])
        self.assertEqual("4", result["next_cursor"])
        self.assertEqual("Fatura", result["title"])
        self.assertIn("note", result)

    def test_long_pages_stop_at_the_character_budget(self) -> None:
        big = "x" * (docs.MAX_CHARS - 10)
        payload = {"kind": "pdf", "page_count": 3,
                   "pages": [{"page": 1, "text": big, "ocr": False, "notes": []},
                             {"page": 2, "text": "y" * 100, "ocr": False, "notes": []}]}
        with patch("mcp_server.app_adapters_docs._run_js", return_value=payload):
            result = docs.preview_read(str(self.pdf), cursor=None, limit=10, timeout_s=10)
        self.assertEqual([1], [page["page"] for page in result["pages"]])
        self.assertEqual("2", result["next_cursor"])

    def test_text_files_are_refused(self) -> None:
        other = Path(self.tmp.name) / "a.txt"
        other.write_text("hi")
        result = mac_app(SETTINGS, app="Preview", action="read_document", path=str(other))
        self.assertEqual("PREVIEW_UNSUPPORTED_TYPE", result["reason_code"])

    def test_no_path_and_no_front_document(self) -> None:
        with patch("mcp_server.app_adapters_docs._run_js", return_value={"running": False, "path": None}):
            result = mac_app(SETTINGS, app="Preview", action="read_document")
        self.assertEqual("PREVIEW_PATH_REQUIRED", result["reason_code"])

    def test_restricted_agent_reads_only_inside_its_paths(self) -> None:
        context = SimpleNamespace(scope=SimpleNamespace(path_roots=("/nowhere-else",)))
        with patch("mcp_server.policy.current_policy_context", return_value=context), \
                patch("mcp_server.app_adapters_docs._run_js") as run:
            result = mac_app(SETTINGS, app="Preview", action="read_document", path=str(self.pdf))
        self.assertEqual("SCOPE_PATH_DENIED", result["reason_code"])
        run.assert_not_called()


class NumbersTests(unittest.TestCase):
    def test_list_gives_each_table_an_item_id(self) -> None:
        payload = {"total": 1, "documents": [{"name": "Bütçe.numbers", "path": "/x/Bütçe.numbers", "sheets": [
            {"name": "Ekim", "tables": [{"name": "Gider", "row_count": 3, "column_count": 2, "header_rows": 1}]}]}]}
        with patch("mcp_server.app_adapters_docs._run_js", return_value=payload):
            result = mac_app(SETTINGS, app="Numbers", action="list_documents")
        self.assertEqual("Bütçe.numbers › Ekim › Gider", result["documents"][0]["sheets"][0]["tables"][0]["item_id"])

    def test_whole_table_read_drops_trailing_blanks_and_pages(self) -> None:
        payload = {"document": "d", "sheet": "s", "table": "t", "row_count": 450, "column_count": 4, "header_rows": 1,
                   "header": ["Ad", "Adet", None, None], "range": "A1:D200", "first_row": 1, "window_end": 200,
                   "rows": [["Ad", "Adet", None, None], ["Elma", "3", "", None], [None, None, None, None]]}
        with patch("mcp_server.app_adapters_docs._run_js", return_value=payload) as run:
            result = mac_app(SETTINGS, app="Numbers", action="read_table", item_id="d › s › t")
        self.assertEqual([["Ad", "Adet"], ["Elma", "3"]], result["rows"])
        self.assertEqual(["Ad", "Adet"], result["header"])
        self.assertEqual("201", result["next_cursor"])
        self.assertEqual(["d", "s", "t", ""], run.call_args.args[1][1:5])

    def test_range_is_normalised_and_kept_as_asked(self) -> None:
        payload = {"document": "d", "sheet": "s", "table": "t", "row_count": 5, "column_count": 4, "header_rows": 0,
                   "range": "B2:C3", "first_row": 2, "window_end": None, "rows": [["1", None], [None, None]]}
        with patch("mcp_server.app_adapters_docs._run_js", return_value=payload) as run:
            result = mac_app(SETTINGS, app="Numbers", action="read_table", item_id="d", query="b2 : c3")
        self.assertEqual(["d", "", "", "B2:C3"], run.call_args.args[1][1:5])
        self.assertEqual([["1", None], [None, None]], result["rows"])
        self.assertNotIn("next_cursor", result)

    def test_not_running_is_reported_not_launched(self) -> None:
        with patch("mcp_server.app_adapter_core.run_osascript",
                   return_value=_answer({"error": "APP_NOT_RUNNING", "message": "closed", "retryable": False})):
            result = mac_app(SETTINGS, app="Numbers", action="read_table")
        self.assertFalse(result["ok"])
        self.assertEqual("APP_NOT_RUNNING", result["reason_code"])

    def test_open_document_reads_back_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "Bütçe.numbers"
            target.mkdir()  # iWork documents can be packages
            with patch("mcp_server.app_adapters.subprocess.run",
                       return_value=subprocess.CompletedProcess([], 0, "", "")) as opened, \
                    patch("mcp_server.app_adapters_docs.document_paths", return_value=[str(target.resolve())]), \
                    patch("mcp_server.app_adapters._focus_begin", return_value=None), \
                    patch("mcp_server.app_adapters._focus_finish", return_value={"status": "preserved"}):
                result = mac_app(SETTINGS, app="Numbers", action="open_document", path=str(target))
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["verified"])
        self.assertEqual(["open", "-g", "-a", "Numbers"], opened.call_args.args[0][:4])


class KeynotePagesTests(unittest.TestCase):
    def test_slides_page_with_cursor(self) -> None:
        payload = {"document": "k", "slide_count": 12, "slides": [{"slide": 11}, {"slide": 12}]}
        with patch("mcp_server.app_adapters_docs._run_js", return_value=payload) as run:
            result = mac_app(SETTINGS, app="Keynote", action="read_slides", item_id="k", cursor="11", limit=5)
        self.assertEqual(["k", "11", "5"], run.call_args.args[1][1:])
        self.assertIsNone(result["next_cursor"])

    def test_pages_text_pages_by_character(self) -> None:
        text = "a" * (docs.MAX_CHARS + 5)
        with patch("mcp_server.app_adapters_docs._run_js", return_value={"document": "p", "text": text}):
            first = mac_app(SETTINGS, app="Pages", action="read_document", item_id="p")
            rest = mac_app(SETTINGS, app="Pages", action="read_document", item_id="p", cursor=first["next_cursor"])
        self.assertEqual(docs.MAX_CHARS, len(first["text"]))
        self.assertEqual(5, len(rest["text"]))
        self.assertIsNone(rest["next_cursor"])

    def test_bad_cursor(self) -> None:
        result = mac_app(SETTINGS, app="Pages", action="read_document", cursor="abc")
        self.assertEqual("APP_ADAPTER_ARGUMENT_INVALID", result["reason_code"])


def _state(state, track=None, position=0):
    return {"running": True, "state": state, "position_s": position,
            "track": {"track_id": track, "name": track} if track else None}


class MusicTests(unittest.TestCase):
    def test_play_a_track_reads_it_back(self) -> None:
        answers = [{"running": True}, {"before": _state("paused", "track:A"), "after": _state("playing", "track:B")}]
        with patch("mcp_server.app_adapters_music._run_js", side_effect=answers) as run:
            result = mac_app(SETTINGS, app="Music", action="play", item_id="track:B")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["verified"])
        self.assertTrue(result["changed"])
        self.assertEqual("pause", result["reverse_action"])
        self.assertEqual(["play", "track:B"], run.call_args.args[1])

    def test_play_opens_closed_music_in_the_background(self) -> None:
        answers = [{"running": False}, {"running": True},
                   {"before": _state("stopped"), "after": _state("playing", "track:A")}]
        with patch("mcp_server.app_adapters_music._run_js", side_effect=answers), \
                patch("mcp_server.app_adapters_music.subprocess.run") as opened:
            result = mac_app(SETTINGS, app="Music", action="play")
        self.assertTrue(result["verified"])
        self.assertEqual(["open", "-g", "-b", "com.apple.Music"], opened.call_args.args[0])

    def test_pause_with_music_closed_changes_nothing(self) -> None:
        with patch("mcp_server.app_adapters_music._run_js", return_value={"running": False}), \
                patch("mcp_server.app_adapters_music.subprocess.run") as opened:
            result = mac_app(SETTINGS, app="Music", action="pause")
        self.assertTrue(result["ok"])
        self.assertFalse(result["changed"])
        opened.assert_not_called()

    def test_previous_track_restarting_the_song_counts(self) -> None:
        answers = [{"running": True},
                   {"before": _state("playing", "track:A", 40), "after": _state("playing", "track:A", 0)}]
        with patch("mcp_server.app_adapters_music._run_js", side_effect=answers):
            result = mac_app(SETTINGS, app="Music", action="previous_track")
        self.assertTrue(result["verified"])
        self.assertTrue(result["changed"])

    def test_unverified_play_says_why(self) -> None:
        answers = [{"running": True}, {"before": _state("stopped"), "after": _state("stopped")}]
        with patch("mcp_server.app_adapters_music._run_js", side_effect=answers):
            result = mac_app(SETTINGS, app="Music", action="play")
        self.assertFalse(result["verified"])
        self.assertIn("note", result)

    def test_item_id_must_be_a_music_id(self) -> None:
        result = mac_app(SETTINGS, app="Music", action="play", item_id="Chill")
        self.assertEqual("APP_ADAPTER_ARGUMENT_INVALID", result["reason_code"])
        result = mac_app(SETTINGS, app="Music", action="pause", item_id="track:A")
        self.assertEqual("APP_ADAPTER_ARGUMENT_INVALID", result["reason_code"])

    def test_find_tracks_needs_words(self) -> None:
        result = mac_app(SETTINGS, app="Music", action="find_tracks")
        self.assertEqual("APP_ADAPTER_ARGUMENT_INVALID", result["reason_code"])


if __name__ == "__main__":
    unittest.main()
