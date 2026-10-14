"""The CI test tiers stay true to the files (#191)."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from tests._tiers import SWIFT_ONLY

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
_SWIFT = re.compile(r'"swiftc"|"xcrun"')
_SERVER = re.compile(r"^\s*(from|import) mcp_server", re.M)


class TierTests(unittest.TestCase):
    def test_swift_only_lists_exactly_the_swift_modules_that_never_load_the_server(self) -> None:
        found = set()
        for path in TESTS.glob("test_*.py"):
            text = path.read_text(encoding="utf-8")
            if _SWIFT.search(text) and not _SERVER.search(text) and "mcp_server." not in text.split("HARNESS")[0]:
                found.add(path.stem)
        self.assertEqual(sorted(found), sorted(SWIFT_ONLY))

    def test_ci_runs_swift_only_tests_once_and_python_tests_everywhere(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "security-regression.yml").read_text(encoding="utf-8")
        self.assertIn("scripts/run_tests.py --tier ${{ matrix.tier }}", workflow)
        self.assertIn('{ python-version: "3.13", tier: full }', workflow)
        self.assertIn('{ python-version: "3.10", tier: python }', workflow)
        self.assertIn('{ python-version: "3.14", tier: python }', workflow)


if __name__ == "__main__":
    unittest.main()
