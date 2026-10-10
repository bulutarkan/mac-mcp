"""#122: the embedding worker stays warm during bursts, leaves under memory pressure, and is measured."""
from __future__ import annotations

import io
import json
import os
import unittest
from unittest.mock import patch

from mcp_server import embedding_manager as manager
from mcp_server import embedding_worker as worker


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class RetentionPolicyTests(unittest.TestCase):
    def test_a_burst_of_searches_extends_the_idle_window(self) -> None:
        now = 5000.0
        self.assertEqual(worker.IDLE_SECONDS, worker.idle_limit([now - 10], now))
        burst = [now - 100, now - 50, now - 1]
        self.assertEqual(worker.BUSY_IDLE_SECONDS, worker.idle_limit(burst, now))
        stale = [now - 500, now - 400, now - 1]  # older than the burst window
        self.assertEqual(worker.IDLE_SECONDS, worker.idle_limit(stale, now))
        self.assertGreaterEqual(worker.BUSY_IDLE_SECONDS, worker.IDLE_SECONDS)

    def test_memory_pressure_ends_an_idle_worker_before_its_idle_window(self) -> None:
        clock = Clock()
        levels = iter([1, 1, 2])
        timeouts = []

        def fake_select(streams, _w, _x, timeout):
            timeouts.append(timeout)
            clock.now += timeout
            return [], [], []

        with patch.object(worker.select, "select", side_effect=fake_select):
            code = worker.next_line(io.StringIO(""), [clock.now], pressure=lambda: next(levels), clock=clock)
        self.assertEqual(worker.EXIT_MEMORY_PRESSURE, code)
        # Pressure is re-checked at least every PRESSURE_CHECK_S while idle.
        self.assertTrue(all(t <= worker.PRESSURE_CHECK_S for t in timeouts))
        self.assertLess(clock.now - 1000.0, worker.IDLE_SECONDS)

    def test_quiet_worker_leaves_after_its_idle_window_and_serves_lines_before_that(self) -> None:
        clock = Clock()

        def quiet(streams, _w, _x, timeout):
            clock.now += timeout
            return [], [], []

        with patch.object(worker.select, "select", side_effect=quiet):
            code = worker.next_line(io.StringIO(""), [clock.now], pressure=lambda: 1, clock=clock)
        self.assertEqual(worker.EXIT_IDLE, code)
        self.assertAlmostEqual(worker.IDLE_SECONDS, clock.now - 1000.0, places=3)

        stream = io.StringIO('{"texts":["a"]}\n')
        with patch.object(worker.select, "select", return_value=([stream], [], [])):
            self.assertEqual('{"texts":["a"]}\n', worker.next_line(stream, [], pressure=lambda: 1, clock=clock))

    def test_critical_pressure_also_counts(self) -> None:
        with patch.object(worker.select, "select") as sel:
            code = worker.next_line(io.StringIO(""), [], pressure=lambda: 4, clock=Clock())
        self.assertEqual(worker.EXIT_MEMORY_PRESSURE, code)
        sel.assert_not_called()

    def test_pressure_level_is_read_from_the_kernel(self) -> None:
        self.assertIn(worker.memory_pressure_level(), {1, 2, 4})


class ManagerStatsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.saved = {k: (list(v) if hasattr(v, "append") else (dict(v) if isinstance(v, dict) else v))
                      for k, v in manager._STATS.items()}

    def tearDown(self) -> None:
        manager._STATS["starts"] = self.saved["starts"]
        manager._STATS["exits"] = self.saved["exits"]
        for key in ("cold_ms", "warm_ms"):
            manager._STATS[key].clear()
            manager._STATS[key].extend(self.saved[key])

    def test_busy_idle_is_configurable_and_never_shorter_than_idle(self) -> None:
        with patch.dict(os.environ, {"MAC_MCP_EMBEDDING_IDLE_SECONDS": "60", "MAC_MCP_EMBEDDING_BUSY_IDLE_SECONDS": "30"}):
            self.assertEqual(60.0, manager.busy_idle_seconds())
        with patch.dict(os.environ, {"MAC_MCP_EMBEDDING_IDLE_SECONDS": "60", "MAC_MCP_EMBEDDING_BUSY_IDLE_SECONDS": "240"}):
            self.assertEqual(240.0, manager.busy_idle_seconds())

    def test_exit_reasons_and_latency_percentiles_are_reported(self) -> None:
        manager._STATS["exits"] = {}
        for code in (0, 3, 3, 1):
            manager._record_exit(code)
        self.assertEqual({"idle": 1, "memory_pressure": 2, "error": 1}, manager._STATS["exits"])
        self.assertEqual({"n": 0}, manager._percentiles([]))
        stats = manager._percentiles([float(v) for v in range(1, 101)])
        self.assertEqual((100, 51.0, 95.0), (stats["n"], stats["p50"], stats["p95"]))  # nearest rank
        status = manager.worker_status()
        for key in ("busy_idle_seconds", "starts", "exits", "latency_ms", "rss_mb"):
            self.assertIn(key, status)

    def test_requests_are_timed_as_cold_or_warm(self) -> None:
        reply = json.dumps({"ok": True, "vectors": [[0.1] * manager.MULTILINGUAL_DIMS]}) + "\n"

        class Proc:
            pid = 4242
            returncode = None

            def __init__(self) -> None:
                self.stdin = io.StringIO()
                self.stdout = io.StringIO(reply * 2)

            def poll(self):
                return None

        proc = Proc()
        manager._STATS["cold_ms"].clear()
        manager._STATS["warm_ms"].clear()
        with manager.WORKER_LOCK, \
             patch.object(manager, "_WORKER", None), \
             patch.object(manager, "_WORKER_CACHE", None), \
             patch.object(manager.select, "select", side_effect=lambda r, w, x, t: (r, [], [])), \
             patch.object(manager.subprocess, "Popen", return_value=proc):
            self.assertIsNotNone(manager.worker_vectors(["a"], allow_start=True))
            self.assertIsNotNone(manager.worker_vectors(["b"], allow_start=True))
        self.assertEqual(1, len(manager._STATS["cold_ms"]))
        self.assertEqual(1, len(manager._STATS["warm_ms"]))


if __name__ == "__main__":
    unittest.main()
