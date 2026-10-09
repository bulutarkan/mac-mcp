from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mcp_server import tools_macos, tools_ui
from mcp_server import clipboard_guard as clipboard_guard_module
from mcp_server.clipboard_guard import ClipboardBusyError, clipboard_guard
from mcp_server.tool_cancellation import ToolCancelledError



class _ProbeLock:
    """A real lock that reports acquire attempts, so a test can prove a contender
    reached the lock instead of hoping a short sleep was long enough."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition()
        self.attempts = 0
        self.refused = 0

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        with self._cond:
            self.attempts += 1
            self._cond.notify_all()
        acquired = self._lock.acquire(blocking, timeout)
        if not acquired:
            with self._cond:
                self.refused += 1
                self._cond.notify_all()
        return acquired

    def release(self) -> None:
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *_exc) -> None:
        self.release()

    def wait_until(self, predicate, timeout: float = 10.0) -> bool:
        with self._cond:
            return self._cond.wait_for(lambda: predicate(self), timeout)

class ClipboardTransactionTests(unittest.TestCase):
    def _paste_patches(self, state: dict[str, str], run_impl):
        state_lock = threading.Lock()

        def fake_get(deadline=None):
            with state_lock:
                return state["value"], None

        def fake_set(value: str, deadline=None):
            with state_lock:
                state["value"] = value
            return True, ""

        return (
            patch.object(tools_ui, "_focus_element", return_value=(True, "")),
            patch.object(tools_ui, "_get_clipboard", side_effect=fake_get),
            patch.object(tools_ui, "_set_clipboard", side_effect=fake_set),
            patch.object(tools_ui, "_run_osascript", side_effect=run_impl),
        )

    def test_two_parallel_pastes_serialize_and_restore_original_clipboard(self) -> None:
        state = {"value": "user-original"}
        first_inside = threading.Event()
        release_first = threading.Event()
        run_order: list[str] = []
        results: dict[str, tuple[bool, str]] = {}
        errors: list[BaseException] = []
        order_lock = threading.Lock()

        def run_impl(*_args, **_kwargs):
            payload = state["value"]
            with order_lock:
                run_order.append(payload)
            if payload == "payload-a":
                first_inside.set()
                self.assertTrue(release_first.wait(2.0))
            return True, "", ""

        probe = _ProbeLock()
        patches = (*self._paste_patches(state, run_impl), patch.object(clipboard_guard_module, "_CLIPBOARD_LOCK", probe))
        for ctx in patches:
            ctx.start()
        try:
            def worker(label: str, payload: str) -> None:
                try:
                    results[label] = tools_ui._paste_text("TextEdit", "w1/1", payload)
                except BaseException as exc:
                    errors.append(exc)

            one = threading.Thread(target=worker, args=("a", "payload-a"))
            two = threading.Thread(target=worker, args=("b", "payload-b"))
            one.start()
            self.assertTrue(first_inside.wait(10.0))
            two.start()
            # The second paste has asked for the clipboard and been refused.
            self.assertTrue(probe.wait_until(lambda lock: lock.refused >= 1), "second paste never reached the clipboard lock")
            self.assertEqual("payload-a", state["value"])
            self.assertEqual(["payload-a"], run_order)
            release_first.set()
            one.join(2.0)
            two.join(2.0)
        finally:
            for ctx in reversed(patches):
                ctx.stop()

        self.assertEqual([], errors)
        self.assertEqual((True, "paste completed"), results["a"])
        self.assertEqual((True, "paste completed"), results["b"])
        self.assertEqual(["payload-a", "payload-b"], run_order)
        self.assertEqual("user-original", state["value"])

    def test_cancel_after_temporary_clipboard_write_restores_previous_value(self) -> None:
        state = {"value": "user-original"}
        set_calls = 0

        def fake_get(deadline=None):
            return state["value"], None

        def fake_set(value: str, deadline=None):
            nonlocal set_calls
            set_calls += 1
            state["value"] = value
            if set_calls == 1:
                raise ToolCancelledError("client_cancelled")
            return True, ""

        with patch.object(tools_ui, "_focus_element", return_value=(True, "")), \
             patch.object(tools_ui, "_get_clipboard", side_effect=fake_get), \
             patch.object(tools_ui, "_set_clipboard", side_effect=fake_set):
            with self.assertRaises(ToolCancelledError):
                tools_ui._paste_text("TextEdit", "w1/1", "payload")

        self.assertEqual("user-original", state["value"])
        self.assertGreaterEqual(set_calls, 2)

    def test_external_clipboard_change_during_paste_is_preserved(self) -> None:
        state = {"value": "user-original"}

        def run_impl(*_args, **_kwargs):
            state["value"] = "user-new-copy"
            return True, "", ""

        patches = self._paste_patches(state, run_impl)
        for ctx in patches:
            ctx.start()
        try:
            result = tools_ui._paste_text("TextEdit", "w1/1", "payload")
        finally:
            for ctx in reversed(patches):
                ctx.stop()

        self.assertEqual((True, "paste completed"), result)
        self.assertEqual("user-new-copy", state["value"])

    def test_direct_clipboard_set_waits_for_temporary_guard(self) -> None:
        started = threading.Event()
        subprocess_called = threading.Event()
        result: dict[str, object] = {}

        def fake_run(*_args, **_kwargs):
            subprocess_called.set()
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        def worker() -> None:
            started.set()
            result.update(tools_macos.clipboard_set(SimpleNamespace(), "direct-value"))

        probe = _ProbeLock()
        with patch.object(tools_macos.subprocess, "run", side_effect=fake_run), \
             patch.object(clipboard_guard_module, "_CLIPBOARD_LOCK", probe):
            with clipboard_guard(timeout_s=10.0):
                thread = threading.Thread(target=worker)
                thread.start()
                self.assertTrue(started.wait(10.0))
                self.assertTrue(probe.wait_until(lambda lock: lock.refused >= 1), "clipboard_set never reached the lock")
                self.assertFalse(subprocess_called.is_set())
            thread.join(10.0)

        self.assertTrue(subprocess_called.is_set())
        self.assertTrue(result.get("ok"))

    def test_clipboard_guard_timeout_is_bounded(self) -> None:
        result: list[str] = []

        def contender() -> None:
            try:
                with clipboard_guard(timeout_s=0.08):
                    result.append("acquired")
            except ClipboardBusyError:
                result.append("busy")

        with clipboard_guard(timeout_s=1.0):
            thread = threading.Thread(target=contender)
            started = time.monotonic()
            thread.start()
            thread.join(1.0)
            elapsed = time.monotonic() - started

        self.assertEqual(["busy"], result)
        self.assertLess(elapsed, 0.5)


if __name__ == "__main__":
    unittest.main()
