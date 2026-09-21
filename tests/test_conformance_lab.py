from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from mcp_server import cli, conformance


class ComputerUseConformanceTests(unittest.TestCase):
    def test_deterministic_suite_has_bounded_meaningful_size_and_passes(self) -> None:
        report = conformance.run_conformance()
        self.assertTrue(report["ok"], report)
        self.assertGreaterEqual(report["deterministic_check_count"], 10)
        self.assertLessEqual(report["deterministic_check_count"], 20)
        self.assertEqual(report["metrics"]["deterministic_pass_rate"], 100.0)
        self.assertEqual(report["metrics"]["focus_safety_regressions"], 0)
        self.assertGreaterEqual(report["metrics"]["focus_safe_contracts"], 4)
        ids = {row["check_id"] for row in report["checks"]}
        self.assertIn("browser.stale_handle", ids)
        self.assertIn("browser.render_readiness", ids)
        self.assertIn("browser.no_effect", ids)
        self.assertIn("computer_plan.closed_loop_recovery", ids)
        self.assertIn("native.semantic_identity", ids)
        self.assertEqual(report["baseline_version"], 2)

    def test_regression_flips_report_red(self) -> None:
        with patch("mcp_server.conformance._background_open_default", return_value=False):
            report = conformance.run_conformance()
        self.assertFalse(report["ok"])
        failed = [row for row in report["checks"] if row["status"] == "fail"]
        self.assertEqual(failed[0]["check_id"], "browser.background_open_default")
        self.assertEqual(failed[0]["reason_code"], "CONFORMANCE_REGRESSION")

    def test_conformance_cli_json(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["conformance", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["kind"], "computer-use-conformance")
        self.assertFalse(payload["live_enabled"])


if __name__ == "__main__":
    unittest.main()
