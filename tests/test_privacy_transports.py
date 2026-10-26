"""#112: the owner's observation pause holds on every transport, not only /mcp.

Every registered tool is classified here: it either observes (it is in PAUSED_TOOLS) or it was reviewed as
something that keeps working while paused. A new tool that is neither fails the contract test, so it cannot
reach the screen, a page, the clipboard or the Accessibility tree around the pause by accident.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError

import mcp_server.rest_routes as rest_routes
from mcp_server import browser_checkpoint, error_contract, fleet, privacy_policy
from mcp_server.policy import PolicyContext, environment_policy_context, reset_policy_context, set_policy_context
from mcp_server.security import load_settings
from tests.test_core_catalog_descriptions import _build_mcp
from tests.test_privacy_policy import PrivacySettings

# Reviewed: none of these reads what is on screen, in a page, on the clipboard or in the Accessibility tree,
# so they keep working while observation is paused.
KEEPS_WORKING_WHILE_PAUSED = frozenset({
    # files, search, shell and jobs: "writing a file or running a command is not observing the screen"
    "read_file", "read_multiple_files", "write_file", "write_files_batch", "edit_file", "move_file", "copy_file",
    "delete_path", "list_directory", "directory_tree", "create_directory", "get_file_info", "find_files",
    "file_transaction_batch", "file_transaction_undo", "search_files", "spotlight_search", "storage_review",
    "run_command", "run_commands_parallel", "run_applescript", "start_background_job", "get_job_status",
    "get_job_output", "stop_job", "list_jobs", "wait_jobs", "delete_job", "process_list", "kill_process",
    "get_system_info", "http_request",
    # Mac settings and launching, without reading what is shown
    "open_app", "open_url", "send_notification", "set_volume", "get_volume", "set_brightness", "set_reminder",
    "clipboard_set", "get_running_apps",
    # tabs closed or brought forward, and downloads waited for as files: nothing in the page is read
    "browser_activate_tab", "browser_close_tab", "browser_wait_for_download", "artifact_pipeline",
    # asking the person, memory, lessons, skills and documents the owner chose
    "ask_user", "ask_confirmation", "memory_add", "memory_update", "memory_delete", "memory_get", "memory_search",
    "lesson_record", "lesson_feedback", "lesson_consolidate", "lesson_delete", "lesson_export", "lesson_search",
    "skill_get", "skill_list", "skill_register", "skill_search", "skill_update_index", "knowledge_search",
    # agents already running, teams and scheduled work (new agents are paused: spawn_agent, spawn_agents)
    "agent_action", "agent_catalog", "get_agent", "list_agents", "wait_agents", "team_messages", "scheduled_tasks",
    # Mac MCP's own panel and updater
    "mac_mcp_panel_setting", "mac_mcp_panel_state", "mac_mcp_update", "open_mac_mcp_panel",
    # dispatchers whose observing parts meet the pause on their own: tool_invoke calls the named tool through
    # the gate; recipe runs each step as a tool call; workspace refuses to read open tabs or the Finder
    # selection; fleet runs on another Mac, under that Mac's own pause
    "tool_discover", "tool_invoke", "recipe", "workspace", "fleet",
})

# The REST handler each /api operation calls, when it is not named like the tool.
_REST_HANDLER = {"mac_snapshot": "unified_read_snapshot", "mac_observe": "observe_ui", "mac_act": "act_ui"}
_REST_BODY = {"mac_act": {"actions": []}, "context_handoff": {"action": "inspect"}}


def _registered_tools() -> set:
    return {tool.name for tool in _build_mcp()._tool_manager.list_tools() if "__" not in tool.name}


def _rest_operations() -> set:
    return {route.operation_id for route in rest_routes.router.routes
            if isinstance(route, APIRoute) and route.operation_id}


class RegistryContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tools = _registered_tools()

    def test_every_tool_is_classified_as_observing_or_reviewed(self) -> None:
        unclassified = sorted(self.tools - privacy_policy.PAUSED_TOOLS - KEEPS_WORKING_WHILE_PAUSED)
        self.assertEqual([], unclassified, "New tool: add it to privacy_policy.PAUSED_TOOLS if it reads the screen, "
                                           "a page, the clipboard or the Accessibility tree; otherwise review it "
                                           "and add it to KEEPS_WORKING_WHILE_PAUSED here.")
        self.assertEqual(set(), privacy_policy.PAUSED_TOOLS & KEEPS_WORKING_WHILE_PAUSED)
        self.assertEqual(set(), (privacy_policy.PAUSED_TOOLS | KEEPS_WORKING_WHILE_PAUSED) - self.tools,
                         "a classified tool is no longer registered; drop it from the lists")

    def test_observing_tools_found_missing_are_paused(self) -> None:
        for tool in ("browser_checkpoint", "context_handoff"):
            self.assertIn(tool, privacy_policy.PAUSED_TOOLS)

    def test_every_rest_operation_is_a_classified_tool(self) -> None:
        operations = _rest_operations()
        self.assertEqual(set(), operations - self.tools)
        self.assertTrue({"screenshot", "clipboard_get", "browser_get_html", "mac_snapshot", "mac_observe"} <= operations)


class RestPauseCase(PrivacySettings):
    def setUp(self) -> None:
        super().setUp()
        saved = (rest_routes._settings, rest_routes._rest_security_context, rest_routes._rest_limiter)
        self.addCleanup(self._restore, saved)
        rest_routes._settings = replace(load_settings(), allow_no_auth=True, api_key="")
        rest_routes._rest_security_context = None
        for patcher in (patch.object(rest_routes, "rate_limit", lambda *args, **kwargs: None),):
            patcher.start()
            self.addCleanup(patcher.stop)
        app = FastAPI()
        app.include_router(rest_routes.router)
        error_contract.install_rest_handlers(app, lambda tool: False)
        self.client = TestClient(app)

    @staticmethod
    def _restore(saved) -> None:
        rest_routes._settings, rest_routes._rest_security_context, rest_routes._rest_limiter = saved

    def call(self, operation: str, headers=None, path: str = "", body=None):
        handler = _REST_HANDLER.get(operation, operation)
        with patch.object(rest_routes, handler, return_value={"ok": True, "read": operation}) as called:
            response = self.client.post(path or f"/{operation}", headers=headers or {},
                                        json=body or {"browser": "Safari", **_REST_BODY.get(operation, {})})
        return response, called

    def assertPaused(self, response, called) -> None:
        self.assertEqual(403, response.status_code, response.text)
        body = response.json()
        self.assertTrue(str(body["detail"]).startswith("observation_paused:"), body)
        # The REST error contract as a refused screenshot or clipboard read already had it: 403, nothing ran.
        self.assertEqual((403, "not_executed"), (body["error"]["http_status"], body["error"]["outcome"]))
        called.assert_not_called()


class RestPauseTests(RestPauseCase):
    def setUp(self) -> None:
        super().setUp()
        trusted = PolicyContext(profile="trusted", actor="agent:privacy-rest-test")
        patcher = patch.object(rest_routes, "resolve_request_identity", return_value=("cred_privacy_rest", trusted))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.observing = sorted(_rest_operations() & privacy_policy.PAUSED_TOOLS)

    def test_every_rest_observation_route_is_refused_while_paused_and_works_otherwise(self) -> None:
        self.assertGreaterEqual(len(self.observing), 18)
        for operation in self.observing:
            with self.subTest(operation=operation):
                response, called = self.call(operation)
                self.assertEqual(200, response.status_code, response.text)
                called.assert_called_once()
        self.rules(observation_paused=True)
        for operation in self.observing:
            with self.subTest(operation=operation):
                self.assertPaused(*self.call(operation))

    def test_grouped_routes_meet_the_pause_too(self) -> None:
        self.rules(observation_paused=True)
        self.assertPaused(*self.call("screenshot", path="/macos", body={"tool": "screenshot"}))
        self.assertPaused(*self.call("clipboard_get", path="/macos", body={"tool": "clipboard_get"}))
        self.assertPaused(*self.call("browser_list_tabs", path="/browser",
                                     body={"tool": "browser_list_tabs", "browser": "Safari"}))

    def test_work_that_does_not_observe_keeps_going_while_paused(self) -> None:
        self.rules(observation_paused=True)
        for operation in ("read_file", "get_volume", "browser_close_tab"):
            with self.subTest(operation=operation):
                response, called = self.call(operation, body={"path": "/tmp/x", "browser": "Safari"})
                self.assertEqual(200, response.status_code, response.text)
                called.assert_called_once()


class FleetPauseTests(RestPauseCase):
    """Another Mac reaches this one with its fleet credential on /api and /mcp; neither gets past the pause."""

    def setUp(self) -> None:
        super().setUp()
        self.store = fleet.FleetStore(Path(tempfile.mkdtemp()) / "fleet")
        me = {"device_id": "mac_bbbbbbbbbbbbbbbb", "name": "Studio", "model": "Mac", "available": True}
        for patcher in (patch.object(fleet, "_STORE", self.store), patch.object(fleet, "identity", return_value=me),
                        patch("mcp_server.policy.permission_profile_name", return_value="trusted")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def credential(self) -> str:
        made = self.store.request_access(self.store.issue_offer("full")["code"], client_device_id="mac_aaaaaaaaaaaaaaaa",
                                         client_name="Air", server_device_id="mac_bbbbbbbbbbbbbbbb", source="s")
        self.store.decide(made["request_id"], allow=True)
        return self.store.poll_request(made["request_id"], made["poll_secret"])["credential"]

    def test_a_paired_mac_reads_nothing_over_rest_while_paused(self) -> None:
        auth = {"Authorization": f"Bearer {self.credential()}"}
        response, called = self.call("browser_list_tabs", headers=auth)
        self.assertEqual(200, response.status_code, response.text)
        called.assert_called_once()
        self.rules(observation_paused=True)
        for operation in ("browser_list_tabs", "mac_snapshot", "screenshot", "clipboard_get"):
            with self.subTest(operation=operation):
                self.assertPaused(*self.call(operation, headers=auth))


class McpPauseTests(PrivacySettings):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mcp = _build_mcp()

    def setUp(self) -> None:
        super().setUp()
        env = patch.dict(os.environ, {"MAC_MCP_PERMISSION_PROFILE": "trusted"})
        env.start()
        self.addCleanup(env.stop)

    def checkpoint(self, actor: str = "local_launcher"):
        token = set_policy_context(environment_policy_context(actor=actor))
        try:
            with patch.object(browser_checkpoint, "create", return_value={"ok": True, "status": "awaiting_human"}) as made:
                try:
                    asyncio.run(self.mcp.call_tool("browser_checkpoint", {"browser": "Google Chrome",
                                                                           "tab_handle": "btab_privacy", "notify": False}))
                    error = None
                except ToolError as exc:
                    error = str(exc)
        finally:
            reset_policy_context(token)
        return error, made

    def test_browser_checkpoint_reads_no_page_while_paused(self) -> None:
        error, made = self.checkpoint()
        self.assertIsNone(error)
        made.assert_called_once()
        self.rules(observation_paused=True)
        error, made = self.checkpoint()
        self.assertIn("observation_paused", error or "")
        made.assert_not_called()

    def test_a_paired_macs_tool_call_meets_the_same_pause(self) -> None:
        # /mcp with a fleet credential and a fleet recipe's steps both run as this kind of call.
        self.rules(observation_paused=True)
        error, made = self.checkpoint(actor="fleet:Air")
        self.assertIn("observation_paused", error or "")
        made.assert_not_called()


class WorkspaceAndDashboardPauseTests(PrivacySettings):
    def test_saving_a_workspace_does_not_read_open_tabs_or_the_finder_selection_while_paused(self) -> None:
        from mcp_server import tools_snapshot, workspaces

        self.rules(observation_paused=True)
        with patch.object(tools_snapshot, "_read_browser_tabs") as tabs, \
                patch.object(tools_snapshot, "_read_selected_context") as finder:
            for include in (["urls"], ["finder"]):
                with self.subTest(include=include), self.assertRaises(privacy_policy.PrivacyBlocked) as caught:
                    workspaces.save(load_settings(), "Research", include=include)
                self.assertEqual("observation_paused", caught.exception.code)
        tabs.assert_not_called()
        finder.assert_not_called()

    def test_the_menu_apps_tab_frame_reads_no_tab_bar_while_paused(self) -> None:
        from starlette.applications import Starlette
        from starlette.testclient import TestClient as StarletteClient

        from mcp_server import ax_native
        from mcp_server.dashboard_routes import create_dashboard_routes
        from mcp_server.observability import TelemetryManager

        token = "privacy-dashboard-test-token-0123456789"
        telemetry = TelemetryManager(db_path=self.dir / "telemetry.sqlite3")
        client = StarletteClient(Starlette(routes=create_dashboard_routes(telemetry, load_settings(), token)))
        self.rules(observation_paused=True)
        with patch.object(ax_native, "observe") as observe:
            response = client.post("/dashboard/api/browser/tab-frame", headers={"authorization": f"Bearer {token}"},
                                   json={"browser": "Safari", "tab_handle": "btab_privacy"})
        self.assertEqual((403, {"ok": False, "error": "observation_paused"}), (response.status_code, response.json()))
        observe.assert_not_called()


class MobileContractTests(unittest.TestCase):
    """The phone sees Mac MCP's own status, agents, usage and run outcomes; no mobile route calls a tool,
    and a mobile session is not an /api or /mcp credential. A new mobile route has to be reviewed here."""

    REVIEWED = {
        "/mobile", "/mobile/", "/mobile/assets/{name}", "/mobile/pair", "/mobile/api/status", "/mobile/api/agents",
        "/mobile/api/agents/cancel", "/mobile/api/sessions", "/mobile/api/activity", "/mobile/api/usage",
        "/mobile/api/runs", "/mobile/api/runs/{run_id}", "/dashboard/api/mobile/pairings",
        "/dashboard/api/mobile/devices", "/dashboard/api/mobile/revoke",
    }

    def test_mobile_routes_are_the_reviewed_ones(self) -> None:
        from mcp_server.mobile_auth import MobileAuthStore
        from mcp_server.mobile_routes import create_mobile_routes
        from mcp_server.observability import TelemetryManager

        with tempfile.TemporaryDirectory() as td:
            routes = create_mobile_routes(TelemetryManager(db_path=Path(td) / "t.sqlite3"), load_settings(),
                                          "mobile-contract-token-0123456789",
                                          auth_store=MobileAuthStore(Path(td) / "mobile_auth.sqlite3"))
        self.assertEqual(self.REVIEWED, {route.path for route in routes})
        source = Path(__file__).resolve().parents[1].joinpath("mcp_server", "mobile_routes.py").read_text("utf-8")
        for handler in ("call_tool", "tools_macos", "tools_browser", "tools_ui", "tools_snapshot", "clipboard_get",
                        "observe_ui", "unified_read_snapshot", "browser_list_tabs"):
            self.assertNotIn(handler, source)


if __name__ == "__main__":
    unittest.main()
