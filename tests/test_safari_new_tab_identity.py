from __future__ import annotations

import threading
import time
import unittest
from contextlib import nullcontext
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import browser_tabs
from mcp_server.tools_browser import (
    _claim_tab_visual,
    _resolve_safari_created_tab,
    _safari_new_tab_creation_lock,
    _visual_claim_script_for_target,
    browser_open_url,
)


def row(
    *,
    window: int,
    tab: int,
    native: str,
    url: str,
    handle: str,
    active: bool = False,
):
    return {
        "browser": "Safari",
        "window_index": window,
        "tab_index": tab,
        "active": active,
        "native_id": native,
        "title": url.rsplit("/", 1)[-1] or "Page",
        "url": url,
        "tab_handle": handle,
    }


class SafariCreatedTabIdentityTests(unittest.TestCase):
    def test_returned_native_identity_beats_shifted_index(self) -> None:
        before_handles = {"btab_old_a", "btab_old_b"}
        before_ids = {"1001", "1002"}
        shifted = [
            row(window=1, tab=1, native="1999", url="https://other.test/", handle="btab_inserted"),
            row(window=1, tab=2, native="1001", url="https://old-a.test/", handle="btab_old_a"),
            row(window=1, tab=3, native="1002", url="https://old-b.test/", handle="btab_old_b"),
            row(window=1, tab=4, native="2002", url="https://target.test/", handle="btab_created"),
        ]
        with patch.object(browser_tabs, "list_tabs", return_value=shifted):
            resolved = _resolve_safari_created_tab(
                "https://target.test/",
                1,
                3,  # stale hint after another tab was inserted
                "2002",
                before_handles,
                before_ids,
                timeout_s=0.1,
            )
        self.assertEqual("btab_created", resolved["tab_handle"])
        self.assertEqual(4, resolved["tab_index"])
        self.assertEqual("2002", resolved["native_id"])

    def test_neighbor_close_reorder_does_not_steal_created_tab(self) -> None:
        before_handles = {"btab_a", "btab_b", "btab_c"}
        before_ids = {"1101", "1102", "1103"}
        after_close = [
            row(window=1, tab=1, native="1101", url="https://a.test/", handle="btab_a"),
            row(window=1, tab=2, native="1103", url="https://c.test/", handle="btab_c"),
            row(window=1, tab=3, native="2200", url="https://target.test/", handle="btab_created"),
        ]
        with patch.object(browser_tabs, "list_tabs", return_value=after_close):
            resolved = _resolve_safari_created_tab(
                "https://target.test/",
                1,
                4,  # creation originally reported index 4 before neighbor closed
                "2200",
                before_handles,
                before_ids,
                timeout_s=0.1,
            )
        self.assertEqual(3, resolved["tab_index"])
        self.assertEqual("btab_created", resolved["tab_handle"])

    def test_duplicate_same_url_with_returned_native_identity_resolves_exact_tab(self) -> None:
        before_handles = {"btab_old"}
        before_ids = {"1201"}
        rows = [
            row(window=1, tab=1, native="1201", url="https://old.test/", handle="btab_old"),
            row(window=1, tab=2, native="2301", url="https://same.test/", handle="btab_other"),
            row(window=1, tab=3, native="2302", url="https://same.test/", handle="btab_created"),
        ]
        with patch.object(browser_tabs, "list_tabs", return_value=rows):
            resolved = _resolve_safari_created_tab(
                "https://same.test/",
                1,
                2,
                "2302",
                before_handles,
                before_ids,
                timeout_s=0.1,
            )
        self.assertEqual("btab_created", resolved["tab_handle"])
        self.assertEqual("2302", resolved["native_id"])

    def test_duplicate_same_url_without_native_identity_fails_closed(self) -> None:
        before_handles = {"btab_old"}
        before_ids = {"1201"}
        ambiguous = [
            row(window=1, tab=1, native="1201", url="https://old.test/", handle="btab_old"),
            row(window=1, tab=2, native="2301", url="https://same.test/", handle="btab_new_one"),
            row(window=1, tab=3, native="2302", url="https://same.test/", handle="btab_new_two"),
        ]
        with patch.object(browser_tabs, "list_tabs", return_value=ambiguous):
            with self.assertRaises(HTTPException) as ctx:
                _resolve_safari_created_tab(
                    "https://same.test/",
                    1,
                    2,
                    "0",
                    before_handles,
                    before_ids,
                    timeout_s=0.06,
                )
        self.assertEqual(409, ctx.exception.status_code)
        self.assertEqual(
            "safari_created_tab_identity_unresolved",
            ctx.exception.detail["error"],
        )
        self.assertTrue(ctx.exception.detail["retryable"])
        self.assertEqual(2, ctx.exception.detail["candidate_count"])

    def test_pid_reuse_from_before_snapshot_fails_closed(self) -> None:
        before_handles = {"btab_old"}
        before_ids = {"2400"}
        reused = [
            row(window=1, tab=1, native="2400", url="https://target.test/", handle="btab_old"),
        ]
        with patch.object(browser_tabs, "list_tabs", return_value=reused):
            with self.assertRaises(HTTPException) as ctx:
                _resolve_safari_created_tab(
                    "https://target.test/",
                    1,
                    1,
                    "2400",
                    before_handles,
                    before_ids,
                    timeout_s=0.06,
                )
        self.assertEqual(409, ctx.exception.status_code)
        self.assertEqual(
            "safari_created_tab_identity_unresolved",
            ctx.exception.detail["error"],
        )

    def test_window_scoped_creation_lock_serializes_same_window_only(self) -> None:
        entered_one = threading.Event()
        release_one = threading.Event()
        entered_same = threading.Event()
        entered_other = threading.Event()

        def holder() -> None:
            with _safari_new_tab_creation_lock(1):
                entered_one.set()
                release_one.wait(2)

        def same_window() -> None:
            entered_one.wait(2)
            with _safari_new_tab_creation_lock(1):
                entered_same.set()

        def other_window() -> None:
            entered_one.wait(2)
            with _safari_new_tab_creation_lock(2):
                entered_other.set()

        t1 = threading.Thread(target=holder)
        t2 = threading.Thread(target=same_window)
        t3 = threading.Thread(target=other_window)
        t1.start()
        t2.start()
        t3.start()
        self.assertTrue(entered_one.wait(1))
        self.assertTrue(entered_other.wait(1), "different Safari window should not be blocked")
        self.assertFalse(entered_same.wait(0.1), "same Safari window should remain serialized")
        release_one.set()
        self.assertTrue(entered_same.wait(1))
        for thread in (t1, t2, t3):
            thread.join(2)
            self.assertFalse(thread.is_alive())

    def test_visual_claim_targets_resolved_safari_window(self) -> None:
        script = _visual_claim_script_for_target(
            "Safari", 2, 4, "https://target.test/",
        )
        self.assertIn("tab 4 of window 2", script)
        self.assertNotIn("tab 4 of window 1", script)


class SafariOpenUrlIdentityIntegrationTests(unittest.TestCase):
    def test_open_url_uses_returned_pid_not_reported_index_and_claims_same_window(self) -> None:
        before = [
            row(window=2, tab=1, native="9201", url="https://owned.test/", handle="btab_owned", active=True),
        ]
        created = row(
            window=2, tab=3, native="9303",
            url="https://target.test/", handle="btab_created",
        )
        visual_calls: list[tuple] = []

        def fake_visual(*args, **kwargs):
            visual_calls.append((args, kwargs))
            return True

        with patch("mcp_server.tools_browser.validate_url"),              patch("mcp_server.tools_browser._new_tab_window", return_value=2),              patch("mcp_server.tools_browser.browser_tabs.list_tabs", return_value=before),              patch("mcp_server.tools_browser._run_osascript", return_value="2|9303") as osa,              patch("mcp_server.tools_browser._resolve_safari_created_tab", return_value=created) as resolve_created,              patch("mcp_server.tools_browser.browser_tabs.claim_created_tab", return_value={"generation": 4}) as claim,              patch("mcp_server.tools_browser._claim_tab_visual", side_effect=fake_visual):
            result = browser_open_url(
                None, "Safari", "https://target.test/",
                new_tab=True, background=True, window_index=2,
            )

        self.assertEqual(2, result["window_index"])
        self.assertEqual(3, result["tab_index"])
        self.assertEqual("btab_created", result["tab_handle"])
        self.assertEqual(4, result["lease_generation"])
        self.assertIn("pid of newTab", osa.call_args.args[0])
        resolve_args = resolve_created.call_args.args
        self.assertEqual("9303", resolve_args[3])
        claim.assert_called_once_with("Safari", "btab_created")
        self.assertEqual(
            ("Safari", 2, 3, "https://target.test/", "btab_created"),
            visual_calls[0][0],
        )

    def test_open_url_claim_stays_inside_creation_lock(self) -> None:
        order: list[str] = []
        created = row(
            window=1, tab=2, native="9402",
            url="https://target.test/", handle="btab_created",
        )

        class LockProbe:
            def __enter__(self):
                order.append("lock-enter")
            def __exit__(self, exc_type, exc, tb):
                order.append("lock-exit")

        def claim(*_args, **_kwargs):
            order.append("claim")
            return {"generation": 1}

        with patch("mcp_server.tools_browser.validate_url"),              patch("mcp_server.tools_browser._new_tab_window", return_value=1),              patch("mcp_server.tools_browser._safari_new_tab_creation_lock", return_value=LockProbe()),              patch("mcp_server.tools_browser.browser_tabs.list_tabs", return_value=[]),              patch("mcp_server.tools_browser._run_osascript", return_value="2|9402"),              patch("mcp_server.tools_browser._resolve_safari_created_tab", return_value=created),              patch("mcp_server.tools_browser.browser_tabs.claim_created_tab", side_effect=claim),              patch("mcp_server.tools_browser._claim_tab_visual", return_value=False):
            browser_open_url(
                None, "Safari", "https://target.test/",
                new_tab=True, background=True,
            )

        self.assertEqual(["lock-enter", "claim", "lock-exit"], order)


if __name__ == "__main__":
    unittest.main()
