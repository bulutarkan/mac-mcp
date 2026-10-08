from __future__ import annotations

import re
import unittest
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parents[1] / "mcp_server" / "dashboard"


class DashboardFreshnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = (DASHBOARD / "index.html").read_text(encoding="utf-8")
        self.js = (DASHBOARD / "dashboard.js").read_text(encoding="utf-8")

    def test_initial_page_is_never_presented_as_live_measurements(self) -> None:
        badge = re.search(r'<span class="connection[^"]*" id="connectionState">.*?</span></span>', self.html).group(0)
        self.assertIn("is-connecting", badge)
        self.assertIn("Connecting…", badge)
        self.assertNotIn(">Live<", badge)
        self.assertNotIn("role=", badge)  # it ticks every few seconds; never a live region
        for metric in ("metricCalls", "metricSuccess", "metricAverage", "metricP95"):
            self.assertIn(f'<strong id="{metric}">—</strong>', self.html)
        self.assertNotIn(">100%<", self.html)

    def test_freshness_is_tracked_per_source_and_failures_keep_data(self) -> None:
        self.assertIn("const freshness = {summary: 0, agents: 0, changes: 0", self.js)
        summary = self.js[self.js.index("async function refreshSummary()"):self.js.index("async function refreshEvents()")]
        self.assertIn("freshness.summary = Date.now();", summary)
        self.assertLess(summary.index("freshness.summary = Date.now();"), summary.index("markOnline();"))
        agents = self.js[self.js.index("async function refreshAgents()"):self.js.index("function changeSetLabel(")]
        self.assertIn("if (!freshness.agents) els.agentList.innerHTML", agents)
        changes = self.js[self.js.index("async function refreshChanges()"):self.js.index("function scheduleChangesRefresh()")]
        self.assertIn("if (!freshness.changes) els.changeHeadline.textContent", changes)

    def test_badge_states_and_only_summary_success_marks_online(self) -> None:
        render = self.js[self.js.index("function renderFreshness()"):self.js.index("async function refreshSummary()")]
        for text in ("Connecting…", "Reconnecting · updated", "Stale · updated", "Live · updated", "Authentication required"):
            self.assertIn(text, render)
        self.assertEqual(1, self.js.count("markOnline();"))
        self.assertIn("window.setInterval(renderFreshness, 5000);", self.js)


if __name__ == "__main__":
    unittest.main()
