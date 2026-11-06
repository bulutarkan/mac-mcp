"""Regression tests for two simultaneously connected Chrome profiles."""
from __future__ import annotations

import asyncio
import threading
import unittest

from fastapi import HTTPException

from mcp_server.chrome_background_bridge import (
    EXPECTED_COMPANION_FEATURES,
    MultiProfileChromeBackgroundBridge,
)


class FakeProfileSocket:
    def __init__(self, bridge, owned_tab, owned_window, label):
        self.bridge = bridge
        self.owned_tab = owned_tab
        self.owned_window = owned_window
        self.label = label
        self.connection_id = None
        self.calls = []
        self.failure = None

    async def send_json(self, message):
        self.calls.append(dict(message))
        request_id = message["request_id"]
        action = message["type"]
        tab_id = message.get("chrome_tab_id")
        window_id = message.get("window_id")
        if action == "list_tabs":
            answer = {"ok": True, "tabs": [{
                "chrome_tab_id": self.owned_tab,
                "chrome_window_id": self.owned_window,
                "index": 0, "active": True,
                "title": self.label, "url": f"https://example.test/{self.label.lower()}",
            }]}
        elif action == "open_tab":
            if window_id != self.owned_window:
                answer = {"ok": False, "error": "chrome_window_not_found"}
            else:
                answer = {"ok": True, "chrome_tab_id": self.owned_tab + 1,
                          "chrome_window_id": self.owned_window}
        elif action == "open_window":
            answer = {"ok": True, "chrome_tab_id": self.owned_tab + 1,
                      "chrome_window_id": self.owned_window}
        elif tab_id != self.owned_tab:
            answer = {"ok": False, "error": "chrome_debugger_evaluate_failed",
                      "message": f"No tab with id: {tab_id}"}
        elif self.failure:
            answer = {"ok": False, "error": "chrome_debugger_evaluate_failed",
                      "message": self.failure}
        else:
            answer = {"ok": True, "chrome_tab_id": self.owned_tab,
                      "result": self.label}
        self.bridge.handle_message(
            {"type": "result", "request_id": request_id, **answer},
            connection_id=self.connection_id,
        )


class MultiProfileBridgeTests(unittest.TestCase):
    def setUp(self):
        self.bridge = MultiProfileChromeBackgroundBridge()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def tearDown(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)
        self.loop.close()

    def connect(self, tab_id, window_id, label, features=None):
        ws = FakeProfileSocket(self.bridge, tab_id, window_id, label)
        conn_id = self.bridge._attach(
            ws, self.loop,
            list(EXPECTED_COMPANION_FEATURES) if features is None else features,
            "2.3.0",
        )
        ws.connection_id = conn_id
        return ws

    def test_concurrent_connections_and_pinned_routes(self):
        findspo = self.connect(101, 1001, "Findspo")
        leanspots = self.connect(202, 2002, "Leanspots")
        self.assertEqual(2, self.bridge.status()["connection_count"])
        self.assertEqual("Leanspots", self.bridge.request_execute_js(202, "1"))
        self.assertEqual("Findspo", self.bridge.request_execute_js(101, "1"))
        self.assertEqual("Findspo", self.bridge.request_execute_js(101, "1"))
        self.assertEqual(3, len(findspo.calls))  # inventory + 2 pinned actions
        self.assertEqual(2, len(leanspots.calls))  # inventory + pinned action
        self.bridge._detach(findspo.connection_id)
        self.assertTrue(self.bridge.is_connected())
        # A profile set change invalidates all previously returned handles.
        with self.assertRaises(HTTPException) as ctx:
            self.bridge.request_execute_js(202, "1")
        self.assertEqual("stale_tab_handle", ctx.exception.detail["error"])
        self.bridge.request_tab_inventory()
        self.assertEqual("Leanspots", self.bridge.request_execute_js(202, "1"))
        self.bridge._detach(leanspots.connection_id)
        self.assertFalse(self.bridge.is_connected())

    def test_tab_inventory_merges_profiles_and_pins_correct_owner(self):
        findspo = self.connect(101, 1001, "Findspo")
        leanspots = self.connect(202, 2002, "Leanspots")
        self.assertTrue(self.bridge.inventory_supported())
        result = self.bridge.request_tab_inventory()
        self.assertEqual({"101", "202"}, {x["native_id"] for x in result})
        self.assertEqual({"Findspo", "Leanspots"}, {x["title"] for x in result})
        self.assertEqual("Findspo", self.bridge.request_execute_js(101, "1"))
        self.assertEqual("Leanspots", self.bridge.request_execute_js(202, "1"))
        self.assertEqual(2, len(findspo.calls))
        self.assertEqual(2, len(leanspots.calls))

    def test_partial_inventory_refuses_stale_companion(self):
        self.connect(101, 1001, "Findspo")
        self.connect(202, 2002, "Old", features=["gestures"])
        self.assertFalse(self.bridge.inventory_supported())
        with self.assertRaises(HTTPException) as ctx:
            self.bridge.request_tab_inventory()
        self.assertEqual("chrome_companion_update_required", ctx.exception.detail["error"])

    def test_inventory_connection_loss_fails_closed(self):
        one = self.connect(101, 1001, "Findspo")
        self.connect(202, 2002, "Leanspots")
        self.bridge._detach(one.connection_id)
        result = self.bridge.request_tab_inventory()
        self.assertEqual(["202"], [item["native_id"] for item in result])

    def test_inventory_duplicate_tab_id_is_rejected(self):
        self.connect(101, 1001, "Findspo")
        self.connect(101, 2002, "Other")
        with self.assertRaises(HTTPException) as ctx:
            self.bridge.request_tab_inventory()
        self.assertEqual("chrome_tab_identity_collision", ctx.exception.detail["error"])

    def test_profile_collision_rejects_before_any_mutating_request(self):
        a = self.connect(101, 1001, "A")
        b = self.connect(101, 2002, "B")
        with self.assertRaises(HTTPException) as context:
            self.bridge.request_execute_js(101, "document.body.textContent")
        self.assertEqual("chrome_tab_identity_collision", context.exception.detail["error"])
        self.assertFalse(self.bridge._inventory_verified)
        self.assertEqual([], [call for peer in (a, b) for call in peer.calls
                              if call["type"] == "execute_js"])

    def test_reused_tab_id_requires_explicit_refresh_after_disconnect(self):
        first = self.connect(101, 1001, "Original")
        other = self.connect(202, 2002, "Other")
        inventory = self.bridge.request_tab_inventory()
        first_owner = next(x["owner_id"] for x in inventory if x["native_id"] == "101")
        self.bridge._detach(first.connection_id)
        # The original ID cannot be re-routed to another account without new
        # owner-scoped handles obtained by listing the tabs again.
        with self.assertRaises(HTTPException) as ctx:
            self.bridge.request_execute_js(101, "1")
        self.assertEqual("stale_tab_handle", ctx.exception.detail["error"])
        self.assertEqual([], [x for x in other.calls if x["type"] == "execute_js"])
        replacement = self.connect(101, 3003, "Replacement")
        with self.assertRaises(HTTPException) as ctx:
            self.bridge.request_execute_js(101, "1")
        self.assertEqual("stale_tab_handle", ctx.exception.detail["error"])
        refreshed = self.bridge.request_tab_inventory()
        new_owner = next(x["owner_id"] for x in refreshed if x["native_id"] == "101")
        self.assertNotEqual(first_owner, new_owner)
        self.assertEqual("Replacement", self.bridge.request_execute_js(101, "1"))
        self.assertEqual([], [x for x in other.calls if x["type"] == "execute_js"])

    def test_inventory_owner_change_fails_closed_when_profile_joins(self):
        first = self.connect(101, 1001, "A")
        self.bridge.request_tab_inventory()
        self.connect(202, 2002, "B")
        self.assertFalse(self.bridge._inventory_verified)
        with self.assertRaises(HTTPException) as ctx:
            self.bridge.request_execute_js(101, "1")
        self.assertEqual("stale_tab_handle", ctx.exception.detail["error"])
        self.assertEqual(0, len([x for x in first.calls if x["type"] == "execute_js"]))

    def test_open_tab_goes_to_only_the_owning_window(self):
        findspo = self.connect(101, 1001, "Findspo")
        leanspots = self.connect(202, 2002, "Leanspots")
        result = self.bridge.request_open_tab("https://example.org/", window_id=2002)
        self.assertEqual(2002, result["chrome_window_id"])
        self.assertEqual(1, len(findspo.calls))  # read-only inventory
        self.assertEqual(2, len(leanspots.calls))  # inventory + open
        with self.assertRaises(HTTPException) as context:
            self.bridge.request_open_tab("https://example.org/")
        self.assertEqual("chrome_profile_selection_required", context.exception.detail["error"])

    def test_stale_profile_cannot_mutate_another_after_disconnect(self):
        a = self.connect(101, 1001, "A")
        b = self.connect(202, 2002, "B")
        self.bridge.request_execute_js(101, "1")
        self.bridge._detach(a.connection_id)
        with self.assertRaises(HTTPException) as context:
            self.bridge.request_execute_js(101, "1")
        self.assertEqual("stale_tab_handle", context.exception.detail["error"])
        self.assertEqual(2, len(a.calls))  # inventory + original action only
        self.assertEqual(1, len(b.calls))  # read-only inventory; never probed with old ID

    def test_non_ownership_error_is_not_replayed(self):
        a = self.connect(101, 1001, "A")
        b = self.connect(202, 2002, "B")
        a.failure = "A real error in the target page"
        with self.assertRaises(HTTPException) as context:
            self.bridge.request_execute_js(101, "1")
        self.assertEqual("chrome_debugger_evaluate_failed", context.exception.detail["error"])
        self.assertEqual(1, len(b.calls))  # inventory only
        self.assertEqual([], [call for call in b.calls if call["type"] == "execute_js"])

    def test_one_old_companion_cannot_mutate_an_unverified_apple_script_tab(self):
        older = self.connect(101, 1001, "Old", features=["gestures"])
        with self.assertRaises(HTTPException) as ctx:
            self.bridge.request_execute_js(101, "1")
        self.assertEqual("chrome_companion_update_required", ctx.exception.detail["error"])
        self.assertEqual("tab_inventory", ctx.exception.detail["feature"])
        self.assertEqual([], older.calls)

    def test_one_verified_profile_routes_only_authenticated_tab(self):
        connected = self.connect(101, 1001, "Verified")
        self.assertEqual("Verified", self.bridge.request_execute_js(101, "1"))
        self.assertEqual(["list_tabs", "execute_js"], [x["type"] for x in connected.calls])
        with self.assertRaises(HTTPException) as ctx:
            self.bridge.request_execute_js(999, "1")
        self.assertEqual("chrome_profile_target_not_found", ctx.exception.detail["error"])
        self.assertNotIn("execute_js", [x["type"] for x in connected.calls[2:]])

    def test_older_profile_can_coexist_but_requires_upgrade_for_window_creation(self):
        older = self.connect(101, 1001, "old", features=["gestures"])
        newer = self.connect(202, 2002, "new")
        self.assertTrue(self.bridge.status()["needs_reload"])
        with self.assertRaises(HTTPException) as context:
            self.bridge.request_execute_js(101, "1")
        self.assertEqual("chrome_companion_update_required", context.exception.detail["error"])
        self.assertEqual("tab_inventory", context.exception.detail["feature"])
        with self.assertRaises(HTTPException) as context:
            self.bridge.request_open_tab("https://example.org/", window_id=1001)
        self.assertEqual("chrome_companion_update_required", context.exception.detail["error"])
        self.assertEqual(0, len(older.calls))
        self.assertEqual(0, len(newer.calls))


if __name__ == "__main__":
    unittest.main()
