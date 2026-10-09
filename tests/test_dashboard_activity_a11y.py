from __future__ import annotations

import re
import unittest
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parents[1] / "mcp_server" / "dashboard"


class DashboardActivityAccessibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = (DASHBOARD / "index.html").read_text(encoding="utf-8")
        self.js = (DASHBOARD / "dashboard.js").read_text(encoding="utf-8")

    def test_activity_is_a_labelled_list_without_partial_table_roles(self) -> None:
        self.assertIn('<ul id="eventRows" class="event-rows" aria-label="Tool calls, newest first"></ul>', self.html)
        self.assertNotIn('role="row"', self.js)
        self.assertNotIn('role="cell"', self.js)
        render = self.js[self.js.index("function renderEvents("):self.js.index("function openDrawer(")]
        self.assertIn("<li><button", render)
        # The button label carries every column in reading order.
        self.assertIn("`${time}, ${event.tool || \"tool\"}, ${event.source || \"mcp\"}, ${took}, ${statusLabel(status)}. Open details`", render)
        self.assertEqual(5, render.count('aria-hidden="true"'))

    def test_one_live_region_announces_finished_calls_not_the_list(self) -> None:
        rows = re.search(r'<ul id="eventRows"[^>]*>', self.html).group(0)
        self.assertNotIn("aria-live", rows)
        self.assertIn('<div id="activityAnnouncer" class="sr-only" role="status" aria-live="polite" aria-atomic="true"></div>', self.html)
        finished = self.js[self.js.index('if (event.kind === "call_finished")'):]
        self.assertIn("announceActivity(`Completed: ${event.tool || \"tool\"} · ${statusLabel(event.status)}`)", finished[:900])
        started = self.js[self.js.index('if (event.kind === "call_started")'):self.js.index('if (event.kind === "call_finished")')]
        self.assertNotIn("announceActivity", started)
        announcer = self.js[self.js.index("function announceActivity("):self.js.index("function handleTelemetry(")]
        self.assertIn("setTimeout", announcer)
        self.assertIn("calls finished. Latest:", announcer)

    def test_keyboard_focus_survives_rerender_and_drawer_close(self) -> None:
        render = self.js[self.js.index("function renderEvents("):self.js.index("function openDrawer(")]
        self.assertIn("els.rows.contains(document.activeElement)", render)
        self.assertIn("row.focus({preventScroll: true})", render)
        close = self.js[self.js.index("function closeDrawer("):self.js.index("function seedTrace(")]
        self.assertIn("state.restoreFocus.isConnected", close)
        self.assertIn("eventRowButton(state.selected.event_id)", close)
        # Focus is restored before the selection is cleared.
        self.assertLess(close.index("target.focus()"), close.index("state.selected = null"))


if __name__ == "__main__":
    unittest.main()
