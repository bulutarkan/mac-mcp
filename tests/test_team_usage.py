"""Usage › Teams: each team agent is remembered with the team, saved team and member it ran as, and its tokens
come only from the provider usage events joined by agent id: never counted twice, never estimated."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.testclient import TestClient

import mcp_server.provider_usage as provider_usage
import mcp_server.tools_agents as agents
from mcp_server import runtime_settings
from mcp_server.dashboard_routes import create_dashboard_routes
from mcp_server.observability import TelemetryManager
from mcp_server.security import load_settings
from tests import test_team_presets as presets

_SETTINGS_DIR = tempfile.TemporaryDirectory(prefix="mac-mcp-team-usage-settings-")
_SETTINGS_ENV = patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(Path(_SETTINGS_DIR.name) / "settings.json")})


def setUpModule() -> None:
    _SETTINGS_ENV.start()
    runtime_settings._usage_privacy_cache.update({"at": -1.0, "value": None})


def tearDownModule() -> None:
    _SETTINGS_ENV.stop()
    runtime_settings._usage_privacy_cache.update({"at": -1.0, "value": None})
    _SETTINGS_DIR.cleanup()


def query(path: Path, sql: str) -> list:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(sql).fetchall()


NIGHT = {"tp_0000000a": {"name": "Night Shift", "member_ids": ["m_a", "m_b"]}}


def event(store, key: str, agent_id: str, total: int | None, *, provider: str = "codex", at: float | None = None) -> bool:
    return store.ingest(provider_usage.UsageRecord(
        event_key=key, provider=provider, session_id="s-" + agent_id, event_id=key, timestamp=at or time.time(),
        agent_id=agent_id, model=None, model_verified=False, source=provider_usage.SOURCE_REPORT,
        input_tokens=None if total is None else total - 10, output_tokens=None if total is None else 10,
        reasoning_tokens=None, cache_read_tokens=None, cache_write_tokens=None, total_tokens=total,
    ))


def attribution(agent_id: str, team_id: str, *, preset: str | None = "tp_0000000a", member: str = "m_a",
                name: str = "Atlas", started: float | None = None, **extra) -> dict:
    return {"agent_id": agent_id, "team_id": team_id, "team_title": "Night Shift docs", "preset_id": preset,
            "preset_name": "Night Shift" if preset else None, "member_id": member if preset else None,
            "member_name": name if preset else None, "provider": "codex", "model": "gpt-6-luna", "reasoning": "high",
            "role": "coder", "task_id": name.lower(), "task_title": name, "started_at": started or time.time() - 60,
            **extra}


class TeamUsageStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "provider.sqlite3"
        self.store = provider_usage.ProviderUsageStore(self.path)

    def test_preset_team_tokens_by_member_day_and_run_without_double_counting(self) -> None:
        store = self.store
        self.assertTrue(store.record_team_agent(attribution("agt_a", "team_1")))
        self.assertTrue(store.record_team_agent(attribution("agt_b", "team_1", member="m_b", name="Nova",
                                                            role="reviewer")))
        # A second record of the same agent changes nothing, and a replayed event is ignored.
        self.assertFalse(store.record_team_agent(attribution("agt_a", "team_1", member="m_b", name="Other")))
        self.assertTrue(event(store, "e1", "agt_a", 100))
        self.assertTrue(event(store, "e2", "agt_a", 50))
        self.assertFalse(event(store, "e2", "agt_a", 50))
        self.assertTrue(event(store, "e3", "agt_b", 30, provider="claude"))
        # A revision of Atlas in the same run is the same member.
        store.record_team_agent(attribution("agt_a2", "team_1"))
        event(store, "e4", "agt_a2", 20)
        # Delegated usage outside any team is not a team's.
        event(store, "e5", "agt_solo", 1000)
        now = time.time()
        store.finish_team_agent("agt_a", ended_at=now - 40, status="completed")
        store.finish_team_agent("agt_a2", started_at=now - 30, ended_at=now - 20, status="completed")
        store.finish_team_agent("agt_b", ended_at=now - 10, status="completed", gate="pass")

        summary = store.team_summary(days=30, presets=NIGHT)
        self.assertEqual(200, summary["total_tokens"])
        self.assertEqual(1, summary["runs"])
        team = summary["teams"][0]
        self.assertEqual(("tp_0000000a", "Night Shift", True, 200, 1, 1.0),
                         (team["id"], team["name"], team["exists"], team["tokens"], team["runs"], team["share"]))
        self.assertEqual((1, 1), (team["gates"], team["gates_passed"]))
        self.assertEqual([("m_a", "Atlas", 170, 1), ("m_b", "Nova", 30, 1)],
                         [(m["key"], m["name"], m["tokens"], m["runs"]) for m in team["members"]])
        self.assertAlmostEqual(170 / 200, team["members"][0]["share"])
        today = team["daily"][-1]
        self.assertEqual({"m_a": 170, "m_b": 30}, today["members"])
        run = team["run_list"][0]
        self.assertEqual(("team_1", "Night Shift docs", 200), (run["team_id"], run["title"], run["tokens"]))
        self.assertEqual({"members": 2, "completed": 2, "running": 0, "failed": 0, "gates": 1, "gates_passed": 1},
                         run["outcome"])
        atlas = run["members"][0]
        self.assertEqual((2, 170), (atlas["agents"], atlas["tokens"]))
        self.assertAlmostEqual(20 + 10, atlas["duration_s"], delta=2)
        self.assertIsNotNone(run["duration_s"])
        # The provider list says how much of each provider's delegated tokens saved teams used.
        self.assertEqual({"tokens": 1170, "team_tokens": 170}, summary["providers"]["codex"])
        self.assertEqual({"tokens": 30, "team_tokens": 30}, summary["providers"]["claude"])
        provider = store.summary(days=30)["providers"]
        self.assertEqual((170, 30), (provider["codex"]["team_tokens"], provider["claude"]["team_tokens"]))

    def test_a_team_without_a_saved_team_counts_apart(self) -> None:
        self.store.record_team_agent(attribution("agt_x", "team_adhoc", preset=None))
        event(self.store, "e1", "agt_x", 70)
        summary = self.store.team_summary(days=30, presets=NIGHT)
        self.assertEqual([], summary["teams"])
        self.assertEqual(0, summary["total_tokens"])
        self.assertEqual({"tokens": 70, "runs": 1}, summary["other_teams"])
        self.assertEqual(0, summary["providers"]["codex"]["team_tokens"])

    def test_a_deleted_preset_keeps_its_history_and_unread_presets_delete_nothing(self) -> None:
        self.store.record_team_agent(attribution("agt_a", "team_1"))
        event(self.store, "e1", "agt_a", 10)
        gone = self.store.team_summary(days=30, presets={})["teams"]
        self.assertEqual([("tp_0000000a", "Night Shift", False, 10)],
                         [(t["id"], t["name"], t["exists"], t["tokens"]) for t in gone])
        self.assertTrue(self.store.team_summary(days=30, presets=None)["teams"][0]["exists"])
        # A renamed preset shows its current name.
        renamed = self.store.team_summary(days=30, presets={"tp_0000000a": {"name": "Day Shift"}})["teams"][0]
        self.assertEqual("Day Shift", renamed["name"])

    def test_period_retention_and_clear(self) -> None:
        now = time.time()
        self.store.record_team_agent(attribution("agt_old", "team_old", started=now - 100 * 86400))
        event(self.store, "e_old", "agt_old", 500, at=now - 100 * 86400)
        self.store.record_team_agent(attribution("agt_ancient", "team_ancient", started=now - 450 * 86400))
        self.assertEqual(0, self.store.team_summary(days=30, presets=NIGHT)["runs"])
        self.assertEqual(500, self.store.team_summary(days=365, presets=NIGHT)["total_tokens"])
        # The retention prune (400 days at most) drops team rows with the usage they describe.
        ids = [row[0] for row in query(self.path, "SELECT agent_id FROM provider_usage_team_agents")]
        self.assertEqual(["agt_old"], ids)
        removed = self.store.clear()
        self.assertGreaterEqual(removed, 2)
        self.assertEqual([(0,)], query(self.path, "SELECT COUNT(*) FROM provider_usage_team_agents"))

    def test_a_run_still_going_and_a_turn_without_a_count(self) -> None:
        self.store.record_team_agent(attribution("agt_a", "team_1"))
        event(self.store, "e1", "agt_a", None)
        event(self.store, "e2", "agt_a", 5)
        summary = self.store.team_summary(days=30, presets=NIGHT)
        run = summary["teams"][0]["run_list"][0]
        self.assertEqual((5, None, 1), (run["tokens"], run["duration_s"], run["outcome"]["running"]))
        self.assertEqual(1, summary["unreported_turns"])
        self.assertIsNone(summary["teams"][0]["average_duration_s"])

    def test_nothing_is_kept_when_usage_metering_is_off(self) -> None:
        with patch.object(provider_usage, "usage_privacy", return_value={"enabled": False, "retention_days": 365}):
            self.assertFalse(self.store.record_team_agent(attribution("agt_a", "team_1")))
        self.assertEqual([], self.store.team_summary(days=30, presets=NIGHT)["teams"])


class TeamUsageRecordingTests(unittest.TestCase):
    """A real team start (provider CLIs faked as in test_team_presets) records each member, and its end comes
    in with the gate."""

    fake_spawn = presets.TeamPresetSpawnTests.fake_spawn
    spawn = presets.TeamPresetSpawnTests.spawn

    def setUp(self) -> None:
        presets.TeamPresetSpawnTests.setUp(self)
        state = self.root / "state"
        env = patch.dict(os.environ, {"MAC_MCP_STATE_DIR": str(state)})
        env.start()
        self.addCleanup(env.stop)
        self.store = provider_usage.get_store()
        self.assertTrue(str(self.store.path).startswith(str(state)))

    def rows(self) -> dict:
        with closing(sqlite3.connect(self.store.path)) as conn:
            conn.row_factory = sqlite3.Row
            return {row["agent_id"]: dict(row) for row in conn.execute("SELECT * FROM provider_usage_team_agents")}

    def finish(self, agent_id: str, status: str = "completed") -> None:
        meta = agents._read_meta(agent_id)
        agents._write_meta(agent_id, {**meta, "status": status, "ended_at": time.time()})

    def test_members_are_recorded_at_start_and_finished_with_their_gate(self) -> None:
        team = self.spawn([
            {"id": "build", "prompt": "SECRET-PROMPT build it"},
            {"id": "check", "prompt": "SECRET-PROMPT check it", "review_of": "build", "depends_on": ["build"]},
        ], team_preset="Night Shift", title="Night Shift docs")
        rows = self.rows()
        self.assertEqual(1, len(rows))
        build = next(iter(rows.values()))
        self.assertEqual((team["team_id"], "tp_0000000a", "Night Shift", "m_00000001", "Atlas", "codex", "gpt-5.5",
                          "high", "coder", "build", "Night Shift docs"),
                         tuple(build[key] for key in ("team_id", "preset_id", "preset_name", "member_id", "member_name",
                                                      "provider", "model", "reasoning", "role", "task_id", "team_title")))
        self.assertIsNone(build["ended_at"])
        stored = agents._read_team(team["team_id"])
        self.assertEqual({"id": "m_00000001", "name": "Atlas"}, stored["tasks"][0]["preset_member"])

        self.finish(build["agent_id"])
        agents._team_tick(team["team_id"])
        rows = self.rows()
        self.assertEqual("completed", rows[build["agent_id"]]["status"])
        reviewer = next(row for row in rows.values() if row["task_id"] == "check")
        self.assertEqual(("m_00000002", "Nova", "reviewer"), (reviewer["member_id"], reviewer["member_name"], reviewer["role"]))
        with patch.object(agents, "_agent_quality_gate", return_value=("pass", "")):
            self.finish(reviewer["agent_id"])
            agents._team_tick(team["team_id"])
        self.assertEqual(("completed", "pass"), (self.rows()[reviewer["agent_id"]]["status"],
                                                 self.rows()[reviewer["agent_id"]]["gate"]))
        # Labels only: no prompt reaches the usage store.
        with closing(sqlite3.connect(self.store.path)) as conn:
            dump = "\n".join(conn.iterdump())
        self.assertNotIn("SECRET-PROMPT", dump)
        columns = [row[1] for row in query(self.store.path, "PRAGMA table_info(provider_usage_team_agents)")]
        self.assertEqual([], [c for c in columns if any(m in c for m in ("prompt", "result", "content", "secret"))])

    def test_an_ad_hoc_team_is_recorded_without_a_preset_and_retry_keeps_members(self) -> None:
        self.spawn([{"id": "a", "prompt": "x"}], provider="codex")
        (row,) = self.rows().values()
        self.assertIsNone(row["preset_id"])
        self.assertIsNone(row["member_id"])

        team = self.spawn([{"id": "a", "prompt": "x", "member": "Echo"}], team_preset="Night Shift")
        agent_id = agents._read_team(team["team_id"])["tasks"][0]["latest_agent_id"]
        self.finish(agent_id, "failed")
        agents._team_tick(team["team_id"])
        presets.write_settings(self.settings_path, [], default={"provider": "codex"})
        with patch.object(agents, "workflow_for_agent", return_value=None):
            retried = agents.agent_action(load_settings(), "retry", team_id=team["team_id"])
        again = next(row for row in self.rows().values() if row["team_id"] == retried["team_id"])
        self.assertEqual(("tp_0000000a", "m_00000003", "Echo"), (again["preset_id"], again["member_id"], again["member_name"]))

    def test_backfill_names_members_of_a_run_from_before_and_a_scratch_agents_dir_is_never_kept(self) -> None:
        team = self.spawn([{"id": "a", "prompt": "x"}, {"id": "b", "prompt": "y"}], team_preset="Night Shift")
        # A run from before tasks kept their member: no rows, and no preset_member on the tasks.
        with closing(sqlite3.connect(self.store.path)) as conn, conn:
            conn.execute("DELETE FROM provider_usage_team_agents")
        meta = agents._read_team(team["team_id"])
        for task in meta["tasks"]:
            task.pop("preset_member")
            self.finish(task["latest_agent_id"])
        agents._write_team(team["team_id"], meta)
        event(self.store, "e1", meta["tasks"][0]["latest_agent_id"], 42)
        summary = agents.team_usage_summary(30)
        night = summary["teams"][0]
        self.assertEqual(("Night Shift", True, 42), (night["name"], night["exists"], night["tokens"]))
        self.assertEqual(["Atlas", "Nova"], [member["name"] for member in night["members"]])
        self.assertEqual("m_00000001", night["members"][0]["key"])
        self.assertEqual(2, night["run_list"][0]["outcome"]["completed"])

        with patch.dict(os.environ, {"MAC_MCP_STATE_DIR": ""}):
            self.assertFalse(agents._team_usage_kept())
            before = len(self.rows())
            agents._record_team_usage(meta, meta["tasks"][0], "agt_scratch")
        self.assertEqual(before, len(self.rows()))


class TeamUsageRouteTests(unittest.TestCase):
    def test_route_requires_the_dashboard_token_and_returns_the_summary(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = provider_usage.ProviderUsageStore(root / "provider.sqlite3")
            store.record_team_agent(attribution("agt_a", "team_1"))
            event(store, "e1", "agt_a", 900)
            presets.write_settings(root / "settings.json", [presets.NIGHT_SHIFT])
            telemetry = TelemetryManager(db_path=root / "telemetry.sqlite3", usage_enabled=False)
            token = "team-usage-dashboard-token-0123456789"
            calls: list[int] = []

            def summary(days: int) -> dict:
                calls.append(days)
                return store.team_summary(days=days, presets=NIGHT)

            with patch("mcp_server.dashboard_routes.team_usage_summary", side_effect=summary):
                client = TestClient(Starlette(routes=create_dashboard_routes(telemetry, load_settings(), token)))
                self.assertEqual(401, client.get("/dashboard/api/team-usage").status_code)
                self.assertEqual(401, client.get("/dashboard/api/team-usage",
                                                 headers={"authorization": "Bearer wrong"}).status_code)
                response = client.get("/dashboard/api/team-usage?days=90", headers={"authorization": "Bearer " + token})
            self.assertEqual(200, response.status_code)
            self.assertEqual([90], calls)
            body = response.json()
            self.assertEqual((900, "Night Shift"), (body["total_tokens"], body["teams"][0]["name"]))
            for key in ("teams", "other_teams", "providers", "available_since", "days", "unreported_turns"):
                self.assertIn(key, body)
            self.assertNotIn("prompt", json.dumps(body).lower())


if __name__ == "__main__":
    unittest.main()
