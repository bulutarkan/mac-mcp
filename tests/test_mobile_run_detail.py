"""#65: a failed run's steps can be opened on the phone.

The mobile list showed six runs as plain rows (outcome and counts); nothing said which step a failed
run stopped at or why. A run now opens in place to its steps: status, the failed step and its safe
reason code. Prompts, tool inputs, raw results, error text and screenshots stay on the Mac.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

from tests.test_mobile_dashboard import MobileDashboardTests

MOBILE = Path(__file__).resolve().parents[1] / "mcp_server" / "mobile"


class MobileRunDetailTests(MobileDashboardTests):
    def phone(self, app) -> TestClient:
        manager = TestClient(app, base_url="https://testserver")
        code = self.pair_code(self.create_pairing(manager)["pair_url"])
        phone = TestClient(app, base_url="https://testserver")
        self.assertEqual(200, phone.post("/mobile/pair", json={"code": code, "device_name": "Phone"}).status_code)
        return phone

    def write_run(self, root: Path) -> str:
        run_id = "run_" + "a" * 12
        record = {
            "run_id": run_id, "kind": "recipe", "name": "Weekly report", "started_at": time.time() - 30,
            "duration_ms": 4200, "ok": False, "reason_code": "ELEMENT_NOT_FOUND", "failed_step": "send",
            "error": "Could not find the Send button for jane@example.com",
            "steps": [
                {"index": 0, "id": "open", "tool": "browser_do", "ok": True, "status": "done"},
                {"index": 1, "id": "send", "tool": "browser_act", "ok": False, "reason_code": "ELEMENT_NOT_FOUND",
                 "error": "selector #send for jane@example.com", "evidence": [{"name": "shot.png", "captured": True}]},
            ],
        }
        (root / f"{run_id}.json").write_text(json.dumps(record), encoding="utf-8")
        return run_id

    def test_a_run_shows_its_steps_and_where_it_stopped_without_its_contents(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app, _telemetry, _store = self.make_app(root)
            runs = root / "runs"
            runs.mkdir()
            with patch("mcp_server.run_evidence.runs_root", return_value=runs):
                run_id = self.write_run(runs)
                stranger = TestClient(app, base_url="https://testserver")
                self.assertIn(stranger.get(f"/mobile/api/runs/{run_id}").status_code, {401, 403})
                phone = self.phone(app)
                answer = phone.get(f"/mobile/api/runs/{run_id}")
                self.assertEqual(200, answer.status_code)
                body = answer.json()
                self.assertEqual(("send", "ELEMENT_NOT_FOUND", False),
                                 (body["run"]["failed_step"], body["run"]["reason_code"], body["run"]["ok"]))
                self.assertEqual(["browser_do", "browser_act"], [step["tool"] for step in body["steps"]])
                text = json.dumps(body)
                self.assertNotIn("jane@example.com", text)
                self.assertNotIn("evidence", text)
                self.assertNotIn("error\"", text.replace('"error": ', ""))  # no error text field at all
                gone = phone.get("/mobile/api/runs/run_" + "b" * 12)
                self.assertEqual(404, gone.status_code)
                self.assertIn("no longer kept", gone.json().get("message", ""), gone.json())
                self.assertEqual("run_not_found", phone.get("/mobile/api/runs/not-a-run-id").json()["error"])

    def test_the_page_opens_a_run_in_place(self) -> None:
        script = (MOBILE / "mobile.js").read_text(encoding="utf-8")
        self.assertIn('class="activity-row run-row" data-run-id=', script)
        self.assertIn('api("/mobile/api/runs/" + encodeURIComponent(runId))', script)
        self.assertIn("aria-expanded", script)


if __name__ == "__main__":
    unittest.main()
