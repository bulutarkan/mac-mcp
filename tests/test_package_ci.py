"""The package is built, installed and upgraded in CI, and each published package is checked (#189)."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github" / "workflows" / "package.yml").read_text(encoding="utf-8")
CHECK = (ROOT / "scripts" / "check_pkg.sh").read_text(encoding="utf-8")


class PackageCiTests(unittest.TestCase):
    def test_package_changes_build_and_install_a_package(self) -> None:
        for trigger in ("pull_request:", "push:", "release:", "workflow_dispatch:"):
            self.assertIn(trigger, WORKFLOW)
        for path in ("packaging/**", "scripts/build_pkg.sh", "scripts/check_pkg.sh", "requirements.lock",
                     "menu_app/ServerHelper/**", "tests/test_pkg_staged_install.py"):
            self.assertEqual(2, WORKFLOW.count(f'- "{path}"'), path)  # pull requests and pushes
        self.assertIn("scripts/build_pkg.sh --allow-unverified", WORKFLOW)
        self.assertIn("scripts/check_pkg.sh dist/Mac-MCP-*-unverified-*-arm64.pkg", WORKFLOW)

    def test_a_published_release_checks_its_own_package(self) -> None:
        self.assertIn("gh release download \"$TAG\"", WORKFLOW)
        self.assertIn("shasum -a 256 -c ./*.pkg.sha256", WORKFLOW)
        self.assertIn('release_id=stable-${TAG#v}', WORKFLOW)  # v2.2.0-r1 -> stable-2.2.0-r1
        self.assertTrue(re.search(r'scripts/check_pkg\.sh "\$RUNNER_TEMP"/release/\*\.pkg "\$\{\{ steps\.release\.outputs\.release_id \}\}"', WORKFLOW))

    def test_the_check_covers_signature_payload_release_and_a_real_install(self) -> None:
        for needed in ("pkgutil --check-signature", "no signature", "pkgutil --payload-files",
                       "./Applications/Mac MCP.app", "./.mac-mcp/pkg/staging", "pkgutil --expand-full",
                       "codesign --verify --deep --strict", "source_sha256",
                       "MAC_MCP_PKG_STAGE=\"$PAYLOAD\"", "tests.test_pkg_staged_install"):
            self.assertIn(needed, CHECK, needed)


if __name__ == "__main__":
    unittest.main()
