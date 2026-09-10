from __future__ import annotations

import unittest

from mcp_server.main import (
    _BROWSER_DO_OUTPUT_BUDGET_BYTES,
    _browser_json_bytes,
    _fit_browser_do_output,
)


class BrowserDoCompactOutputTests(unittest.TestCase):
    def test_small_payload_is_unchanged(self):
        payload = {"ok": True, "data": {"price": ["7.300 TL"]}, "url": "https://example.com"}
        self.assertIs(payload, _fit_browser_do_output(payload))

    def test_large_state_is_bounded_and_marked(self):
        payload = {
            "ok": True,
            "data": {
                "price": ["7.300 TL"],
                "cancellation": ["Ücretsiz İptal"],
                "notes": ["x" * 6000 for _ in range(8)],
            },
            "url": "https://example.com/hotel",
            "title": "Hotel",
            "tab_handle": "btab_test",
            "closed": True,
            "state": {
                "ok": True,
                "url": "https://example.com/hotel",
                "title": "Hotel",
                "scroll": {"x": 0, "y": 1800},
                "dom_revision": 42,
                "elements": [{"text": "y" * 2000} for _ in range(120)],
            },
        }
        compact = _fit_browser_do_output(payload)
        self.assertLessEqual(_browser_json_bytes(compact), _BROWSER_DO_OUTPUT_BUDGET_BYTES)
        self.assertTrue(compact.get("output_truncated"))
        self.assertEqual(["7.300 TL"], compact["data"]["price"])
        self.assertNotIn("elements", compact.get("state", {}))

    def test_pathological_payload_still_respects_budget(self):
        payload = {
            "ok": True,
            "url": "https://example.com/" + "u" * 20000,
            "title": "t" * 20000,
            "tab_handle": "h" * 20000,
            "data": {f"field_{i}": ["z" * 5000 for _ in range(5)] for i in range(100)},
        }
        compact = _fit_browser_do_output(payload)
        self.assertLessEqual(_browser_json_bytes(compact), _BROWSER_DO_OUTPUT_BUDGET_BYTES)
        self.assertTrue(compact.get("output_truncated"))


if __name__ == "__main__":
    unittest.main()
