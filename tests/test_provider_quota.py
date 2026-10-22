from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp_server import provider_quota as quota
from mcp_server.dashboard_routes import create_dashboard_routes
from mcp_server.observability import TelemetryManager
from mcp_server.provider_quota import QuotaCache, QuotaError
from mcp_server.security import load_settings


class QuotaSourceTests(unittest.TestCase):
    def test_providers_map_to_the_plan_they_spend(self) -> None:
        self.assertEqual("codex", quota.quota_source("codex", "gpt-6-luna"))
        self.assertEqual("claude", quota.quota_source("Claude", "haiku"))
        self.assertEqual("opencode-openai", quota.quota_source("opencode", "openai/gpt-6-luna"))
        self.assertEqual("opencode-go", quota.quota_source("opencode", "opencode-go/kimi-k2"))
        # OpenCode Zen and unknown providers have no 5-hour/weekly plan.
        self.assertIsNone(quota.quota_source("opencode", "opencode/big-pickle"))
        self.assertIsNone(quota.quota_source("chatgpt", "GPT-5.6 Sol"))

    def test_team_sources_come_from_running_agents_then_the_team(self) -> None:
        agents = [
            {"team_id": "t1", "status": "running", "provider": "codex", "model": "gpt"},
            {"team_id": "t1", "status": "running", "provider": "claude", "model": "haiku"},
            {"team_id": "t1", "status": "running", "provider": "codex", "model": "gpt"},
            {"team_id": "t1", "status": "completed", "provider": "opencode", "model": "openai/gpt"},
            {"team_id": "t2", "status": "running", "provider": "opencode", "model": "opencode-go/x"},
        ]
        self.assertEqual(["codex", "claude"], quota.team_quota_sources(agents, {"team_id": "t1"}))
        self.assertEqual(["claude"], quota.team_quota_sources([], {"team_id": "t3", "provider": "claude"}))


class ParserTests(unittest.TestCase):
    def test_codex_routes_windows_by_span(self) -> None:
        body = {
            "plan_type": "plus",
            "rate_limit": {
                "primary_window": {"used_percent": 9, "limit_window_seconds": 18000, "reset_at": 1792667027},
                "secondary_window": {"used_percent": 19, "limit_window_seconds": 604800, "reset_at": 1793085793},
            },
        }
        result = quota.parse_codex(body)
        self.assertEqual("plus", result["plan"])
        self.assertEqual(
            [{"kind": "5h", "used_percent": 9.0, "resets_at": 1792667027.0},
             {"kind": "1w", "used_percent": 19.0, "resets_at": 1793085793.0}],
            result["windows"],
        )
        weekly_only = quota.parse_codex({"rate_limit": {
            "primary_window": {"used_percent": 40, "limit_window_seconds": 604800, "reset_at": 1},
            "secondary_window": None,
        }})
        self.assertEqual(["1w"], [window["kind"] for window in weekly_only["windows"]])

    def test_claude_reads_percentages_and_skips_missing_weekly(self) -> None:
        body = {
            "five_hour": {"utilization": 18.0, "resets_at": "2026-10-22T13:00:00.058780+00:00"},
            "seven_day": None,
        }
        result = quota.parse_claude(body)
        self.assertEqual(1, len(result["windows"]))
        window = result["windows"][0]
        self.assertEqual(("5h", 18.0), (window["kind"], window["used_percent"]))
        self.assertAlmostEqual(1792674000.06, window["resets_at"], places=1)
        # 0.5 means half a percent, not 50%.
        self.assertEqual(0.5, quota.parse_claude({"five_hour": {"utilization": 0.5}})["windows"][0]["used_percent"])
        both = quota.parse_claude({"limits": [
            {"kind": "session", "percent": 30, "resets_at": None},
            {"kind": "weekly", "percent": 60, "resets_at": None},
        ]})
        self.assertEqual([("5h", 30.0), ("1w", 60.0)], [(w["kind"], w["used_percent"]) for w in both["windows"]])
        with self.assertRaises(QuotaError) as raised:
            quota.parse_claude({"error": {"type": "rate_limit_error"}})
        self.assertTrue(raised.exception.rate_limited)

    def test_opencode_go_rolling_is_the_five_hour_window(self) -> None:
        result = quota.parse_opencode_go({"usage": {
            "rolling": {"percent": 12.5, "resetsAt": "2026-08-12T02:00:00.000Z"},
            "weekly": {"percent": 40, "resetsAt": "2026-08-18T00:00:00.000Z"},
            "monthly": {"percent": 70},
        }})
        self.assertEqual([("5h", 12.5), ("1w", 40.0)], [(w["kind"], w["used_percent"]) for w in result["windows"]])
        with self.assertRaises(QuotaError):
            quota.parse_opencode_go({"usage": {}})


class CredentialTests(unittest.TestCase):
    def test_expired_claude_login_is_reported_without_a_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
                "accessToken": "tok", "expiresAt": 1000, "subscriptionType": "max",
            }}))
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": tmp, "CLAUDE_CODE_OAUTH_TOKEN": ""}), \
                    patch.object(quota, "_get_json", side_effect=AssertionError("no request")):
                with self.assertRaises(QuotaError) as raised:
                    quota.fetch_claude()
        self.assertEqual("login expired", raised.exception.message)

    def test_codex_sends_the_selected_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "auth.json").write_text(json.dumps({"tokens": {"access_token": "tok", "account_id": "acct"}}))
            seen = {}

            def fake_get(url, headers):
                seen.update(url=url, headers=headers)
                return {"rate_limit": {"primary_window": {"used_percent": 1, "limit_window_seconds": 18000}}}

            with patch.dict(os.environ, {"CODEX_HOME": tmp}), patch.object(quota, "_get_json", side_effect=fake_get):
                result = quota.fetch_codex()
        self.assertEqual("https://chatgpt.com/backend-api/wham/usage", seen["url"])
        self.assertEqual("Bearer tok", seen["headers"]["Authorization"])
        self.assertEqual("acct", seen["headers"]["ChatGPT-Account-Id"])
        self.assertEqual(["5h"], [w["kind"] for w in result["windows"]])

    def test_missing_login_is_not_signed_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"XDG_DATA_HOME": tmp}):
            with self.assertRaises(QuotaError) as raised:
                quota.fetch_opencode_go()
        self.assertEqual("not signed in", raised.exception.message)


class CacheTests(unittest.TestCase):
    def make(self, fetchers):
        self.now = 1000.0
        return QuotaCache(fetchers, clock=lambda: self.now, run_in_background=False)

    def test_reads_are_cached_until_the_source_ttl(self) -> None:
        calls = []

        def codex():
            calls.append(self.now)
            return {"plan": "plus", "windows": [{"kind": "5h", "used_percent": 5.0, "resets_at": None}]}

        cache = self.make({"codex": codex})
        rows = cache.snapshot(["codex", "codex", "unknown"])
        self.assertEqual(1, len(rows))
        self.assertEqual(("codex", "Codex", None), (rows[0]["source"], rows[0]["label"], rows[0]["error"]))
        self.now += 30
        cache.snapshot(["codex"])
        self.assertEqual(1, len(calls))
        self.now += quota.SOURCE_TTL_S["codex"]
        cache.snapshot(["codex"])
        self.assertEqual(2, len(calls))

    def test_transient_error_keeps_the_last_reading_and_auth_error_clears_it(self) -> None:
        outcomes = [
            {"windows": [{"kind": "5h", "used_percent": 20.0, "resets_at": None}]},
            QuotaError("offline"),
            QuotaError("login expired"),
        ]

        def codex():
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        cache = self.make({"codex": codex})
        cache.snapshot(["codex"])
        self.now += 600
        row = cache.snapshot(["codex"])[0]
        self.assertEqual("offline", row["error"])
        self.assertEqual(20.0, row["windows"][0]["used_percent"])
        self.now += 600
        row = cache.snapshot(["codex"])[0]
        self.assertEqual(("login expired", []), (row["error"], row["windows"]))

    def test_claude_rate_limit_waits_out_the_cooldown(self) -> None:
        calls = []

        def claude():
            calls.append(self.now)
            raise QuotaError("rate limited", rate_limited=True)

        cache = self.make({"claude": claude})
        cache.snapshot(["claude"])
        self.now += quota.SOURCE_TTL_S["claude"] + 1
        cache.snapshot(["claude"])
        self.assertEqual(1, len(calls))
        self.now = 1000.0 + quota.CLAUDE_RATE_LIMIT_COOLDOWN_S
        cache.snapshot(["claude"])
        self.assertEqual(2, len(calls))

    def test_teams_quota_covers_running_teams_only(self) -> None:
        cache = self.make({"codex": lambda: {"windows": [{"kind": "5h", "used_percent": 1.0, "resets_at": None}]}})
        agents = [{"team_id": "live", "status": "running", "provider": "codex", "model": "gpt"}]
        teams = [{"team_id": "live", "pending_count": 2}, {"team_id": "done", "pending_count": 0, "provider": "codex"}]
        with patch.object(quota, "get_cache", return_value=cache), \
                patch.dict(os.environ, {"MAC_MCP_PROVIDER_QUOTA": ""}):
            result = quota.teams_quota(agents, teams)
        self.assertEqual(["live"], list(result))
        self.assertEqual("codex", result["live"][0]["source"])
        with patch.object(quota, "get_cache", side_effect=AssertionError("no fetch")):
            self.assertEqual({}, quota.teams_quota(agents, teams))


class AgentsRouteTests(unittest.TestCase):
    def test_each_team_row_carries_its_quota(self) -> None:
        token = "quota-dashboard-token"
        agent = {"agent_id": "agt_1", "team_id": "team_live", "status": "running", "provider": "codex", "model": "gpt"}
        team = {"team_id": "team_live", "status": "running", "pending_count": 1, "provider": "codex", "title": "Live"}
        row = {"source": "codex", "label": "Codex", "plan": "plus", "error": None, "fetched_at": 1.0,
               "windows": [{"kind": "5h", "used_percent": 9.0, "resets_at": 2.0}]}
        with tempfile.TemporaryDirectory() as td:
            telemetry = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
            app = Starlette(routes=create_dashboard_routes(telemetry, load_settings(), token))
            with patch("mcp_server.dashboard_routes.list_agents",
                       return_value={"ok": True, "count": 1, "agents": [agent], "global_admission": None}), \
                    patch("mcp_server.dashboard_routes.dashboard_team_summary", return_value=team), \
                    patch("mcp_server.dashboard_routes.teams_quota", return_value={"team_live": [row]}) as teams_quota:
                payload = TestClient(app).get("/dashboard/api/agents", headers={"authorization": f"Bearer {token}"}).json()
        self.assertEqual([row], payload["teams"][0]["quota"])
        teams_quota.assert_called_once()


if __name__ == "__main__":
    unittest.main()
