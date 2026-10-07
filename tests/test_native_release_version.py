from __future__ import annotations

import plistlib
import unittest
from pathlib import Path

from mcp_server.version import __version__


ROOT = Path(__file__).resolve().parents[1]
APP_INFO = ROOT / "menu_app" / "Info.plist"
EXT_INFO = ROOT / "menu_app" / "SafariExtension" / "Info.plist"


class NativeReleaseVersionTests(unittest.TestCase):
    def test_native_public_version_matches_product_version(self) -> None:
        with APP_INFO.open("rb") as handle:
            app = plistlib.load(handle)
        self.assertEqual(__version__, app["CFBundleShortVersionString"])

    def test_app_and_extension_build_numbers_are_monotonic_and_aligned(self) -> None:
        with APP_INFO.open("rb") as handle:
            app = plistlib.load(handle)
        with EXT_INFO.open("rb") as handle:
            extension = plistlib.load(handle)

        app_build = str(app["CFBundleVersion"])
        extension_build = str(extension["CFBundleVersion"])
        self.assertTrue(app_build.isdigit())
        self.assertEqual(app_build, extension_build)
        # Build 220 was the 2.1.8 r6 native baseline. Native sources changed
        # after that checkpoint, so subsequent distributed builds must advance.
        self.assertGreaterEqual(int(app_build), 221)


if __name__ == "__main__":
    unittest.main()
