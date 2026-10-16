"""#199: run records keep a small skeleton for long and their contents for a chosen, shorter time."""
from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import privacy_inventory, privacy_policy, run_records
from mcp_server import scheduled_task_runner as runner
from mcp_server import scheduled_tasks as store

GITHUB_TOKEN = "ghp_" + "A" * 36


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="mac-mcp-runrec-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.settings_file = self.dir / "settings.json"
        self.settings({})
        env = patch.dict(os.environ, {"MAC_MCP_STATE_DIR": str(self.dir / "state"),
                                       "MAC_MCP_SETTINGS_PATH": str(self.settings_file)})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(privacy_policy._cache.update, {"at": -1.0, "value": None})
        run_records._key_cache.clear()
        self.addCleanup(run_records._key_cache.clear)

    def settings(self, privacy: dict) -> None:
        self.settings_file.write_text(json.dumps({"privacy": privacy}))
        privacy_policy._cache.update({"at": -1.0, "value": None})

    def agent_run(self, *, summary="Leads up 12%.", steps=None, now=None):
        task = store.create_task(store.validate_definition(
            {"name": "Report", "kind": "ai", "prompt": "Summarize.", "schedule": {"type": "daily", "time": "09:00"}}))
        run_id = store.start_run(task["id"], trigger="manual", now=now)
        steps = steps if steps is not None else [
            {"kind": "thinking", "status": "done", "title": "Thinking", "text": "The client Ayşe Yılmaz wants it."},
            {"kind": "command", "status": "done", "title": "Ran a command", "detail": f"curl -H 'Authorization: token {GITHUB_TOKEN}'",
             "input": {"command": "curl …"}, "output": "200 OK"},
            {"kind": "tool", "status": "failed", "title": "read_file", "input": {"path": "/Users/me/a.txt"}, "output": "no"},
        ]
        detail = {"agent_id": "agent_1", "provider": "codex", "model": "gpt-5", "outcome": "success",
                  "usage": {"input_tokens": 10, "output_tokens": 20},
                  "artifacts": [{"ref": "~/Desktop/report.xlsx", "description": "Excel"}], "steps": steps}
        return store.finish_run(run_id, "done", summary=summary, output=f"Report text {GITHUB_TOKEN}",
                                detail=detail, now=now)


class FingerprintTests(Case):
    def test_canonical_keyed_and_versioned(self) -> None:
        a = run_records.fingerprint({"path": "/a", "mode": "r"})
        self.assertEqual(a, run_records.fingerprint({"mode": "r", "path": "/a"}))
        self.assertNotEqual(a, run_records.fingerprint({"path": "/b", "mode": "r"}))
        self.assertRegex(a, r"^v1:[0-9a-f]{24}$")
        key = self.dir / "state" / "state" / "run-fingerprint.key"
        self.assertEqual(0o600, stat.S_IMODE(key.stat().st_mode))
        # A different device (key) gives a different fingerprint for the same input.
        key.write_bytes(os.urandom(32))
        run_records._key_cache.clear()
        self.assertNotEqual(a, run_records.fingerprint({"path": "/a", "mode": "r"}))

    def test_a_broken_or_open_key_file_is_replaced_or_closed(self) -> None:
        key = self.dir / "state" / "state" / "run-fingerprint.key"
        key.parent.mkdir(parents=True, exist_ok=True)
        key.write_bytes(b"")  # a crash between creating and writing it
        first = run_records.fingerprint({"a": 1})
        self.assertGreaterEqual(len(key.read_bytes()), 32)
        self.assertEqual(0o600, stat.S_IMODE(key.stat().st_mode))
        run_records._key_cache.clear()
        self.assertEqual(first, run_records.fingerprint({"a": 1}))  # the replacement is the one kept
        key.chmod(0o644)
        run_records._key_cache.clear()
        run_records.fingerprint({"a": 1})
        self.assertEqual(0o600, stat.S_IMODE(key.stat().st_mode))

    def test_a_plain_hash_of_a_guessable_value_is_not_what_is_stored(self) -> None:
        import hashlib
        value = {"path": "/a"}
        plain = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.assertNotIn(plain[:24], run_records.fingerprint(value))


class StoredRunTests(Case):
    def test_known_secrets_are_masked_before_they_are_written(self) -> None:
        run = self.agent_run(summary=f"Used {GITHUB_TOKEN}")
        stored = json.dumps(store.get_run(run["id"]))
        self.assertNotIn(GITHUB_TOKEN, stored)
        self.assertIn("[REDACTED]", stored)

    def test_steps_and_runs_carry_fingerprints_and_the_same_steps_match(self) -> None:
        first = self.agent_run()
        second = self.agent_run(summary="Different words, same steps.")
        steps = first["detail"]["steps"]
        self.assertTrue(all(step.get("fp", "").startswith("v1:") for step in steps if step.get("input") or step.get("detail")))
        self.assertEqual(first["detail"]["steps_fp"], second["detail"]["steps_fp"])
        changed = self.agent_run(steps=[{"kind": "tool", "status": "done", "title": "read_file", "input": {"path": "/b"}}])
        self.assertNotEqual(first["detail"]["steps_fp"], changed["detail"]["steps_fp"])

    def test_contents_expire_after_the_chosen_days_and_the_skeleton_stays(self) -> None:
        old = time.time() - 40 * 86400
        run = self.agent_run(now=old)
        fps = [step.get("fp") for step in run["detail"]["steps"]]
        self.agent_run()  # any later run applies the retention
        kept = store.get_run(run["id"])
        self.assertIsNone(kept["summary"])
        self.assertIsNone(kept["output"])
        self.assertIsNotNone(kept["details_expired_at"])
        detail = kept["detail"]
        self.assertEqual(("codex", "gpt-5", "success", {"input_tokens": 10, "output_tokens": 20}, 1),
                         (detail["provider"], detail["model"], detail["outcome"], detail["usage"], detail["artifact_count"]))
        self.assertEqual([{"kind": "thinking", "status": "done", "fp": None},
                          {"kind": "command", "status": "done", "fp": fps[1]},
                          {"kind": "tool", "status": "failed", "fp": fps[2]}],
                         [{key: step.get(key) for key in ("kind", "status", "fp")} for step in detail["steps"]])
        rendered = json.dumps(kept)
        for leaked in ("Ayşe", "Leads up", "report.xlsx", "/Users/me/a.txt", "curl", "200 OK"):
            self.assertNotIn(leaked, rendered)
        self.assertEqual(run["detail"]["steps_fp"], detail["steps_fp"])
        self.assertEqual("done", kept["status"])

    def test_contents_expire_without_any_new_run(self) -> None:
        # Paused tasks finish no runs; reading the history must not hand back expired contents.
        run = self.agent_run(now=time.time() - 40 * 86400)
        self.assertIsNone(store.get_run(run["id"])["summary"])
        self.assertFalse(store.list_runs(run["task_id"])[0]["summary"])
        fresh = self.agent_run(now=time.time() - 10 * 86400)
        self.settings({"run_detail_retention_days": 7})
        store.apply_retention()
        conn = sqlite3.connect(store.db_path())
        try:
            summary = conn.execute("SELECT summary FROM runs WHERE id=?", (fresh["id"],)).fetchone()[0]
        finally:
            conn.close()
        self.assertIsNone(summary)

    def test_the_owner_picks_the_period(self) -> None:
        old = time.time() - 40 * 86400
        self.settings({"run_detail_retention_days": 90})
        run = self.agent_run(now=old)
        self.agent_run()
        self.assertEqual("Leads up 12%.", store.get_run(run["id"])["summary"])
        self.settings({"run_detail_retention_days": 7})
        recent = self.agent_run(now=time.time() - 8 * 86400)
        self.agent_run()
        self.assertIsNone(store.get_run(recent["id"])["summary"])

    def test_the_report_endpoint_never_rebuilds_expired_steps_from_agent_files(self) -> None:
        from starlette.applications import Starlette
        from starlette.testclient import TestClient
        from mcp_server.dashboard_routes import create_dashboard_routes
        from mcp_server.observability import TelemetryManager
        from mcp_server.security import load_settings
        run = self.agent_run(now=time.time() - 40 * 86400)
        self.agent_run()
        token = "dashboard-test-token-0123456789-abcdefghijklmnopqrstuvwxyz"
        telemetry = TelemetryManager(db_path=self.dir / "t.sqlite3", usage_enabled=False, async_writes=False)
        app = Starlette(routes=create_dashboard_routes(telemetry, load_settings(), dashboard_token=token))
        with patch.object(runner, "_agent_steps", side_effect=AssertionError("must not read agent files")), \
                TestClient(app) as client:
            body = client.get(f"/dashboard/api/scheduled-tasks/run?run_id={run['id']}",
                              headers={"authorization": f"Bearer {token}"}).json()
        self.assertIsNotNone(body["run"]["details_expired_at"])

    def test_older_databases_gain_the_column(self) -> None:
        run = self.agent_run()
        conn = sqlite3.connect(store.db_path())
        conn.execute("ALTER TABLE runs DROP COLUMN details_expired_at")
        conn.commit()
        conn.close()
        store._DB_IDENTITIES.clear()
        self.assertIsNone(store.get_run(run["id"])["details_expired_at"])


class PrivacySurfaceTests(Case):
    def test_retention_choice_is_snapped_and_listed(self) -> None:
        from mcp_server.runtime_settings import run_detail_privacy
        self.assertEqual(30, run_detail_privacy()["retention_days"])
        self.settings({"run_detail_retention_days": 12})
        self.assertEqual(7, run_detail_privacy()["retention_days"])
        rows = {row["key"]: row for row in privacy_inventory.stores(measure=False)}
        self.assertIn("contents 7 days", rows["scheduled_tasks"]["kept"])
        self.assertNotEqual("until deleted", rows["agents"]["kept"])  # agent folders expire (#22)

    def test_settings_endpoint_accepts_only_the_listed_periods(self) -> None:
        from starlette.applications import Starlette
        from starlette.testclient import TestClient
        from mcp_server.dashboard_routes import create_dashboard_routes
        from mcp_server.observability import TelemetryManager
        from mcp_server.security import load_settings
        token = "dashboard-test-token-0123456789-abcdefghijklmnopqrstuvwxyz"
        telemetry = TelemetryManager(db_path=self.dir / "t.sqlite3", usage_enabled=False, async_writes=False)
        app = Starlette(routes=create_dashboard_routes(telemetry, load_settings(), dashboard_token=token))
        auth = {"authorization": f"Bearer {token}"}
        with TestClient(app) as client:
            bad = client.post("/dashboard/api/privacy/settings", json={"run_detail_retention_days": 12}, headers=auth)
            good = client.post("/dashboard/api/privacy/settings", json={"run_detail_retention_days": 90}, headers=auth)
            overview = client.get("/dashboard/api/privacy", headers=auth).json()
        self.assertEqual(400, bad.status_code)
        self.assertEqual(200, good.status_code)
        self.assertEqual(90, overview["run_detail_retention_days"])


class RecoveredRunTests(Case):
    def test_a_run_followed_after_a_restart_is_marked_private_too(self) -> None:
        task = store.create_task(store.validate_definition(
            {"name": "Report", "kind": "ai", "prompt": "Summarize.", "schedule": {"type": "daily", "time": "09:00"}}))
        run_id = store.start_run(task["id"], trigger="manual")
        self.settings({"private_mode": True})
        with patch.object(runner, "wait_for_agent", return_value={"status": "done", "summary": "secret", "detail": {"a": 1}}):
            runner.Scheduler(settings=None)._follow_agent(run_id, "agent_1", time.time() + 5)
        run = store.get_run(run_id)
        self.assertEqual(({"private_mode": True}, None), (run["detail"], run["summary"]))



class NativeSurfaceTests(unittest.TestCase):
    def test_privacy_pane_picker_and_the_report_note(self) -> None:
        sources = Path(__file__).resolve().parents[1] / "menu_app" / "Sources"
        pane = (sources / "PrivacyPane.swift").read_text(encoding="utf-8")
        self.assertIn('save(["run_detail_retention_days": days])', pane)
        self.assertIn('object["run_detail_retention_choices"]', pane)
        report = (sources / "RunReportView.swift").read_text(encoding="utf-8")
        self.assertIn("shown.detailsExpiredAt != nil", report)
        self.assertIn("isAgent && !steps.isEmpty && shown.detailsExpiredAt == nil", report)


if __name__ == "__main__":
    unittest.main()
