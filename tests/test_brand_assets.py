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
# Opaque on purpose: the social card, and home-screen icons (iOS turns transparency into black).
OPAQUE = {"assets/brand/social-preview.png", "site/assets/img/apple-touch-icon.png",
          "mcp_server/mobile/icon-192.png", "mcp_server/mobile/icon-512.png"}
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

    def test_the_app_build_uses_the_small_icons(self) -> None:
        text = (ROOT / "menu_app/build_app.sh").read_text(encoding="utf-8")
        self.assertIn("AppIconSmall/${size}.png", text)


class ServerHelperKeepsItsPermissions(unittest.TestCase):
    """The helper holds the owner's Accessibility and Screen Recording grants, which macOS ties to its
    exact ad-hoc binary. A new icon would rebuild it and drop them, so the helper keeps a frozen icon."""

    FROZEN_SHA256 = "20678a6267ae8c654b14978cf143f3ff7f22c4f0e6d1ade75de493b3dd5f6ce8"

    def test_the_helper_builds_from_its_own_frozen_icon(self) -> None:
        import hashlib
        text = (ROOT / "menu_app/ServerHelper/build_helper.sh").read_text(encoding="utf-8")
        self.assertIn('ICON="${MAC_MCP_HELPER_ICON:-${SCRIPT_DIR}/AppIcon.png}"', text)
        self.assertNotIn("AppIconSmall", text)
        frozen = (ROOT / "menu_app/ServerHelper/AppIcon.png").read_bytes()
        self.assertEqual(self.FROZEN_SHA256, hashlib.sha256(frozen).hexdigest())


class RenderedPngs(unittest.TestCase):
    def test_every_png_has_its_size_and_an_alpha_channel(self) -> None:
        for relative, width, height in PNGS:
            with self.subTest(png=relative):
                w, h, colour = png_header(ROOT / relative)
                self.assertEqual((width, height), (w, h))
                if relative in OPAQUE:
                    continue  # full pictures (social card, home-screen icons): opaque on purpose
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



class SiteBrandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.page = (ROOT / "site" / "index.html").read_text(encoding="utf-8")

    def test_the_brand_mark_follows_the_page_theme(self) -> None:
        # Bronze on the light page, champagne on the dark one; the theme switch can override the
        # Mac's setting, so the swap is done in CSS rather than with a media-query <picture>.
        brand = self.page[self.page.index('<a class="brand"'):][:400]
        self.assertIn('<img class="on-light" src="assets/img/mark-bronze.svg"', brand)
        self.assertIn('<img class="on-dark" src="assets/img/mark.svg"', brand)
        css = (ROOT / "site" / "css" / "site.css").read_text(encoding="utf-8")
        self.assertIn(".on-light { display: var(--show-light); }", css)
        self.assertIn(".on-dark { display: var(--show-dark); }", css)

    def test_the_island_uses_the_champagne_mark(self) -> None:
        self.assertIn('<img class="island-mark" src="assets/img/mark.svg"', self.page)

    def test_icons_for_tabs_and_home_screens(self) -> None:
        # The tab icon keeps the obsidian tile: a bare champagne mark vanishes on a light tab bar.
        self.assertIn('<link rel="icon" type="image/png" href="assets/img/app-icon-256.png?v=2">', self.page)
        self.assertIn('<link rel="apple-touch-icon" href="assets/img/apple-touch-icon.png?v=2">', self.page)
        self.assertTrue((ROOT / "site" / "assets" / "img" / "mark-bronze.svg").is_file())


MARK_PATH = 'd="M14 49 V15 L32 33 L50 15 V29"'


class ServerPageBrandTests(unittest.TestCase):
    """The mobile page, the dashboard and the ChatGPT panel show the monogram on an obsidian tile."""

    def test_mobile_and_dashboard_show_the_mark(self) -> None:
        for page in ("mcp_server/mobile/index.html", "mcp_server/dashboard/index.html"):
            with self.subTest(page=page):
                text = (ROOT / page).read_text(encoding="utf-8")
                self.assertIn(MARK_PATH, text)
                self.assertNotIn("M6 8.5 16 3l10 5.5v15L16 29 6 23.5z", text)  # the old hexagon

    def test_chatgpt_panel_source_and_build_show_the_mark(self) -> None:
        for page in ("chatgpt_ui/src/index.html", "mcp_server/chatgpt_ui/control-center.html"):
            with self.subTest(page=page):
                text = (ROOT / page).read_text(encoding="utf-8")
                self.assertIn(MARK_PATH, text)
                self.assertNotIn('d="M21 19.5 34 32 21 44.5"', text)  # the old terminal prompt

    def test_the_mobile_manifest_has_home_screen_icons(self) -> None:
        import json
        manifest = json.loads((ROOT / "mcp_server/mobile/manifest.json").read_text())
        sizes = {icon["sizes"]: icon["src"] for icon in manifest["icons"]}
        self.assertEqual({"192x192": "/mobile/assets/icon-192.png", "512x512": "/mobile/assets/icon-512.png"}, sizes)
        page = (ROOT / "mcp_server/mobile/index.html").read_text(encoding="utf-8")
        self.assertIn('<link rel="apple-touch-icon" href="/mobile/assets/icon-192.png">', page)

    def test_the_server_serves_the_mobile_icons(self) -> None:
        import tempfile
        from starlette.testclient import TestClient
        from tests import test_mobile_dashboard as fixture
        with tempfile.TemporaryDirectory() as td:
            app, _telemetry, _store = fixture.MobileDashboardTests().make_app(Path(td))
            client = TestClient(app)
            for name in ("icon-192.png", "icon-512.png"):
                with self.subTest(icon=name):
                    reply = client.get(f"/mobile/assets/{name}")
                    self.assertEqual(200, reply.status_code)
                    self.assertEqual("image/png", reply.headers["content-type"])
                    self.assertEqual(b"\x89PNG", reply.content[:4])


class VideoBrandTests(unittest.TestCase):
    """The demo video mirrors the app: the bare mark in the menu header and the island, the app icon at the end."""

    def setUp(self) -> None:
        self.components = (ROOT / "video/src/components.tsx").read_text(encoding="utf-8")
        self.scenes = (ROOT / "video/src/scenes.tsx").read_text(encoding="utf-8")

    def test_the_mark_and_the_icon_are_drawn_as_they_are(self) -> None:
        self.assertIn('staticFile("mark.svg")', self.components)
        icon = self.components[self.components.index("export const AppIcon"):][:300]
        self.assertNotIn("borderRadius", icon)  # the icon has its own tile and transparent margin
        self.assertTrue((ROOT / "video/public/mark.svg").is_file())

    def test_scenes_use_the_mark_where_the_app_does(self) -> None:
        self.assertIn("<BrandMark size={52} />", self.scenes)
        self.assertIn("<BrandMark size={56} />", self.scenes)
        self.assertIn("<AppIcon size={184} />", self.scenes)
        self.assertNotIn("<AppIcon size={150} style={{ boxShadow", self.scenes)


class HomeScreenIcons(unittest.TestCase):
    """iOS fills transparent pixels with black and adds its own corners, so home-screen icons are full bleed."""

    @unittest.skipUnless(MAGICK, "ImageMagick is not installed")
    def test_touch_and_pwa_icons_are_opaque_to_the_corner(self) -> None:
        for relative in ("site/assets/img/apple-touch-icon.png", "mcp_server/mobile/icon-192.png", "mcp_server/mobile/icon-512.png"):
            with self.subTest(icon=relative):
                self.assertEqual("True", magick(str(ROOT / relative), "-format", "%[opaque]", "info:"))

    def test_the_manifest_marks_the_icons_maskable(self) -> None:
        import json
        manifest = json.loads((ROOT / "mcp_server/mobile/manifest.json").read_text())
        self.assertTrue(all(icon.get("purpose") == "any maskable" for icon in manifest["icons"]))


class SiteCacheBusting(unittest.TestCase):
    def test_new_images_get_new_urls(self) -> None:
        page = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
        self.assertIn('href="assets/img/app-icon-256.png?v=2"', page)
        self.assertIn('og-image.jpg?v=2"', page)


class VideoEndCard(unittest.TestCase):
    def test_the_end_card_icon_keeps_its_old_visible_size(self) -> None:
        # The new icon has its own 10 % transparent margin; 184 shows the tile as large as the old 150.
        self.assertIn("<AppIcon size={184} />", (ROOT / "video/src/scenes.tsx").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
