import struct
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ICON = ROOT / "menu_app" / "Assets" / "AppIcon.png"


class AppBrandingTests(unittest.TestCase):
    """#102: the app icon is part of the repository and every build uses it."""

    def test_icon_source_is_a_square_png_of_at_least_1024_px(self) -> None:
        data = ICON.read_bytes()
        self.assertEqual(b"\x89PNG\r\n\x1a\n", data[:8])
        width, height = struct.unpack(">II", data[16:24])
        self.assertEqual(width, height)
        self.assertGreaterEqual(width, 1024)

    def test_build_uses_the_repository_icon_and_fails_without_one(self) -> None:
        script = (ROOT / "menu_app" / "build_app.sh").read_text(encoding="utf-8")
        self.assertNotIn("Downloads", script)
        self.assertIn('${SCRIPT_DIR}/Assets/AppIcon.png', script)
        self.assertRegex(script, r'if \[\[ ! -f "\$\{ICON_SOURCE\}" \]\]; then')
        self.assertIn('[[ -f "${RESOURCES}/AppIcon.icns" ]]', script)

    def test_visible_copy_names_the_product_mac_mcp(self) -> None:
        # Identifiers are lowercase (mac-mcp); the product is written "Mac MCP".
        for path in (ROOT / "menu_app" / "Sources").glob("*.swift"):
            self.assertNotIn("Mac-MCP", path.read_text(encoding="utf-8"), path.name)


if __name__ == "__main__":
    unittest.main()
