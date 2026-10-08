from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from mcp_server.version import __version__

ROOT = Path(__file__).resolve().parents[1]
RELEASE_DOC = ROOT / "RELEASE.md"
MANIFEST = ROOT / "release" / "stable-manifest.json"


def _current_block() -> dict[str, str]:
    text = RELEASE_DOC.read_text(encoding="utf-8")
    section = text.split("### Current release", 1)[1].split("### Historical", 1)[0]
    block = re.findall(r"```text\n(.*?)```", section, re.S)
    if len(block) != 1:
        raise AssertionError("RELEASE.md must have exactly one text block under Current release")
    return dict(line.split(": ", 1) for line in block[0].strip().splitlines())


# RELEASE.md is the owner's local release policy (excluded from the repository),
# so a fresh checkout has nothing to compare.
@unittest.skipUnless(RELEASE_DOC.exists(), "RELEASE.md is local-only and absent from this checkout")
class ReleaseDocumentationTests(unittest.TestCase):
    def test_current_block_matches_the_signed_manifest(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        current = _current_block()
        self.assertEqual(manifest["release_id"], current["release_id"])
        self.assertEqual(manifest["version"], current["product_version"])
        self.assertEqual(f"v{manifest['version']}", current["git_tag"])
        artifacts = manifest.get("artifacts") or []
        if artifacts:
            self.assertNotEqual("none", current["native_artifacts"])
        else:
            self.assertEqual("none", current["native_artifacts"])

    def test_documented_version_matches_the_product_version(self) -> None:
        self.assertEqual(__version__, _current_block()["product_version"])

    def test_old_release_stays_labelled_historical(self) -> None:
        text = RELEASE_DOC.read_text(encoding="utf-8")
        self.assertNotIn("Current 2.1.5", text)
        historical = text.split("### Historical 2.1.5 baseline", 1)[1].split("\n## ", 1)[0]
        # Immutable facts of the old release are kept as they were.
        self.assertIn("release_id: stable-2.1.5-20260918", historical)
        self.assertIn("commit: 955015bc6ffbf11334bd923d805261f8321f57e5", historical)


if __name__ == "__main__":
    unittest.main()
