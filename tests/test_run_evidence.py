"""#108: auditable run evidence timeline.

A computer_plan or recipe run gets a run_id that is stamped on its own telemetry row
and on every nested step; a small per-run record keeps the ordered steps and
outcomes; opted-in checkpoint screenshots are kept only when the owner turned run
evidence on, never while a password field is on screen, and expire with the record.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp_server import run_evidence
from mcp_server.computer_plan import ComputerPlanError, execute_computer_plan
from mcp_server.observability import ObservedFastMCP, TelemetryManager, current_run_id
from mcp_server.policy import PolicyContext

JPEG = b"\xff\xd8\xff\xe0fake-jpeg-bytes\xff\xd9"
TOKEN = "dashboard-test-token-0123456789-abcdefghijklmnopqrstuvwxyz"
AUTH = {"authorization": f"Bearer {TOKEN}"}


def image_result(elements=()):
    observation = json.dumps({"ok": True, "elements": list(elements)})
    return [SimpleNamespace(type="text", text=observation),
            SimpleNamespace(type="image", data=base64.b64encode(JPEG).decode(), mimeType="image/jpeg")]


class RunEvidenceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings_path = self.root / "settings.json"
        self.env = patch.dict(os.environ, {"MAC_MCP_STATE_DIR": str(self.root / "state"),
                                           "MAC_MCP_SETTINGS_PATH": str(self.settings_path)})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.temp.cleanup()

    def evidence_on(self, days=7) -> None:
        self.settings_path.write_text(json.dumps({"privacy": {"run_evidence_capture": True,
                                                              "run_evidence_retention_days": days}}), encoding="utf-8")


class RunIdTelemetryTests(RunEvidenceTestCase):
    def test_a_plan_and_all_its_steps_share_one_run_id(self) -> None:
        async def run():
            telemetry = TelemetryManager(db_path=self.root / "telemetry.sqlite3")
            mcp = ObservedFastMCP(name="run-id", telemetry=telemetry,
                                  policy_context_provider=lambda: PolicyContext(profile="trusted", actor="test"))
            seen = []

            @mcp.tool(name="browser_list_tabs", structured_output=False)
            def browser_list_tabs(browser: str = "Safari") -> dict:
                seen.append(current_run_id())
                return {"ok": True, "tabs": []}

            @mcp.tool(name="computer_plan", structured_output=False)
            async def computer_plan(steps: list[dict]) -> dict:
                return {**await execute_computer_plan(mcp.call_tool, steps=steps), "run_id": current_run_id()}

            steps = [{"id": "a", "tool": "browser_list_tabs", "arguments": {"browser": "Safari"}},
                     {"id": "b", "tool": "browser_list_tabs", "arguments": {"browser": "Safari"}}]
            first = json.loads((await mcp.call_tool("computer_plan", {"steps": steps}))[-1].text)
            second = json.loads((await mcp.call_tool("computer_plan", {"steps": steps}))[-1].text)
            await mcp.call_tool("browser_list_tabs", {"browser": "Safari"})
            return telemetry, first, second, seen

        telemetry, first, second, seen = asyncio.run(run())
        self.assertTrue(run_evidence.valid_run_id(first["run_id"]))
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual([first["run_id"]] * 2 + [second["run_id"]] * 2 + [None], seen)
        rows = telemetry.query_events(hours=1, limit=50, run_id=first["run_id"])
        self.assertEqual(["browser_list_tabs", "browser_list_tabs", "computer_plan"], sorted(row["tool"] for row in rows))
        standalone = [row for row in telemetry.query_events(hours=1, limit=50) if not row.get("run_id")]
        self.assertEqual(["browser_list_tabs"], [row["tool"] for row in standalone])


class EvidenceCaptureTests(RunEvidenceTestCase):
    def capture(self, result, run_id="run_0123456789ab", tool="browser_act"):
        calls = []

        async def call_tool(name, arguments):
            calls.append((name, arguments))
            return result

        hook = run_evidence.make_capture(run_id, call_tool)
        step = {"id": "submit", "tool": tool, "evidence": "both",
                "arguments": {"browser": "Safari", "tab_handle": "btab_x", "app": "Notes"}}
        return asyncio.run(hook(step, "after")), calls

    def test_checkpoint_screenshot_is_kept_owner_only(self) -> None:
        record, calls = self.capture(image_result())
        self.assertEqual(("browser_observe", "viewport", "btab_x"),
                         (calls[0][0], calls[0][1]["visual"], calls[0][1]["tab_handle"]))
        self.assertTrue(record["captured"])
        path = run_evidence.evidence_file("run_0123456789ab", record["file"])
        self.assertEqual(JPEG, path.read_bytes())
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE(path.parent.parent.stat().st_mode))

    def test_password_or_secure_field_on_screen_withholds_the_screenshot(self) -> None:
        for result in (image_result([{"tag": "input", "input_type": "password", "value": "[hidden]"}]),
                       image_result([{"autocomplete": "one-time-code"}]),
                       [SimpleNamespace(type="text", text='{"role": "AXSecureTextField"}'),
                        SimpleNamespace(type="image", data=base64.b64encode(JPEG).decode())]):
            with self.subTest(result=str(result[0].text)[:40]):
                record, _calls = self.capture(result, tool="mac_act")
                self.assertEqual((False, "withheld_sensitive_field_on_screen"), (record["captured"], record["reason"]))
        self.assertEqual([], list((run_evidence.runs_root()).rglob("*.jpg")))

    def test_capture_is_off_unless_the_owner_turned_it_on(self) -> None:
        async def call_tool(name, arguments):
            raise AssertionError("no capture call when evidence is off")

        self.assertIsNone(run_evidence.capture_for_run("run_0123456789ab", call_tool))
        self.evidence_on()
        self.assertIsNotNone(run_evidence.capture_for_run("run_0123456789ab", call_tool))
        self.assertIsNone(run_evidence.capture_for_run(None, call_tool))


class PlanEvidenceHookTests(RunEvidenceTestCase):
    def run_plan(self, steps, hook):
        async def call_tool(name, arguments):
            return {"ok": True}
        return asyncio.run(execute_computer_plan(call_tool, steps=steps, plan_version=2, evidence_capture=hook))

    def test_only_opted_in_steps_are_captured_and_capture_errors_never_stop_the_plan(self) -> None:
        captured = []

        async def hook(step, when):
            captured.append((step["id"], when))
            if step["id"] == "b":
                raise RuntimeError("screen recording permission missing")
            return {"when": when, "captured": True, "file": f"01-{step['id']}-{when}.jpg"}

        result = self.run_plan([
            {"id": "a", "tool": "browser_list_tabs", "arguments": {}, "evidence": "both"},
            {"id": "b", "tool": "browser_list_tabs", "arguments": {}, "evidence": "after"},
            {"id": "c", "tool": "browser_list_tabs", "arguments": {}},
        ], hook)
        self.assertTrue(result["ok"], result)
        self.assertEqual([("a", "before"), ("a", "after"), ("b", "after")], captured)
        steps = {record["id"]: record for record in result["steps"]}
        self.assertEqual(2, len(steps["a"]["evidence"]))
        self.assertEqual("capture_failed: RuntimeError", steps["b"]["evidence"][0]["reason"])
        self.assertNotIn("evidence", steps["c"])
        with self.assertRaises(ComputerPlanError):
            self.run_plan([{"id": "x", "tool": "browser_list_tabs", "arguments": {}, "evidence": "always"}], None)

    def test_the_hook_sees_resolved_ref_arguments(self) -> None:
        seen = []

        async def hook(step, when):
            seen.append(step["arguments"]["tab_handle"])
            return {"when": when, "captured": False, "reason": "test"}

        async def call_tool(name, arguments):
            return {"ok": True, "tab_handle": "btab_from_open"}

        result = asyncio.run(execute_computer_plan(call_tool, plan_version=2, evidence_capture=hook, steps=[
            {"id": "open", "tool": "browser_do", "arguments": {"browser": "Safari", "url": "https://example.com"}},
            {"id": "act", "tool": "browser_act", "evidence": "after", "arguments": {
                "browser": "Safari", "tab_handle": {"$ref": "open.tab_handle"}, "actions": [{"type": "scroll", "dy": 10}]}},
        ]))
        self.assertTrue(result["ok"], result)
        self.assertEqual(["btab_from_open"], seen)


class RunRecordTests(RunEvidenceTestCase):
    PLAN_RESULT = {"ok": False, "reason_code": "STEP_CALL_FAILED", "step_id": "b",
                   "error": "typed sk-live-0123456789abcdefghijklmnopqrstuv into the box",
                   "steps": [{"index": 0, "id": "a", "type": "tool", "tool": "browser_act", "ok": True, "status": "completed",
                              "duration_ms": 120, "result": {"url": "https://example.com/?token=secret"},
                              "evidence": [{"when": "after", "captured": True, "file": "01-a-after.jpg"}]},
                             {"index": 1, "id": "b", "type": "tool", "tool": "browser_act", "ok": False, "status": "failed",
                              "duration_ms": 50, "reason_code": "TARGET_NOT_FOUND"}]}

    def test_record_keeps_ordered_sanitized_steps_and_expires_with_its_screenshots(self) -> None:
        run_id = "run_00000000000a"
        folder = run_evidence.runs_root() / run_id
        folder.mkdir()
        (folder / "01-a-after.jpg").write_bytes(JPEG)
        record = run_evidence.record_run(run_id, kind="recipe", name="Invoice", started_at=time.time() - 3,
                                         result=self.PLAN_RESULT, recipe_id="rcp_0123456789ab")
        self.assertEqual(["a", "b"], [step["id"] for step in record["steps"]])
        self.assertNotIn("result", record["steps"][0], "step payloads are not kept")
        self.assertNotIn("sk-live-0123456789abcdefghijklmnopqrstuv", json.dumps(record))
        self.assertEqual((1, "b"), (record["evidence_count"], record["failed_step"]))
        self.assertEqual(run_id, run_evidence.list_runs()[0]["run_id"])
        self.assertEqual(0, run_evidence.prune(retention_days=7))
        self.assertEqual(1, run_evidence.prune(now=time.time() + 8 * 86400, retention_days=7))
        self.assertIsNone(run_evidence.get_run(run_id))
        self.assertFalse(folder.exists(), "expiry removes the screenshots with the record")

    def test_dashboard_shows_the_run_timeline_and_serves_evidence_only_with_the_token(self) -> None:
        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        from mcp_server.dashboard_routes import create_dashboard_routes
        from mcp_server.security import load_settings

        telemetry = TelemetryManager(db_path=self.root / "telemetry.sqlite3")
        run_id = "run_00000000000b"
        event = telemetry.start_call("mcp", "browser_act", {"actions": []},
                                     metadata={"run_id": run_id, "session_id": "actor:test"})
        telemetry.finish_call(event, result={"ok": True})
        telemetry.record_security_event(session_id="actor:test", event_type="SERVER_RISK_APPROVAL", tool="browser_act",
                                        tool_class="browser", origin=None, decision="granted", reason_code="OWNER_APPROVED",
                                        profile="trusted", actor="test", agent_id=None)
        folder = run_evidence.runs_root() / run_id
        folder.mkdir()
        (folder / "01-a-after.jpg").write_bytes(JPEG)
        run_evidence.record_run(run_id, kind="plan", name=None, started_at=time.time() - 5, result=self.PLAN_RESULT)
        client = TestClient(Starlette(routes=create_dashboard_routes(telemetry, load_settings(), TOKEN)))
        self.assertEqual(401, client.get("/dashboard/api/runs").status_code)
        self.assertEqual([run_id], [row["run_id"] for row in client.get("/dashboard/api/runs", headers=AUTH).json()["runs"]])
        detail = client.get(f"/dashboard/api/runs/detail?run_id={run_id}", headers=AUTH).json()
        self.assertEqual(["browser_act"], [row["tool"] for row in detail["events"]])
        self.assertEqual(["SERVER_RISK_APPROVAL"], [row["event_type"] for row in detail["security_events"]])
        url = f"/dashboard/api/runs/evidence?run_id={run_id}&file=01-a-after.jpg"
        self.assertEqual(401, client.get(url).status_code)
        self.assertEqual(JPEG, client.get(url, headers=AUTH).content)
        self.assertEqual(404, client.get(f"/dashboard/api/runs/evidence?run_id={run_id}&file=../telemetry.sqlite3",
                                         headers=AUTH).status_code)
        saved = client.post("/dashboard/api/runs/settings", json={"capture": True, "retention_days": 1}, headers=AUTH).json()
        self.assertEqual((True, 1), (saved["capture"], saved["retention_days"]))
        self.assertEqual(400, client.post("/dashboard/api/runs/settings", json={"retention_days": 9}, headers=AUTH).status_code)


if __name__ == "__main__":
    unittest.main()
