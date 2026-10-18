"""Team History (#263): past agent teams as counts only, for Settings › Agents."""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp_server import provider_usage
from mcp_server import tools_agents as agents
from mcp_server.dashboard_routes import create_dashboard_routes
from mcp_server.observability import TelemetryManager
from mcp_server.security import load_settings

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "menu_app" / "Sources"
TOKEN = "team-history-dashboard-token-0123456789"
AUTH = {"authorization": f"Bearer {TOKEN}"}
SECRET_PROMPT = "Summarise the patient file of Jane Doe for the board"


class HistoryFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.patches = [
            patch.object(agents, "AGENTS_DIR", root / "agents"),
            patch.object(agents, "TEAMS_DIR", root / "teams"),
        ]
        for item in self.patches:
            item.start()
        agents.AGENTS_DIR.mkdir(parents=True)
        agents.TEAMS_DIR.mkdir(parents=True)

    def tearDown(self) -> None:
        for item in self.patches:
            item.stop()
        self.temp.cleanup()

    def agent(self, agent_id: str, *, status: str = "completed", provider: str = "codex", tool_calls: int = 3,
              total_tokens: int | None = 1000, started: float, seconds: float = 10.0) -> None:
        folder = agents.AGENTS_DIR / agent_id
        folder.mkdir(parents=True)
        (folder / "prompt.txt").write_text(SECRET_PROMPT, encoding="utf-8")
        meta = {
            "agent_id": agent_id, "provider": provider, "model": "gpt-test", "status": status,
            "result_outcome": "success" if status == "completed" else None,
            "title": SECRET_PROMPT, "started_at": started,
            "ended_at": started + seconds if status in agents.TERMINAL_STATUSES else None,
            "tool_call_count": tool_calls, "turn_count": 1,
        }
        if total_tokens is not None:
            meta["usage_cumulative"] = {"total_tokens": total_tokens}
        (folder / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

    def team(self, team_id: str, *, title: str, created: float, members: dict[str, str]) -> None:
        folder = agents.TEAMS_DIR / team_id
        folder.mkdir(parents=True)
        meta = {
            "team_id": team_id, "title": title, "provider": "codex", "model": "gpt-test",
            "created_at": created, "updated_at": created, "agent_ids": list(members),
            "tasks": [
                {"id": f"task_{index}", "role": role, "title": SECRET_PROMPT, "state": "completed", "agent_ids": [agent_id]}
                for index, (agent_id, role) in enumerate(members.items(), start=1)
            ],
        }
        (folder / "meta.json").write_text(json.dumps(meta), encoding="utf-8")


class TeamHistoryTests(HistoryFixture):
    def test_teams_newest_first_with_counts_and_no_prompt_text(self) -> None:
        now = time.time()
        self.agent("agt_old", started=now - 500)
        self.team("team_old", title="Older review", created=now - 600, members={"agt_old": "reader"})
        self.agent("agt_a", started=now - 100, tool_calls=7, total_tokens=4000, seconds=30)
        self.agent("agt_b", started=now - 100, tool_calls=2, total_tokens=None, provider="opencode", seconds=12)
        self.team("team_new", title="Repo   review\nfor roadmap", created=now - 120,
                  members={"agt_a": "security", "agt_b": "performance"})

        with patch.object(provider_usage, "agent_totals", return_value={}):
            history = agents.team_history(limit=10)

        self.assertEqual(["team_new", "team_old"], [team["team_id"] for team in history["teams"]])
        self.assertEqual(2, history["total"])
        newest = history["teams"][0]
        self.assertEqual("Repo review for roadmap", newest["title"])  # one line
        self.assertEqual(2, newest["agent_count"])
        self.assertEqual(9, newest["tool_calls"])
        self.assertEqual(["security", "performance"], [agent["label"] for agent in newest["agents"]])
        self.assertEqual(30000, newest["agents"][0]["duration_ms"])
        # An agent whose tool reported no usage is unknown (null), not zero, and stays out of the total.
        self.assertIsNone(newest["agents"][1]["tokens"]["total"])
        self.assertEqual(4000, newest["tokens"]["total"])
        self.assertEqual(1, newest["tokens_unknown_agents"])
        self.assertIn("retention_days", history)
        blob = json.dumps(history)
        self.assertNotIn("patient", blob)
        self.assertNotIn("Jane", blob)

    def test_paging_and_bounds(self) -> None:
        now = time.time()
        for index in range(5):
            self.agent(f"agt_{index}", started=now - index)
            self.team(f"team_{index}", title=f"Team {index}", created=now - index, members={f"agt_{index}": "r"})
        with patch.object(provider_usage, "agent_totals", return_value={}):
            page = agents.team_history(limit=2, offset=2)
            huge = agents.team_history(limit=500)
        self.assertEqual(["team_2", "team_3"], [team["team_id"] for team in page["teams"]])
        self.assertEqual(5, page["total"])
        self.assertEqual(50, huge["limit"])

    def test_token_kinds_come_from_usage_rows(self) -> None:
        now = time.time()
        self.agent("agt_a", started=now - 10, total_tokens=10)
        self.team("team_a", title="T", created=now - 20, members={"agt_a": "r"})
        rows = {"agt_a": {"input": 900, "output": 100, "reasoning": None, "cache_read": 700, "cache_write": None, "total": 1000}}
        with patch.object(provider_usage, "agent_totals", return_value=rows):
            agent = agents.team_history()["teams"][0]["agents"][0]
        self.assertEqual(1000, agent["tokens"]["total"])
        self.assertEqual(700, agent["tokens"]["cache_read"])
        self.assertIsNone(agent["tokens"]["reasoning"])

    def test_secrets_in_a_team_title_are_redacted(self) -> None:
        now = time.time()
        self.agent("agt_a", started=now - 10)
        self.team("team_a", title="Deploy with sk-ant-api03-" + "a" * 40, created=now - 20, members={"agt_a": "r"})
        with patch.object(provider_usage, "agent_totals", return_value={}):
            title = agents.team_history()["teams"][0]["title"]
        self.assertNotIn("a" * 40, title)

    def test_no_teams(self) -> None:
        history = agents.team_history()
        self.assertEqual([], history["teams"])
        self.assertEqual(0, history["total"])


class AgentTotalsTests(unittest.TestCase):
    def test_sums_by_agent_and_marks_unreported_kinds_none(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = provider_usage.ProviderUsageStore(Path(folder) / "usage.sqlite3")
            with closing(store._connect()) as conn, conn:
                store._schema(conn)
                for key, agent_id, inp, out, cache in (("e1", "agt_a", 100, 10, 50), ("e2", "agt_a", 200, 20, None),
                                                       ("e3", "agt_b", 5, 1, None)):
                    conn.execute(
                        "INSERT INTO provider_usage_events (event_key, provider, session_id, event_id, timestamp, "
                        "local_date, timezone, agent_id, source, schema_version, input_tokens, output_tokens, "
                        "cache_read_tokens, total_tokens) VALUES (?, 'codex', 's', ?, 0, '2026-10-18', 'UTC', ?, "
                        "'report', 1, ?, ?, ?, ?)",
                        (key, key, agent_id, inp, out, cache, inp + out),
                    )
            with patch.object(provider_usage, "get_store", return_value=store):
                totals = provider_usage.agent_totals(["agt_a", "agt_b", "agt_missing"])
        self.assertEqual({"agt_a", "agt_b"}, set(totals))
        self.assertEqual(300, totals["agt_a"]["input"])
        self.assertEqual(50, totals["agt_a"]["cache_read"])
        self.assertIsNone(totals["agt_b"]["cache_read"])
        self.assertIsNone(totals["agt_a"]["reasoning"])
        self.assertEqual({}, provider_usage.agent_totals([]))


class TeamHistoryEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"MAC_MCP_STATE_DIR": str(Path(self.temp.name) / "state")}, clear=False)
        self.env.start()
        telemetry = TelemetryManager(db_path=Path(self.temp.name) / "telemetry.sqlite3", max_events=10)
        app = Starlette(routes=create_dashboard_routes(telemetry, load_settings(), TOKEN))
        self.client = TestClient(app, base_url="http://127.0.0.1")

    def tearDown(self) -> None:
        self.env.stop()
        self.temp.cleanup()

    def test_requires_the_owner_token(self) -> None:
        response = self.client.get("/dashboard/api/agents/history")
        self.assertIn(response.status_code, {401, 403})

    def test_returns_history_with_bounded_paging(self) -> None:
        payload = {"ok": True, "teams": [], "total": 0, "limit": 50, "offset": 0, "retention_days": 30}
        with patch("mcp_server.dashboard_routes.team_history", return_value=payload) as history:
            response = self.client.get("/dashboard/api/agents/history?limit=999&offset=-4", headers=AUTH)
        self.assertEqual(200, response.status_code)
        self.assertEqual(payload, response.json())
        history.assert_called_once_with(limit=50, offset=0)


class TeamHistoryAppSourceTests(unittest.TestCase):
    def test_agents_pane_has_providers_and_team_history_sections(self) -> None:
        settings = (SOURCES / "SettingsView.swift").read_text(encoding="utf-8")
        self.assertIn('AgentsFoldingSection(title: "Providers")', settings)
        self.assertIn('AgentsFoldingSection(title: "Team History")', settings)
        self.assertIn("refreshTeamHistory", settings)

    def test_section_respects_reduce_motion_and_reads_numbers_to_voiceover(self) -> None:
        source = (SOURCES / "TeamHistorySection.swift").read_text(encoding="utf-8")
        self.assertEqual(2, source.count("@Environment(\\.accessibilityReduceMotion)"))
        self.assertGreaterEqual(source.count(".accessibilityLabel("), 3)
        self.assertIn("No agent team has run yet.", source)
        self.assertIn("Tokens not reported", source)
        self.assertNotRegex(source, re.compile(r"(?<![A-Za-z0-9_])@State\b"))

    def test_build_script_and_search_know_the_section(self) -> None:
        script = (ROOT / "menu_app" / "build_app.sh").read_text(encoding="utf-8")
        self.assertIn('"${SCRIPT_DIR}/Sources/TeamHistorySection.swift"', script)
        search = (SOURCES / "SettingsSearch.swift").read_text(encoding="utf-8")
        self.assertIn("team history", search)


if __name__ == "__main__":
    unittest.main()
