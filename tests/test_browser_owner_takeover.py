"""Take over (#124): the owner takes a browser tab from Mac MCP and no caller writes to it again."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp_server import browser_activity, browser_tabs, workspace_arbitration
from mcp_server.dashboard_routes import create_dashboard_routes
from mcp_server.observability import TelemetryManager
from mcp_server.policy import PolicyContext, reset_policy_context, set_policy_context
from mcp_server.security import load_settings
from mcp_server.tools_browser_agent import _browser_act_locked

DASHBOARD_TOKEN = "test-dashboard-token-124"
DASHBOARD_AUTH = {"Authorization": f"Bearer {DASHBOARD_TOKEN}"}
HANDLE = "btab_safari_owner124"
ROW = {
    "browser": "Safari", "window_index": 1, "tab_index": 2, "tab_handle": HANDLE,
    "native_id": "4242", "title": "Shop", "url": "https://shop.example.test/cart", "active": False,
}


def _agent_context(agent_id: str = "agt_one", team_id: str = "team_one"):
    return set_policy_context(PolicyContext(
        profile="trusted", actor=f"agent:{agent_id}", agent_id=agent_id, team_id=team_id,
    ))


def _lease(agent_id: str = "agt_one", generation: int = 7) -> dict:
    now = time.time()
    return {
        "owner": f"agent:{agent_id}", "agent_id": agent_id, "team_id": "team_one", "profile": "trusted",
        "origin": "https://shop.example.test", "generation": generation,
        "created_at": now, "last_seen_at": now, "expires_at": now + 300,
    }


class OwnerTakeoverTests(unittest.TestCase):
    def setUp(self) -> None:
        browser_tabs._REGISTRY.clear()
        browser_tabs._LOGICAL_LEASES.clear()
        browser_tabs._LEASE_HISTORY.clear()
        workspace_arbitration._OWNER_TAKEOVERS.clear()
        self.addCleanup(workspace_arbitration._OWNER_TAKEOVERS.clear)
        self.addCleanup(browser_tabs._LOGICAL_LEASES.clear)

    def test_take_over_ends_the_agent_lease_and_refuses_every_later_lease(self) -> None:
        browser_tabs._LOGICAL_LEASES[HANDLE] = _lease()
        record = browser_tabs.take_over("Safari", HANDLE)
        self.assertEqual("agt_one", record["agent_id"])
        self.assertNotIn(HANDLE, browser_tabs.logical_lease_snapshot())

        token = _agent_context()
        try:
            with patch.object(browser_tabs, "resolve_tab", return_value=(1, 2, dict(ROW))) as resolve:
                for mutation in (True, False):
                    with self.assertRaises(HTTPException) as caught:
                        with browser_tabs.tab_lease("Safari", tab_handle=HANDLE, mutation=mutation):
                            self.fail("the agent must not reach a taken-over tab")
                    detail = caught.exception.detail
                    self.assertEqual(409, caught.exception.status_code)
                    self.assertEqual("OWNER_TOOK_OVER", detail["reason_code"])
                    self.assertFalse(detail["retryable"])
                    self.assertIn("took over", detail["message"])
            resolve.assert_not_called()
        finally:
            reset_policy_context(token)

    def test_a_chat_without_agent_identity_is_refused_too(self) -> None:
        browser_tabs.take_over("Safari", HANDLE)
        with self.assertRaises(HTTPException) as caught:
            with browser_tabs.tab_lease("Safari", tab_handle=HANDLE, mutation=True):
                pass
        self.assertEqual("OWNER_TOOK_OVER", caught.exception.detail["reason_code"])
        fresh, blocked = browser_tabs.revalidate_mutation_lease("Safari", HANDLE, None)
        self.assertIsNone(fresh)
        self.assertEqual("OWNER_TOOK_OVER", blocked["reason_code"])

    def test_owner_clicks_still_reach_the_tab_and_hand_back_frees_it(self) -> None:
        browser_tabs.take_over("Safari", HANDLE)
        with patch.object(browser_tabs, "resolve_tab", return_value=(1, 2, dict(ROW))):
            with workspace_arbitration.owner_action():
                with browser_tabs.tab_lease("Safari", tab_handle=HANDLE, mutation=True) as target:
                    self.assertEqual(HANDLE, target.tab_handle)
            self.assertTrue(browser_tabs.hand_back(HANDLE))
            with browser_tabs.tab_lease("Safari", tab_handle=HANDLE, mutation=True) as target:
                self.assertEqual(HANDLE, target.tab_handle)
        self.assertFalse(browser_tabs.hand_back(HANDLE))

    def test_delegated_batch_stops_before_its_next_write_after_take_over(self) -> None:
        browser_tabs._LOGICAL_LEASES[HANDLE] = _lease()
        written: list[str] = []

        def page(*args, **kwargs):
            # The owner clicks Take over while the first action runs.
            action = "click" if not written else "type"
            written.append(action)
            if action == "click":
                browser_tabs.take_over("Safari", HANDLE)
            return {
                "ok": True,
                "actions": [{"ok": True, "type": action, "element_id": "e1", "effect_observed": True,
                             "verification": "state_changed"}],
                "state": {"ok": True, "url": ROW["url"], "title": "Shop", "dom_revision": 2},
            }

        token = _agent_context()
        try:
            with patch("mcp_server.tools_browser_agent._try_fused_action", return_value={"js_calls": 0}), \
                 patch("mcp_server.tools_browser_agent._resolve_tab_target", return_value=(1, 2)), \
                 patch("mcp_server.tools_browser_agent._wait_for_element_readiness", return_value={
                     "ready": True, "element_id": "e1", "stable_for_ms": 300, "dom_revision": 1,
                     "rect": {"x": 1, "y": 1, "w": 9, "h": 9}, "_js_calls": 1}), \
                 patch.object(browser_tabs, "resolve_tab", return_value=(1, 2, dict(ROW))), \
                 patch("mcp_server.tools_browser_agent._run_json_js", side_effect=page):
                result = _browser_act_locked(
                    load_settings(), "Safari",
                    [{"type": "click", "element_id": "e1"},
                     {"type": "type", "element_id": "e1", "text": "agent text"}],
                    window_index=1, tab_index=2, tab_handle=HANDLE, lease_generation=7, return_state="compact",
                )
        finally:
            reset_policy_context(token)

        self.assertEqual(["click"], written, "nothing may be written to the tab after Take over")
        self.assertFalse(result["ok"])
        self.assertEqual("OWNER_TOOK_OVER", result["reason_code"])
        self.assertFalse(result["automatic_retry"])

    def test_chat_batch_stops_before_its_next_write_after_take_over(self) -> None:
        written: list[str] = []

        def page(*args, **kwargs):
            action = "click" if not written else "type"
            written.append(action)
            if action == "click":
                browser_tabs.take_over("Safari", HANDLE)
            return {
                "ok": True,
                "actions": [{"ok": True, "type": action, "element_id": "e1", "effect_observed": True,
                             "verification": "state_changed"}],
                "state": {"ok": True, "url": ROW["url"], "title": "Shop", "dom_revision": 2},
            }

        with patch("mcp_server.tools_browser_agent._try_fused_action", return_value={"js_calls": 0}), \
             patch("mcp_server.tools_browser_agent._resolve_tab_target", return_value=(1, 2)), \
             patch("mcp_server.tools_browser_agent._wait_for_element_readiness", return_value={
                 "ready": True, "element_id": "e1", "stable_for_ms": 300, "dom_revision": 1,
                 "rect": {"x": 1, "y": 1, "w": 9, "h": 9}, "_js_calls": 1}), \
             patch("mcp_server.tools_browser_agent._run_json_js", side_effect=page):
            result = _browser_act_locked(
                load_settings(), "Safari",
                [{"type": "click", "element_id": "e1"},
                 {"type": "type", "element_id": "e1", "text": "chat text"}],
                window_index=1, tab_index=2, tab_handle=HANDLE, lease_generation=0, return_state="compact",
            )
        self.assertEqual(["click"], written)
        self.assertEqual("OWNER_TOOK_OVER", result["reason_code"])

    def test_safari_process_swap_carries_the_takeover_to_the_new_handle(self) -> None:
        browser_tabs._REGISTRY[HANDLE] = dict(ROW)
        browser_tabs.take_over("Safari", HANDLE)
        swapped = [dict(ROW, native_id="9999", tab_handle=None)]
        swapped[0].pop("tab_handle")
        with patch.object(browser_tabs, "_scan", return_value=swapped):
            rows = browser_tabs.list_tabs("Safari")
        new_handle = rows[0]["tab_handle"]
        self.assertNotEqual(HANDLE, new_handle)
        taken = workspace_arbitration.owner_takeover_snapshot()
        self.assertIn(new_handle, taken)
        self.assertNotIn(HANDLE, taken)
        # A closed tab ends its takeover.
        with patch.object(browser_tabs, "_scan", return_value=[]):
            browser_tabs.list_tabs("Safari")
        self.assertEqual({}, workspace_arbitration.owner_takeover_snapshot())


class ActiveTabRowsTests(unittest.TestCase):
    def test_two_agents_in_two_tabs_get_two_named_rows(self) -> None:
        now = time.time()
        labels = {"agt_a": {"who": "Codex agent", "task": "Compare prices"},
                  "agt_b": {"who": "Research team", "task": "Read reviews"}}
        rows = browser_activity.active_tab_rows(
            events=[
                {"tool": "browser_act", "status": "running", "timestamp": now, "agent_id": "agt_a",
                 "_ctx": {"browser": "Safari", "tab_handle": "btab_safari_a", "site": "a.test", "action": "Act"}},
                {"tool": "browser_observe", "status": "success", "timestamp": now - 3, "agent_id": "agt_b",
                 "team_id": "team_b",
                 "_ctx": {"browser": "Google Chrome", "tab_handle": "btab_chrome_b", "site": "b.test", "action": "Observe"}},
                {"tool": "browser_act", "status": "success", "timestamp": now - 600, "agent_id": "agt_old",
                 "_ctx": {"browser": "Safari", "tab_handle": "btab_safari_old"}},
            ],
            leases={"btab_chrome_b": {"agent_id": "agt_b", "team_id": "team_b", "last_seen_at": now - 1,
                                      "origin": "https://b.test"}},
            takeovers={},
            registry={},
            context_for=lambda event: event.get("_ctx"),
            now=now,
            label_for=lambda agent, team: labels.get(agent, {"who": None, "task": None}),
        )
        self.assertEqual(["btab_safari_a", "btab_chrome_b"], [row["tab_handle"] for row in rows])
        self.assertEqual(("Codex agent", "Compare prices"), (rows[0]["who"], rows[0]["task"]))
        self.assertEqual(("Research team", "Read reviews"), (rows[1]["who"], rows[1]["task"]))
        self.assertTrue(rows[0]["running"])
        self.assertTrue(all(row["can_stop"] for row in rows))

    def test_taken_over_tab_shows_as_yours_and_cannot_be_stopped(self) -> None:
        now = time.time()
        rows = browser_activity.active_tab_rows(
            events=[], leases={},
            takeovers={"btab_safari_x": {"browser": "Safari", "agent_id": "agt_a", "taken_at": now}},
            registry={"btab_safari_x": {"browser": "Safari", "url": "https://www.shop.test/cart"}},
            context_for=lambda event: None, now=now,
            label_for=lambda agent, team: {"who": "Codex agent", "task": "Buy"},
        )
        self.assertEqual(1, len(rows))
        self.assertTrue(rows[0]["taken_over"])
        self.assertFalse(rows[0]["can_stop"])
        self.assertEqual("shop.test", rows[0]["site"])

    def test_overlay_label_is_short_and_free_of_links_paths_and_addresses(self) -> None:
        with patch("mcp_server.tools_agents._read_meta", return_value={
            "provider": "codex", "title": "Email bob@example.com the report at /Users/me/x.pdf from https://a.test/p",
        }):
            browser_activity._LABEL_CACHE.clear()
            label = browser_activity.agent_label("agt_label")
        self.assertEqual("Codex agent", label["who"])
        for leaked in ("bob@example.com", "/Users/me", "https://"):
            self.assertNotIn(leaked, label["task"])
        self.assertLessEqual(len(label["task"]), 48)
        token = _agent_context("agt_label", "")
        try:
            prefix = browser_activity.visual_who_prefix(("Safari", "1", ""))
        finally:
            reset_policy_context(token)
        self.assertEqual("window.__mcpVisualWho=\"Codex agent\";\n", prefix)  # the page never sees the task
        # The next chat script on that document clears the name once, then leaves scripts alone.
        self.assertEqual("window.__mcpVisualWho=null;\n", browser_activity.visual_who_prefix(("Safari", "1", "")))
        self.assertEqual("", browser_activity.visual_who_prefix(("Safari", "1", "")))


class TakeOverRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        workspace_arbitration._OWNER_TAKEOVERS.clear()
        browser_tabs._LOGICAL_LEASES.clear()
        self.addCleanup(workspace_arbitration._OWNER_TAKEOVERS.clear)

    def test_take_over_needs_no_confirmation_brings_the_tab_forward_and_lists_it(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            manager = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
            app = Starlette(routes=create_dashboard_routes(manager, load_settings(), DASHBOARD_TOKEN))
            client = TestClient(app)

            def activate(*args, **kwargs):
                # The owner's own Show Tab runs although the tab is now taken over.
                self.assertIsNone(workspace_arbitration.owner_takeover_block(HANDLE))
                return {"ok": True, "browser": "Safari", "tab_handle": HANDLE}

            with patch("mcp_server.dashboard_routes.browser_activate_tab", side_effect=activate) as shown:
                response = client.post("/dashboard/api/browser/take-over",
                                       json={"browser": "Safari", "tab_handle": HANDLE, "who": "Chat 2"},
                                       headers=DASHBOARD_AUTH)
            self.assertEqual(200, response.status_code)
            self.assertTrue(response.json()["shown"])
            shown.assert_called_once()
            self.assertIsNotNone(workspace_arbitration.owner_takeover_block(HANDLE))

            tabs = client.get("/dashboard/api/browser/active-tabs", headers=DASHBOARD_AUTH).json()["tabs"]
            self.assertEqual([HANDLE], [row["tab_handle"] for row in tabs])
            self.assertTrue(tabs[0]["taken_over"])
            self.assertEqual("Chat 2", tabs[0]["who"])

            released = client.post("/dashboard/api/browser/hand-back", json={"tab_handle": HANDLE},
                                   headers=DASHBOARD_AUTH).json()
            self.assertTrue(released["released"])
            self.assertIsNone(workspace_arbitration.owner_takeover_block(HANDLE))

    def test_take_over_is_localhost_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            manager = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
            app = Starlette(routes=create_dashboard_routes(manager, load_settings(), DASHBOARD_TOKEN))
            response = TestClient(app).post(
                "/dashboard/api/browser/take-over", json={"browser": "Safari", "tab_handle": HANDLE},
                headers={**DASHBOARD_AUTH, "x-forwarded-for": "8.8.8.8"},
            )
        self.assertEqual(403, response.status_code)
        self.assertEqual({}, workspace_arbitration.owner_takeover_snapshot())


ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "menu_app/Sources"


class NativeAndOverlaySurfaceTests(unittest.TestCase):
    """The #124 surfaces: menu rows, notification titles, glow cadence, idle rail, cursor text."""

    def test_menu_has_one_row_per_tab_with_take_over_show_and_stop(self) -> None:
        menu = (SOURCES / "MenuBarView.swift").read_text(encoding="utf-8")
        card = menu[menu.index("private func browserTabsCard("):menu.index("private func browserActivityLabel(")]
        self.assertIn("ForEach(tabs) { tab in browserTabRow(tab) }", card)
        for control in ('Button("Take over")', 'Button("Hand back")', "state.showBrowserTab(tab)",
                        "state.stopBrowserTabAgent(tab)", "state.browserTabTitle(tab)"):
            self.assertIn(control, card)
        # No confirmation dialog in front of Take over.
        self.assertNotIn("confirmationDialog", card)
        self.assertNotIn(".alert(", card)

    def test_notification_titles_say_how_it_ended_and_stay_quiet_in_view(self) -> None:
        source = (SOURCES / "AgentNotificationController.swift").read_text(encoding="utf-8")
        for title in ("finished", "failed", "timed out", "is stuck", "ran out of budget"):
            self.assertIn(title, source)
        self.assertIn('case "timeout": return .timedOut', source)
        self.assertIn('case "stalled": return .stuck', source)
        self.assertIn("content.interruptionLevel = .passive", source)
        self.assertIn("completionHandler(quiet ? [.list] : [.banner])", source)
        self.assertNotIn('"Agent needs attention"', source)

    def test_glow_follows_the_tab_sparsely(self) -> None:
        pulse = (SOURCES / "BrowserTabPulse.swift").read_text(encoding="utf-8")
        self.assertIn("static let followSeconds: Double = 0.6", pulse)

    def test_rail_folds_to_an_edge_marker_when_idle_in_both_companions(self) -> None:
        for name in ("BrowserVisualCompanion", "ChromeVisualCompanion"):
            source = (ROOT / "menu_app" / name / "visual.js").read_text(encoding="utf-8")
            self.assertIn(".companion.idle:not(.sidebar-open) .history-rail { width: 5px;", source)
            finish = source[source.index("const finish = () => {"):source.index("hideTimer = setTimeout(finish")]
            self.assertIn("companion.classList.add('idle')", finish)
            self.assertIn("companion.classList.remove('idle')", source)
            # The overlay names the agent with text only, never markup.
            self.assertIn("ui.querySelector('.history-title').textContent = who.name || 'Mac MCP'", source)

    def test_desktop_cursor_text_matches_its_behaviour(self) -> None:
        view = (SOURCES / "SettingsView.swift").read_text(encoding="utf-8")
        self.assertIn("It floats above all windows on whichever desktop you are looking at", view)
        self.assertNotIn("whatever you have in front covers it", view)


if __name__ == "__main__":
    unittest.main()
