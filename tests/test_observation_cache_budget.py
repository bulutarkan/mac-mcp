from __future__ import annotations

import time
import tracemalloc
import unittest
from unittest.mock import patch

from mcp_server import tools_ui


def make_nodes(count: int, text_len: int = 40, tag: str = "") -> list[dict]:
    return [
        {
            "element_id": f"w1/{index}",
            "parent_id": "w1",
            "role": "AXStaticText",
            "subrole": "",
            "title": f"{tag}{index}".ljust(text_len, "t"),
            "description": "text".ljust(text_len, "d"),
            "value": f"{tag}{index}".ljust(text_len, "v"),
            "position": {"x": index, "y": index, "width": 100, "height": 20},
            "enabled": True,
            "focused": False,
            "actions": ["AXPress", "AXShowMenu"],
            "child_count": 0,
            "identifier": "",
        }
        for index in range(count)
    ]


METADATA = {"active_app": "Demo", "pid": 7, "window_count": 1, "windows": [{"index": 1, "title": "Demo"}]}


def save(nodes: list[dict]) -> str:
    return tools_ui._save_observation("Demo", 1, nodes, METADATA, fingerprint="fp", tree_revision="rev")


class ObservationCacheBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        with tools_ui._OBSERVATIONS_LOCK:
            tools_ui._OBSERVATIONS.clear()

    tearDown = setUp

    def total(self) -> int:
        return sum(int(entry["retained_bytes"]) for entry in tools_ui._OBSERVATIONS.values())

    def test_byte_budget_evicts_least_recently_used_and_keeps_the_newest(self) -> None:
        one = tools_ui._OBSERVATION_OVERHEAD_BYTES + sum(tools_ui._approx_bytes(n) for n in make_nodes(200))
        with patch.object(tools_ui, "_OBSERVATION_CACHE_MAX_BYTES", int(one * 3.5)):
            first, second, third = save(make_nodes(200)), save(make_nodes(200)), save(make_nodes(200))
            time.sleep(0.01)
            self.assertIsNotNone(tools_ui._get_observation(first), "an observation in use stays warm")
            fourth = save(make_nodes(200))
            self.assertLessEqual(self.total(), int(one * 3.5))
            self.assertEqual({first, third, fourth}, set(tools_ui._OBSERVATIONS))
            self.assertNotIn(second, tools_ui._OBSERVATIONS)

    def test_an_oversized_observation_is_still_kept_alone(self) -> None:
        with patch.object(tools_ui, "_OBSERVATION_CACHE_MAX_BYTES", 1024):
            save(make_nodes(10))
            newest = save(make_nodes(10))
        self.assertEqual([newest], list(tools_ui._OBSERVATIONS))
        self.assertIsNotNone(tools_ui._get_observation(newest))

    def test_count_limit_still_applies(self) -> None:
        ids = [save(make_nodes(1)) for _ in range(tools_ui._MAX_OBSERVATIONS + 5)]
        self.assertEqual(tools_ui._MAX_OBSERVATIONS, len(tools_ui._OBSERVATIONS))
        self.assertEqual(set(ids[5:]), set(tools_ui._OBSERVATIONS))

    def test_derived_observation_shares_unchanged_nodes_and_accounts_for_updates(self) -> None:
        base_id = save(make_nodes(50))
        base = tools_ui._OBSERVATIONS[base_id]
        updated = dict(base["nodes"]["w1/3"], value="changed")
        derived_id, returned = tools_ui._store_derived_observation(base_id, {"w1/3": updated, "w1/4": None})
        derived = tools_ui._OBSERVATIONS[derived_id]
        self.assertIs(base["nodes"]["w1/0"], derived["nodes"]["w1/0"])
        self.assertIsNot(updated, derived["nodes"]["w1/3"])
        self.assertEqual("changed", derived["nodes"]["w1/3"]["value"])
        self.assertNotIn("w1/4", derived["nodes"])
        self.assertIn("w1/4", base["nodes"], "the base observation is unchanged")
        self.assertNotEqual("changed", base["nodes"]["w1/3"]["value"])
        expected = base["retained_bytes"] - 2 * tools_ui._approx_bytes(base["nodes"]["w1/3"]) \
            + tools_ui._approx_bytes(derived["nodes"]["w1/3"])
        self.assertAlmostEqual(expected, derived["retained_bytes"], delta=16)
        self.assertEqual(49, len(returned["nodes"]))
        returned["window_handles"]["x"] = "y"
        self.assertNotIn("x", derived["window_handles"], "metadata handed out is a copy")

    def test_stress_fixture_stays_under_the_budget(self) -> None:
        tracemalloc.start()
        try:
            baseline = tracemalloc.get_traced_memory()[0]
            ids = [save(make_nodes(500, text_len=200, tag=f"o{n}-")) for n in range(tools_ui._MAX_OBSERVATIONS)]
            for base_id in ids[-16:]:
                if base_id in tools_ui._OBSERVATIONS:
                    node = dict(tools_ui._OBSERVATIONS[base_id]["nodes"]["w1/1"], value="x")
                    tools_ui._store_derived_observation(base_id, {"w1/1": node})
            retained = tracemalloc.get_traced_memory()[0] - baseline
        finally:
            tracemalloc.stop()
        self.assertLessEqual(self.total(), tools_ui._OBSERVATION_CACHE_MAX_BYTES)
        self.assertLessEqual(retained, tools_ui._OBSERVATION_CACHE_MAX_BYTES * 1.1)
        self.assertLess(len(tools_ui._OBSERVATIONS), tools_ui._MAX_OBSERVATIONS)


if __name__ == "__main__":
    unittest.main()
