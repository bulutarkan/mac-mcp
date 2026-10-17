"""Discovery imports this module first (files load in name order), so the hermetic environment is in
place before any test module imports mcp_server; `python -m unittest tests.<module>` gets it from the
package. #243."""
from __future__ import annotations

import os
import unittest

import tests  # noqa: F401  (applies tests/_hermetic_env)
from tests import _hermetic_env


class HermeticEnvironmentTests(unittest.TestCase):
    def test_every_run_starts_from_the_same_settings(self) -> None:
        if not _hermetic_env.APPLIED:
            self.skipTest("MAC_MCP_TEST_AMBIENT_ENV=1 keeps the caller's environment")
        for name, value in _hermetic_env.BASELINE.items():
            self.assertEqual(value, os.environ.get(name), name)
        for name in _hermetic_env.AMBIENT:
            self.assertNotIn(name, os.environ)


if __name__ == "__main__":
    unittest.main()
