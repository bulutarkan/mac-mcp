from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import agent_admission, app_adapters, browser_tabs, tools_browser, tools_browser_agent, tools_ui
from mcp_server.computer_plan import derive_computer_plan_resources
from mcp_server.policy import PolicyContext, reset_policy_context, set_policy_context
from mcp_server.security import load_settings
from mcp_server.workspace_arbitration import (
    browser_human_takeover,
    native_app_human_takeover,
    native_window_resource_id,
    sanitize_resource_claims,
)


class HumanInputProbeContractTests(unittest.TestCase):
    def test_human_input_probe_uses_hid_system_state(self) -> None:
        from mcp_server import workspace_arbitration
        self.assertEqual(1, workspace_arbitration._CG_HID_SYSTEM_STATE)
        self.assertNotIn(
            "_CG_COMBINED_SESSION_STATE",
            workspace_arbitration.seconds_since_user_input.__code__.co_names,
        )


class WorkspaceResourceIdentityTests(unittest.TestCase):
    def test_native_window_identity_is_namespaced_and_conflicts_with_app(self) -> None:
        window_id = native_window_resource_id(
            "mwin_123",
            app_handle="MAPP_ABC",
            app="TextEdit",
        )
        self.assertEqual("textedit:mwin_123", window_id)
        app = agent_admission.normalize_claims([
            {"kind": "native_app", "id": "MAPP_ABC", "mode": "write"},
        ])
        window = agent_admission.normalize_claims([
            {"kind": "native_window", "id": "MAPP_ABC:mwin_123", "mode": "write"},
        ])
        self.assertTrue(agent_admission.resources_conflict(app, window))

    def test_computer_plan_namespaces_window_and_only_foreground_text_claims_clipboard(self) -> None:
        auto = derive_computer_plan_resources([
            {
                "id": "native",
                "tool": "mac_act",
                "arguments": {
                    "app": "TextEdit",
                    "app_handle": "mapp_abc",
                    "window_handle": "mwin_123",
                    "actions": [
                        {"type": "type", "element_id": "w1/1", "text": "hello"},
                    ],
                },
            }
        ])
        self.assertIn(
            {"kind": "native_window", "id": "textedit:mwin_123", "mode": "write"},
            auto,
        )
        self.assertNotIn(
            {"kind": "clipboard", "id": "system", "mode": "write"},
            auto,
        )

        foreground = derive_computer_plan_resources([
            {
                "id": "native",
                "tool": "mac_act",
                "arguments": {
                    "app": "TextEdit",
                    "app_handle": "mapp_abc",
                    "window_handle": "mwin_123",
                    "actions": [
                        {
                            "type": "paste",
                            "element_id": "w1/1",
                            "text": "hello",
                            "input_mode": "foreground",
                        },
                    ],
                },
            }
        ])
        self.assertIn(
            {"kind": "clipboard", "id": "system", "mode": "write"},
            foreground,
        )

    def test_first_party_app_and_window_share_bundle_namespace(self) -> None:
        from mcp_server.workspace_arbitration import native_app_resource_id

        app_id = native_app_resource_id(app="Notes")
        window_id = native_window_resource_id(
            "mwin_notes",
            bundle_id="com.apple.Notes",
            app="Notes",
        )
        self.assertEqual("com.apple.notes", app_id)
        self.assertEqual("com.apple.notes:mwin_notes", window_id)
        self.assertTrue(
            agent_admission.resources_conflict(
                [{"kind": "native_app", "id": app_id, "mode": "write"}],
                [{"kind": "native_window", "id": window_id, "mode": "write"}],
            )
        )
        self.assertEqual(
            [
                {
                    "kind": "native_window",
                    "mode": "write",
                    "label": "Notes window",
                }
            ],
            sanitize_resource_claims([
                {"kind": "native_window", "id": window_id, "mode": "write"}
            ]),
        )

    def test_sanitized_resource_activity_never_contains_raw_resource_ids(self) -> None:
        rows = sanitize_resource_claims([
            {"kind": "native_window", "id": "notes:mwin_secret123", "mode": "write"},
            {"kind": "browser_tab", "id": "safari:secret-handle", "mode": "write"},
            {"kind": "file", "id": "/Users/example/private.txt", "mode": "read"},
        ])
        self.assertEqual(
            [
                {"kind": "native_window", "mode": "write", "label": "Notes window"},
                {"kind": "browser_tab", "mode": "write", "label": "Browser tab"},
                {"kind": "file", "mode": "read", "label": "File"},
            ],
            rows,
        )
        self.assertNotIn("secret", str(rows))
        self.assertNotIn("/Users/", str(rows))


class NativeAppHumanPriorityTests(unittest.TestCase):
    def test_root_local_app_adapter_is_not_blocked(self) -> None:
        self.assertIsNone(native_app_human_takeover("Notes"))

    def test_delegated_mutating_adapter_yields_but_read_adapter_stays_available(self) -> None:
        token = set_policy_context(
            PolicyContext(
                profile="trusted",
                actor="agent:agt_app",
                agent_id="agt_app",
                team_id="team_app",
            )
        )
        try:
            with patch(
                "mcp_server.workspace_arbitration.frontmost_application_name",
                return_value=("Notes", None),
            ), patch(
                "mcp_server.workspace_arbitration.recent_user_input",
                return_value=(True, None, 0.1),
            ):
                takeover = native_app_human_takeover("Notes")
            self.assertEqual("HUMAN_ACTIVE_RESOURCE", takeover["reason_code"])

            with patch.object(
                app_adapters,
                "native_app_human_takeover",
                return_value={
                    "reason_code": "HUMAN_ACTIVE_RESOURCE",
                    "human_priority": True,
                    "yielded": True,
                },
            ), patch.object(app_adapters, "_notes_open") as open_note:
                blocked = app_adapters.mac_app(
                    object(),
                    app="Notes",
                    action="open_note",
                    item_id="x-coredata://note",
                )
            self.assertFalse(blocked["ok"])
            self.assertEqual("HUMAN_ACTIVE_RESOURCE", blocked["reason_code"])
            self.assertTrue(blocked["human_priority"])
            self.assertTrue(blocked["yielded"])
            open_note.assert_not_called()

            with patch.object(
                app_adapters,
                "native_app_human_takeover",
            ) as guard, patch.object(
                app_adapters,
                "_notes_find",
                return_value={
                    "ok": True,
                    "verified": True,
                    "items": [],
                    "match_count": 0,
                },
            ):
                read = app_adapters.mac_app(
                    object(),
                    app="Notes",
                    action="find_notes",
                    query="hello",
                )
            self.assertTrue(read["ok"])
            guard.assert_not_called()
        finally:
            reset_policy_context(token)

    def test_computer_plan_marks_semantic_reads_as_read_and_open_as_write(self) -> None:
        read = derive_computer_plan_resources([
            {
                "id": "find",
                "tool": "mac_app",
                "arguments": {"app": "Notes", "action": "find_notes"},
            }
        ])
        write = derive_computer_plan_resources([
            {
                "id": "open",
                "tool": "mac_app",
                "arguments": {"app": "Notes", "action": "open_note"},
            }
        ])
        self.assertIn(
            {"kind": "native_app", "id": "com.apple.notes", "mode": "read"},
            read,
        )
        self.assertIn(
            {"kind": "native_app", "id": "com.apple.notes", "mode": "write"},
            write,
        )


class BrowserReadVsMutationTests(unittest.TestCase):
    def test_internal_semantic_js_read_does_not_request_mutation_guard(self) -> None:
        settings = load_settings()
        target = browser_tabs.TabTarget(
            browser="Safari",
            window_index=1,
            tab_index=1,
            tab_handle="tab_read",
            native_id="101",
            title="Visible",
            url="https://example.test",
            active=True,
        )

        class Lease:
            def __enter__(self):
                return target
            def __exit__(self, exc_type, exc, tb):
                return False

        encoded = "eyJvayI6dHJ1ZSwidmFsdWUiOiJyZWFkIn0="
        with patch.object(
            tools_browser_agent,
            "_tab_lease",
            return_value=Lease(),
        ) as lease, patch.object(
            tools_browser_agent,
            "_execute_js_for_target",
            return_value=encoded,
        ):
            payload = tools_browser_agent._run_json_js(
                settings,
                "Safari",
                "return 'read';",
                tab_handle="tab_read",
            )

        self.assertTrue(payload["ok"])
        self.assertEqual("read", payload["value"])
        self.assertFalse(bool(lease.call_args.kwargs.get("mutation", False)))


class LegacyBrowserReadGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = load_settings()
        self.target = browser_tabs.TabTarget(
            browser="Safari",
            window_index=1,
            tab_index=1,
            tab_handle="tab_read",
            native_id="101",
            title="Visible",
            url="https://example.test",
            active=True,
        )

    def _lease(self):
        target = self.target

        class Lease:
            def __enter__(self):
                return target

            def __exit__(self, exc_type, exc, tb):
                return False

        return Lease()

    def test_public_arbitrary_js_requests_mutation_guard(self) -> None:
        with patch.object(
            tools_browser,
            "_tab_lease",
            return_value=self._lease(),
        ) as lease, patch.object(
            tools_browser,
            "_execute_js_for_target",
            return_value="OK",
        ):
            result = tools_browser.browser_execute_js(
                self.settings,
                "Safari",
                "document.title='mutated'",
                tab_handle="tab_read",
            )

        self.assertTrue(result["ok"])
        self.assertTrue(lease.call_args.kwargs["mutation"])
        self.assertFalse(lease.call_args.kwargs["allow_rebind"])

    def test_private_js_read_never_requests_mutation_guard(self) -> None:
        with patch.object(
            tools_browser,
            "_tab_lease",
            return_value=self._lease(),
        ) as lease, patch.object(
            tools_browser,
            "_execute_js_for_target",
            return_value="read-only",
        ):
            result = tools_browser._browser_execute_js_read(
                self.settings,
                "Safari",
                "document.title",
                tab_handle="tab_read",
            )

        self.assertTrue(result["ok"])
        self.assertFalse(lease.call_args.kwargs["mutation"])
        self.assertTrue(lease.call_args.kwargs["allow_rebind"])

    def test_legacy_read_helpers_use_private_read_path(self) -> None:
        with patch.object(
            tools_browser,
            "_browser_execute_js_read",
            side_effect=[
                {"ok": True, "result": "true", "truncated": False},
                {"ok": True, "result": "<html>ok</html>", "truncated": False},
                {
                    "ok": True,
                    "result": '{"url":"https://example.test","title":"Visible","scroll":{"x":0,"y":0},"viewport":{"w":800,"h":600},"tree":null}',
                    "truncated": False,
                },
            ],
        ) as read_js, patch.object(
            tools_browser,
            "browser_execute_js",
        ) as public_js:
            wait = tools_browser.browser_wait_for_selector(
                self.settings,
                "Safari",
                "#ready",
                timeout_s=1,
                tab_handle="tab_read",
            )
            html = tools_browser.browser_get_html(
                self.settings,
                "Safari",
                tab_handle="tab_read",
            )
            snapshot = tools_browser.browser_get_snapshot(
                self.settings,
                "Safari",
                tab_handle="tab_read",
            )

        self.assertTrue(wait["found"])
        self.assertEqual("<html>ok</html>", html["html"])
        self.assertTrue(snapshot["ok"])
        self.assertEqual(3, read_js.call_count)
        public_js.assert_not_called()


class WorkspaceLeaseContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_resource_lease_has_generation_modes_and_activity(self) -> None:
        first = agent_admission.request_resource_lease(
            self.root,
            owner_id="plan-one",
            resources=[
                {"kind": "native_window", "id": "TextEdit:mwin_1", "mode": "write"},
            ],
            ttl_s=30,
        )
        self.assertTrue(first["admitted"])
        self.assertEqual(1, first["generation"])

        snapshot = agent_admission.snapshot(self.root)
        lease = next(
            row for row in snapshot["leases"]
            if row["lease_id"] == first["lease_id"]
        )
        self.assertEqual(1, lease["generation"])
        self.assertEqual(["write"], lease["resource_modes"])
        self.assertEqual(lease["acquired_at"], lease["last_activity_at"])

        agent_admission.release(self.root, lease_id=first["lease_id"])
        second = agent_admission.request_resource_lease(
            self.root,
            owner_id="plan-two",
            resources=[
                {"kind": "native_window", "id": "TextEdit:mwin_1", "mode": "read"},
            ],
            ttl_s=30,
        )
        self.assertEqual(2, second["generation"])


class DynamicAgentResourceClaimTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_two_agents_serialize_same_window_and_release_unblocks(self) -> None:
        first = agent_admission.claim_agent_resources(
            self.root,
            agent_id="agt_one",
            resources=[
                {"kind": "native_window", "id": "app:mwin_1", "mode": "write"},
            ],
        )
        self.assertTrue(first["admitted"])

        blocked = agent_admission.claim_agent_resources(
            self.root,
            agent_id="agt_two",
            resources=[
                {"kind": "native_window", "id": "app:mwin_1", "mode": "write"},
            ],
        )
        self.assertFalse(blocked["admitted"])
        self.assertEqual("resource_busy", blocked["reason"])

        agent_admission.release(self.root, agent_id="agt_one")
        second = agent_admission.claim_agent_resources(
            self.root,
            agent_id="agt_two",
            resources=[
                {"kind": "native_window", "id": "app:mwin_1", "mode": "write"},
            ],
        )
        self.assertTrue(second["admitted"])

    def test_read_read_share_and_write_conflicts(self) -> None:
        first = agent_admission.claim_agent_resources(
            self.root,
            agent_id="agt_reader_one",
            resources=[
                {"kind": "native_window", "id": "app:mwin_2", "mode": "read"},
            ],
        )
        second = agent_admission.claim_agent_resources(
            self.root,
            agent_id="agt_reader_two",
            resources=[
                {"kind": "native_window", "id": "app:mwin_2", "mode": "read"},
            ],
        )
        writer = agent_admission.claim_agent_resources(
            self.root,
            agent_id="agt_writer",
            resources=[
                {"kind": "native_window", "id": "app:mwin_2", "mode": "write"},
            ],
        )
        self.assertTrue(first["admitted"])
        self.assertTrue(second["admitted"])
        self.assertFalse(writer["admitted"])

    def test_existing_main_lease_is_extended_instead_of_duplicated(self) -> None:
        admitted = agent_admission.request_admission(
            self.root,
            request_id="req-main",
            team_id="team-main",
            task_id="task-main",
            provider="codex",
            resources=[
                {"kind": "workspace", "id": str(self.root), "mode": "read"},
            ],
        )
        agent_admission.bind_agent(
            self.root,
            str(admitted["lease_id"]),
            "agt_main",
        )
        claimed = agent_admission.claim_agent_resources(
            self.root,
            agent_id="agt_main",
            resources=[
                {"kind": "browser_tab", "id": "tab-one", "mode": "write"},
            ],
        )
        self.assertEqual(admitted["lease_id"], claimed["lease_id"])
        snapshot = agent_admission.snapshot(self.root)
        owned = [
            lease for lease in snapshot["leases"]
            if lease.get("agent_id") == "agt_main"
        ]
        self.assertEqual(1, len(owned))
        self.assertIn(
            {"kind": "browser_tab", "id": "tab-one", "mode": "write"},
            owned[0]["resources"],
        )


class BrowserHumanPriorityTests(unittest.TestCase):
    def setUp(self) -> None:
        browser_tabs._REGISTRY.clear()
        browser_tabs._RESOURCE_LOCKS.clear()
        browser_tabs._LOGICAL_LEASES.clear()
        browser_tabs._LEASE_HISTORY.clear()

    def test_root_local_mutation_is_not_blocked_by_human_priority_guard(self) -> None:
        row = {
            "browser": "Safari",
            "window_index": 1,
            "tab_index": 1,
            "tab_handle": "tab_test",
            "native_id": "101",
            "title": "Visible",
            "url": "https://example.test",
            "active": True,
        }
        self.assertIsNone(browser_human_takeover("Safari", row))

    def test_delegated_agent_keeps_acting_on_frontmost_active_tab(self) -> None:
        row = {
            "browser": "Safari",
            "window_index": 1,
            "tab_index": 1,
            "tab_handle": "tab_test",
            "native_id": "101",
            "title": "Visible",
            "url": "https://example.test",
            "active": True,
        }
        token = set_policy_context(
            PolicyContext(
                profile="trusted",
                actor="agent:agt_human",
                agent_id="agt_human",
                team_id="team_human",
            )
        )
        try:
            with patch(
                "mcp_server.workspace_arbitration.frontmost_application_name",
                return_value=("Safari", None),
            ), patch(
                "mcp_server.workspace_arbitration.recent_user_input",
                return_value=(True, None, 0.2),
            ):
                conflict = browser_human_takeover("Safari", row)
        finally:
            reset_policy_context(token)
        # The owner watches agents work in the visible tab; that must not block them.
        self.assertIsNone(conflict)

    def test_mutating_tab_lease_yields_on_global_resource_busy(self) -> None:
        row = {
            "browser": "Safari",
            "window_index": 2,
            "tab_index": 1,
            "tab_handle": "tab_busy",
            "native_id": "202",
            "title": "Background",
            "url": "https://example.test",
            "active": False,
        }
        with patch.object(
            browser_tabs,
            "resolve_tab",
            return_value=(2, 1, row),
        ), patch.object(
            browser_tabs,
            "browser_human_takeover",
            return_value=None,
        ), patch.object(
            browser_tabs,
            "claim_delegated_resource",
            return_value={
                "ok": False,
                "reason_code": "RESOURCE_BUSY",
                "retryable": True,
                "yielded": True,
                "resource_kind": "browser_tab",
            },
        ), patch.object(browser_tabs, "_claim_logical_lease") as claim:
            with self.assertRaises(HTTPException) as caught:
                with browser_tabs.tab_lease(
                    "Safari",
                    tab_handle="tab_busy",
                    mutation=True,
                ):
                    pass
        self.assertEqual(409, caught.exception.status_code)
        self.assertEqual(
            "RESOURCE_BUSY",
            caught.exception.detail["reason_code"],
        )
        claim.assert_not_called()

    def test_mutating_tab_lease_fails_before_logical_claim_when_human_is_active(self) -> None:
        row = {
            "browser": "Safari",
            "window_index": 1,
            "tab_index": 1,
            "tab_handle": "tab_test",
            "native_id": "101",
            "title": "Visible",
            "url": "https://example.test",
            "active": True,
        }
        with patch.object(
            browser_tabs,
            "resolve_tab",
            return_value=(1, 1, row),
        ), patch.object(
            browser_tabs,
            "browser_human_takeover",
            return_value={
                "reason_code": "HUMAN_ACTIVE_RESOURCE",
                "retryable": True,
                "human_priority": True,
                "yielded": True,
            },
        ), patch.object(browser_tabs, "_claim_logical_lease") as claim:
            with self.assertRaises(HTTPException) as caught:
                with browser_tabs.tab_lease(
                    "Safari",
                    tab_handle="tab_test",
                    mutation=True,
                ):
                    pass
        self.assertEqual(409, caught.exception.status_code)
        self.assertEqual(
            "HUMAN_ACTIVE_RESOURCE",
            caught.exception.detail["reason_code"],
        )
        claim.assert_not_called()


class NativeHumanPriorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.target = {
            "app": "TargetApp",
            "pid": 4321,
            "window_index": 1,
            "app_handle": "mapp_target",
            "window_handle": "mwin_target",
            "bundle_id": "com.example.target",
        }

    def test_root_local_native_action_has_no_human_arbitration(self) -> None:
        self.assertIsNone(tools_ui._delegated_native_human_guard(self.target))

    def test_delegated_agent_yields_when_target_window_is_frontmost(self) -> None:
        token = set_policy_context(
            PolicyContext(
                profile="trusted",
                actor="agent:agt_native",
                agent_id="agt_native",
                team_id="team_native",
            )
        )
        try:
            with patch.object(
                tools_ui,
                "_current_focus_key",
                return_value=((4321, 1), None),
            ), patch.object(
                tools_ui,
                "recent_user_input",
                return_value=(True, None, 0.1),
            ):
                conflict = tools_ui._delegated_native_human_guard(self.target)
        finally:
            reset_policy_context(token)

        self.assertEqual("HUMAN_ACTIVE_RESOURCE", conflict["reason_code"])
        self.assertTrue(conflict["human_priority"])
        self.assertTrue(conflict["yielded"])

    def test_delegated_agent_can_mutate_different_background_window(self) -> None:
        token = set_policy_context(
            PolicyContext(
                profile="trusted",
                actor="agent:agt_native",
                agent_id="agt_native",
                team_id="team_native",
            )
        )
        try:
            with patch.object(
                tools_ui,
                "_current_focus_key",
                return_value=((4321, 2), None),
            ):
                conflict = tools_ui._delegated_native_human_guard(self.target)
        finally:
            reset_policy_context(token)
        self.assertIsNone(conflict)


if __name__ == "__main__":
    unittest.main()
