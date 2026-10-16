"""The Mac MCP monogram: hand-authored SVG sources and the PNGs rendered from them (#187, #204)."""
from __future__ import annotations

import shutil
import struct
import subprocess
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRAND = ROOT / "assets" / "brand"
SOURCES = ("mark.svg", "mark-bronze.svg", "mark-mono.svg", "app-icon.svg")
# (path, width, height): every PNG the surfaces use, rendered by scripts/build_brand.py.
PNGS = (
    ("menu_app/Assets/AppIcon.png", 1024, 1024),
    ("menu_app/Assets/BrandMark.png", 64, 64),
    ("menu_app/Assets/BrandMarkLight.png", 64, 64),
    ("menu_app/Assets/MenuBarMark.png", 18, 18),
    ("menu_app/Assets/MenuBarMark@2x.png", 36, 36),
    ("site/assets/img/app-icon-256.png", 256, 256),
    ("site/assets/img/apple-touch-icon.png", 180, 180),
    ("video/public/app-icon.png", 512, 512),
    ("mcp_server/mobile/icon-192.png", 192, 192),
    ("mcp_server/mobile/icon-512.png", 512, 512),
    ("assets/brand/social-preview.png", 1280, 640),
    ("menu_app/Assets/AppIconSmall/16.png", 16, 16),
    ("menu_app/Assets/AppIconSmall/32.png", 32, 32),
    ("menu_app/Assets/AppIconSmall/64.png", 64, 64),
)
MAGICK = shutil.which("magick")
RSVG = shutil.which("rsvg-convert")


def png_header(path: Path):
    data = path.read_bytes()[:33]
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path} is not a PNG"
    width, height, depth, colour = struct.unpack(">IIBB", data[16:26])
    return width, height, colour


def magick(*args: str) -> str:
    return subprocess.run([MAGICK, *args], capture_output=True, text=True, check=True).stdout.strip()


class BrandSources(unittest.TestCase):
    def test_sources_are_plain_vector_files(self) -> None:
        for name in SOURCES:
            with self.subTest(source=name):
                text = (BRAND / name).read_text(encoding="utf-8")
                root = ET.fromstring(text)
                self.assertTrue(root.tag.endswith("svg"))
                self.assertIn("viewBox", root.attrib)
                tags = {element.tag.split("}")[-1] for element in root.iter()}
                self.assertFalse(tags & {"script", "image", "foreignObject", "font", "style"}, tags)
                self.assertNotIn("http://", text.replace("http://www.w3.org/2000/svg", ""))
                self.assertNotIn("https://", text)

    def test_the_mark_is_the_approved_geometry(self) -> None:
        for name in ("mark.svg", "mark-bronze.svg", "mark-mono.svg"):
            with self.subTest(source=name):
                text = (BRAND / name).read_text(encoding="utf-8")
                self.assertIn('d="M14 49 V15 L32 33 L50 15 V29"', text)
                self.assertIn("translate(50 33.2) rotate(22) scale(0.8) translate(-3.4 -3.2)", text)
                self.assertIn('d="M3.4 3.2 23 11.2 14.6 14.4 11.3 23.2Z"', text)
        self.assertIn("#f1e7d2", (BRAND / "mark.svg").read_text())
        self.assertIn("#9c8a66", (BRAND / "mark-bronze.svg").read_text())
        self.assertIn("currentColor", (BRAND / "mark-mono.svg").read_text())


class SmallAppIcons(unittest.TestCase):
    """Finder lists and small Dock sizes get hand-tuned icons with a heavier line, not a shrunk 1024."""

    def test_both_bundle_builds_use_the_small_icons(self) -> None:
        for script in ("menu_app/build_app.sh", "menu_app/ServerHelper/build_helper.sh"):
            with self.subTest(script=script):
                text = (ROOT / script).read_text(encoding="utf-8")
                self.assertIn("AppIconSmall/${size}.png", text)


class RenderedPngs(unittest.TestCase):
    def test_every_png_has_its_size_and_an_alpha_channel(self) -> None:
        for relative, width, height in PNGS:
            with self.subTest(png=relative):
                w, h, colour = png_header(ROOT / relative)
                self.assertEqual((width, height), (w, h))
                if relative == "assets/brand/social-preview.png":
                    continue  # a social card is a full picture: opaque on purpose
                self.assertIn(colour, (4, 6), "the PNG needs an alpha channel")

    @unittest.skipUnless(MAGICK, "ImageMagick is not installed")
    def test_the_app_icon_is_transparent_outside_its_tile(self) -> None:
        icon = str(ROOT / "menu_app/Assets/AppIcon.png")
        self.assertEqual("0", magick(icon, "-format", "%[fx:int(p{4,4}.a*255)]", "info:"))
        self.assertEqual("255", magick(icon, "-format", "%[fx:int(p{512,200}.a*255)]", "info:"))

    @unittest.skipUnless(MAGICK, "ImageMagick is not installed")
    def test_the_menu_bar_mark_is_a_template_black_and_alpha(self) -> None:
        for name in ("MenuBarMark.png", "MenuBarMark@2x.png"):
            with self.subTest(png=name):
                path = str(ROOT / "menu_app/Assets" / name)
                self.assertEqual("0", magick(path, "-alpha", "off", "-format", "%[fx:int(maxima*255)]", "info:"))
                self.assertNotEqual("0", magick(path, "-alpha", "extract", "-format", "%[fx:int(maxima*255)]", "info:"))

    @unittest.skipUnless(MAGICK and RSVG, "rsvg-convert or ImageMagick is not installed")
    def test_rendered_files_match_their_sources(self) -> None:
        proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_brand.py"), "--check"],
                              capture_output=True, text=True)
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()


class SiteBrandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.page = (ROOT / "site" / "index.html").read_text(encoding="utf-8")

    def test_the_brand_mark_follows_the_page_theme(self) -> None:
        brand = self.page[self.page.index('<a class="brand"'):][:400]
        self.assertIn('<source srcset="assets/img/mark-bronze.svg" media="(prefers-color-scheme: light)">', brand)
        self.assertIn('<img src="assets/img/mark.svg" alt="" width="28" height="28">', brand)

    def test_the_island_uses_the_champagne_mark(self) -> None:
        self.assertIn('<img class="island-mark" src="assets/img/mark.svg"', self.page)

    def test_icons_for_tabs_and_home_screens(self) -> None:
        # The tab icon keeps the obsidian tile: a bare champagne mark vanishes on a light tab bar.
        self.assertIn('<link rel="icon" type="image/png" href="assets/img/app-icon-256.png">', self.page)
        self.assertIn('<link rel="apple-touch-icon" href="assets/img/apple-touch-icon.png">', self.page)
        self.assertTrue((ROOT / "site" / "assets" / "img" / "mark-bronze.svg").is_file())
