#!/usr/bin/env python3
"""Run the test suite, or one tier of it (#191).

  --tier full    every test (default)
  --tier python  everything except the Swift-only modules in tests/_tiers.py

Run from the repository root, as CI does: python scripts/run_tests.py --tier python
"""
from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _without(suite: unittest.TestSuite, skipped: set) -> unittest.TestSuite:
    kept = unittest.TestSuite()
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            kept.addTest(_without(item, skipped))
        elif type(item).__module__.split(".")[-1] not in skipped:
            kept.addTest(item)
    return kept


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tier", choices=("full", "python"), default="full")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(ROOT))
    # Same as `python -m unittest discover -s tests` from the repository root.
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    if args.tier == "python":
        from tests._tiers import SWIFT_ONLY
        suite = _without(suite, set(SWIFT_ONLY))
    result = unittest.TextTestRunner(verbosity=2 if args.verbose else 1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
