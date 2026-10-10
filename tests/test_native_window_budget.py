"""#95: an all-window observation shares the node budget and says when it cut a window short."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import tools_ui
from mcp_server.security import load_settings

FS, RS = "\x1f", "\x1e"
SWIFT = (Path(tools_ui.__file__).with_name("ax_observe.swift")).read_text(encoding="utf-8")


def _raw(budgets):
    records = [FS.join(["__META__", "DemoApp", "false", str(len(budgets)), "One || Two", "4321", "dev.demo"])]
    for index, (used, budget, cut) in enumerate(budgets, start=1):
        records.append(FS.join(["__WINDOW__", str(index), f"W{index}", "", "", "0", "0", "800", "600",
                                "AXStandardWindow", "false", "false"]))
        records.append(FS.join(["__NODE__", f"w{index}", "", "AXWindow", "", f"W{index}", "", "", "0", "0",
                                "800", "600", "true", "false", "", "1", ""]))
        records.append(FS.join(["__BUDGET__", str(index), str(used), str(budget), "true" if cut else "false"]))
    return RS.join(records)


class ObserverScriptTests(unittest.TestCase):
    def test_both_observers_give_each_window_a_share_of_what_is_left(self) -> None:
        script = tools_ui._observation_script("DemoApp", 0, 5, 30, max_nodes=500)
        self.assertIn("set budget to (maxNodes - usedNodes) div remainingWindows", script)
        self.assertIn("if windowIndex is 0 then set remainingWindows to windowCount", script)
        self.assertIn('set end of recordList to "__BUDGET__"', script)
        self.assertIn("let budget = max(0, (maxNodes - usedNodes) / max(1, remainingWindows))", SWIFT)
        self.assertIn('record(["__BUDGET__"', SWIFT)

    def test_both_observers_flag_only_budget_stops(self) -> None:
        script = tools_ui._observation_script("DemoApp", 0, 5, 30, max_nodes=500)
        self.assertEqual(2, script.count("set item 2 of counter to true"))
        self.assertIn("if counter >= maxNodes { budgetHit = true; return }", SWIFT)
        self.assertIn("if counter >= maxNodes { budgetHit = true; break }", SWIFT)
        # Depth and child limits stay separate from the budget flag.
        self.assertIn("if index >= maxChildren { break }", SWIFT)

    def test_a_targeted_window_keeps_the_whole_budget(self) -> None:
        script = tools_ui._observation_script("DemoApp", 2, 5, 30, max_nodes=500)
        self.assertIn("set remainingWindows to 1", script)


class BudgetReportTests(unittest.TestCase):
    def test_parser_reads_budget_records(self) -> None:
        metadata, nodes = tools_ui._parse_observation(_raw([(250, 250, True), (40, 250, False)]))
        self.assertEqual(2, len(nodes))
        self.assertEqual(
            [{"window_index": 1, "nodes": 250, "budget": 250, "truncated": True},
             {"window_index": 2, "nodes": 40, "budget": 250, "truncated": False}],
            metadata["node_budget"],
        )

    def _observe(self, budgets):
        with patch.object(tools_ui, "_read_native_tree", return_value=(True, _raw(budgets), "", "ax_native")), \
             patch.object(tools_ui.ax_watch, "change_token", return_value=None):
            payload, _ = tools_ui._collect_observation(load_settings(), "DemoApp", 0, 5, 30, False, False)
        return payload

    def test_a_budget_cut_is_always_visible(self) -> None:
        payload = self._observe([(250, 250, True), (40, 250, False)])
        self.assertTrue(payload["ok"])
        self.assertEqual([1], payload["node_budget"]["truncated_windows"])
        self.assertEqual("equal_share_of_remaining_per_window", payload["node_budget"]["allocation"])
        budget = payload["telemetry"]["context_budget"]
        self.assertTrue(budget["truncated"])
        self.assertIn("window(s) 1", budget["expand_hint"])
        self.assertIn("window_index", budget["expand_hint"])

    def test_windows_left_without_budget_are_listed(self) -> None:
        payload = self._observe([(500, 500, True), (0, 0, True)])
        self.assertEqual([2], payload["node_budget"]["omitted_windows"])

    def test_a_single_window_that_fit_adds_no_noise(self) -> None:
        payload = self._observe([(12, 500, False)])
        self.assertNotIn("node_budget", payload)


if __name__ == "__main__":
    unittest.main()
