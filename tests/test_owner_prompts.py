"""ask_user in the menu app's own window: the question waits there, files come back, pictures as base64."""
from __future__ import annotations

import base64
import io
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image as PILImage
from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp_server import owner_prompts, process_supervision, tools_interactive
from mcp_server.dashboard_routes import create_dashboard_routes
from mcp_server.observability import TelemetryManager
from mcp_server.security import load_settings

TOKEN = "owner-prompt-dashboard-token-0123456789"
AUTH = {"authorization": f"Bearer {TOKEN}"}


class _MenuApp:
    """A stand-in for the menu app: listens, then answers the next question the way a test says."""

    def __init__(self, test: unittest.TestCase) -> None:
        self.test = test

    def __enter__(self) -> "_MenuApp":
        owner_prompts.menu_app_stream_opened()
        return self

    def __exit__(self, *exc) -> None:
        owner_prompts.menu_app_stream_closed()

    @staticmethod
    def next_prompt(timeout: float = 3.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            rows = owner_prompts.pending()
            if rows:
                return rows[0]
            time.sleep(0.02)
        raise AssertionError("no question reached the menu app")


def _run(fn, *args, **kwargs):
    box: dict = {}
    thread = threading.Thread(target=lambda: box.setdefault("result", fn(*args, **kwargs)))
    thread.start()
    return thread, box


def _png(path: Path, size=(3200, 1800), mode="RGB") -> None:
    PILImage.new(mode, size, (200, 120, 40, 255)[: len(mode)]).save(path, format="PNG")


class OwnerPromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mac-mcp-owner-prompts-")
        self.env = patch.dict(os.environ, {"MAC_MCP_STATE_DIR": self.temp.name})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.temp.cleanup()

    def test_without_the_menu_app_the_caller_falls_back_at_once(self) -> None:
        started = time.time()
        self.assertIsNone(owner_prompts.ask({"question": "Q"}, 30))
        self.assertLess(time.time() - started, 0.5)
        self.assertEqual([], owner_prompts.pending())

    def test_a_question_nobody_claims_falls_back_after_the_claim_wait(self) -> None:
        with _MenuApp(self), patch.object(owner_prompts, "CLAIM_TIMEOUT_S", 0.3):
            self.assertIsNone(owner_prompts.ask({"question": "Q"}, 30))
        self.assertEqual([], owner_prompts.pending())
        self.assertEqual([], list(owner_prompts.uploads_root().iterdir()))

    def test_text_answer_reaches_ask_user_with_the_menu_app_shape(self) -> None:
        with _MenuApp(self) as app:
            thread, box = _run(tools_interactive.ask_user, None, "Which range?", sender="Claude Code",
                               timeout_s=20, suggestions=["Last 7 days", "last 7 days", "  This   month "])
            row = app.next_prompt()
            self.assertEqual("Which range?", row["question"])
            self.assertEqual("Claude Code", row["sender"])
            self.assertEqual(["Last 7 days", "This month"], row["suggestions"])
            self.assertTrue(row["allow_attachments"])
            self.assertTrue(Path(row["upload_dir"]).is_dir())
            self.assertTrue(owner_prompts.claim(row["prompt_id"]))
            self.assertEqual({"ok": True}, owner_prompts.submit(row["prompt_id"], "send", "Last 30 days"))
            thread.join(5)
        self.assertEqual({"ok": True, "ui": "menu_app", "response": "Last 30 days", "timed_out": False,
                          "skipped": False, "attachments": []}, box["result"])
        self.assertFalse(Path(row["upload_dir"]).exists())

    def test_skip_and_timeout(self) -> None:
        with _MenuApp(self) as app:
            thread, box = _run(tools_interactive.ask_user, None, "Skip me?", timeout_s=20)
            row = app.next_prompt()
            owner_prompts.claim(row["prompt_id"])
            owner_prompts.submit(row["prompt_id"], "skip")
            thread.join(5)
            self.assertTrue(box["result"]["skipped"])
            self.assertIsNone(box["result"]["response"])

            thread, box = _run(tools_interactive.ask_user, None, "Wait for me?", timeout_s=1)
            owner_prompts.claim(app.next_prompt()["prompt_id"])
            thread.join(5)
            self.assertTrue(box["result"]["timed_out"])
            self.assertEqual("menu_app", box["result"]["ui"])

    def test_pictures_come_back_as_base64_and_other_files_by_path(self) -> None:
        with _MenuApp(self) as app:
            thread, box = _run(owner_prompts.ask, {"question": "Send the screenshot"}, 20)
            row = app.next_prompt()
            owner_prompts.claim(row["prompt_id"])
            folder = Path(row["upload_dir"])
            _png(folder / "screen.png")
            (folder / "notes.pdf").write_bytes(b"%PDF-1.4 test")
            result = owner_prompts.submit(row["prompt_id"], "send", "",
                                          [{"path": str(folder / "screen.png")}, {"path": str(folder / "notes.pdf")}])
            self.assertEqual({"ok": True}, result)
            thread.join(5)
        picture, document = box["result"]["attachments"]
        self.assertEqual(("screen.png", "image", "image/png"), (picture["name"], picture["kind"], picture["mime_type"]))
        self.assertEqual("base64", picture["encoding"])
        decoded = PILImage.open(io.BytesIO(base64.b64decode(picture["data_base64"])))
        self.assertEqual("JPEG", decoded.format)
        self.assertEqual(owner_prompts.INLINE_IMAGE_EDGE, max(decoded.size))
        self.assertEqual(("notes.pdf", "file"), (document["name"], document["kind"]))
        self.assertNotIn("data_base64", document)
        self.assertTrue(Path(document["path"]).is_file())  # kept so the agent can read it

    def test_transparent_pictures_stay_png(self) -> None:
        folder = Path(self.temp.name)
        _png(folder / "logo.png", size=(40, 40), mode="RGBA")
        described = owner_prompts._describe([folder / "logo.png"])
        self.assertEqual("image/png", described[0]["data_mime_type"])

    def test_only_files_in_the_question_folder_are_accepted(self) -> None:
        outside = Path(self.temp.name) / "secret.txt"
        outside.write_text("private")
        with _MenuApp(self) as app:
            thread, box = _run(owner_prompts.ask, {"question": "Q"}, 20)
            row = app.next_prompt()
            folder = Path(row["upload_dir"])
            (folder / "link.txt").symlink_to(outside)
            for bad in [str(outside), str(folder / "link.txt"), str(folder / ".." / "secret.txt"), str(folder)]:
                self.assertEqual("invalid_attachment",
                                 owner_prompts.submit(row["prompt_id"], "send", "x", [{"path": bad}])["error"])
            self.assertEqual("empty_answer", owner_prompts.submit(row["prompt_id"], "send", "  ")["error"])
            self.assertEqual("invalid_action", owner_prompts.submit(row["prompt_id"], "delete")["error"])
            owner_prompts.submit(row["prompt_id"], "skip")
            thread.join(5)
        self.assertEqual({"action": "skip"}, box["result"])
        self.assertFalse(folder.exists())  # skipped: what was added is removed
        self.assertEqual("prompt_not_found", owner_prompts.submit(row["prompt_id"], "skip")["error"])

    def test_no_upload_folder_when_files_are_not_allowed(self) -> None:
        with _MenuApp(self) as app:
            thread, box = _run(owner_prompts.ask, {"question": "Q"}, 20, allow_attachments=False)
            row = app.next_prompt()
            self.assertIsNone(row["upload_dir"])
            self.assertEqual("attachments_not_allowed",
                             owner_prompts.submit(row["prompt_id"], "send", "x", [{"path": "/tmp/x"}])["error"])
            owner_prompts.submit(row["prompt_id"], "send", "fine")
            thread.join(5)
        self.assertEqual("fine", box["result"]["text"])

    def test_mcp_result_turns_pictures_into_image_blocks(self) -> None:
        tiny = base64.b64encode(b"\xff\xd8jpeg").decode()
        result = {"ok": True, "response": "see", "attachments": [
            {"name": "a.jpg", "kind": "image", "data_base64": tiny, "data_mime_type": "image/jpeg", "encoding": "base64"},
            {"name": "b.pdf", "kind": "file", "path": "/x/b.pdf"},
        ]}
        text, image = tools_interactive.ask_user_mcp_result(result)
        self.assertNotIn("data_base64", text)
        self.assertIn('"shown_below":true', text)
        self.assertEqual(b"\xff\xd8jpeg", image.data)
        plain = {"ok": True, "response": "hi", "attachments": []}
        self.assertIs(plain, tools_interactive.ask_user_mcp_result(plain))

    def test_the_dialog_stays_the_fallback(self) -> None:
        class Done:
            pid, returncode = 1, 0

            def communicate(self, timeout=None):
                return "typed\n", ""

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

        with patch.object(process_supervision.subprocess, "Popen", return_value=Done()) as popen:
            result = tools_interactive.ask_user(None, "Anyone?", timeout_s=5)
        popen.assert_called_once()
        self.assertEqual({"ok": True, "response": "typed", "timed_out": False, "skipped": False}, result)


class OwnerPromptRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mac-mcp-owner-prompt-routes-")
        self.env = patch.dict(os.environ, {"MAC_MCP_STATE_DIR": self.temp.name})
        self.env.start()
        telemetry = TelemetryManager(db_path=Path(self.temp.name) / "telemetry.sqlite3", max_events=100,
                                     usage_enabled=False, async_writes=False)
        self.client = TestClient(Starlette(routes=create_dashboard_routes(telemetry, load_settings(), TOKEN)))

    def tearDown(self) -> None:
        self.env.stop()
        self.temp.cleanup()

    def test_routes_need_the_dashboard_token(self) -> None:
        self.assertEqual(401, self.client.get("/dashboard/api/owner-prompts").status_code)
        self.assertEqual(401, self.client.post("/dashboard/api/owner-prompts/answer", json={}).status_code)

    def test_list_claim_and_answer_over_http(self) -> None:
        with _MenuApp(self) as app:
            thread, box = _run(owner_prompts.ask, {"question": "Over HTTP?"}, 20)
            app.next_prompt()
            rows = self.client.get("/dashboard/api/owner-prompts", headers=AUTH).json()["prompts"]
            self.assertEqual("Over HTTP?", rows[0]["question"])
            prompt_id = rows[0]["prompt_id"]
            self.assertEqual(404, self.client.post("/dashboard/api/owner-prompts/claim", headers=AUTH,
                                                   json={"prompt_id": "nope"}).status_code)
            self.assertEqual(200, self.client.post("/dashboard/api/owner-prompts/claim", headers=AUTH,
                                                   json={"prompt_id": prompt_id}).status_code)
            bad = self.client.post("/dashboard/api/owner-prompts/answer", headers=AUTH,
                                   json={"prompt_id": prompt_id, "action": "send", "text": ""})
            self.assertEqual((400, "empty_answer"), (bad.status_code, bad.json()["error"]))
            done = self.client.post("/dashboard/api/owner-prompts/answer", headers=AUTH,
                                    json={"prompt_id": prompt_id, "action": "send", "text": "Yes"})
            self.assertEqual(200, done.status_code)
            thread.join(5)
        self.assertEqual("Yes", box["result"]["text"])


if __name__ == "__main__":
    unittest.main()
