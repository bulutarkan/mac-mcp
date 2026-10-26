"""The product site (#101), its release data, the README front door (#57) and the demo video."""
from __future__ import annotations

import html
import importlib.util
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "site"
_spec = importlib.util.spec_from_file_location("build_site", ROOT / "scripts" / "build_site.py")
build_site = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_site)

MANIFEST = json.loads((ROOT / "release" / "stable-manifest.json").read_text())


def fixture_release() -> dict:
    return json.loads((FIXTURES / "release_latest.json").read_text())


def fixture_sha() -> str:
    return (FIXTURES / "release.pkg.sha256").read_text()


def manifest_for(version: str, release_id: str):
    return lambda tag: {"version": version, "release_id": release_id, "payload_sha256": "f" * 64}


def build(release=None, sha=None, **kwargs):
    release = release if release is not None else fixture_release()
    defaults = {"manifest_at": manifest_for("2.1.9", "stable-2.1.9-r10"), "verify": lambda tag: None, "min_release": "2.1.0"}
    defaults.update(kwargs)
    return build_site.release_data(release, sha if sha is not None else fixture_sha(), **defaults)


class ReleaseDataTests(unittest.TestCase):
    def test_the_real_release_renders_with_its_exact_package(self) -> None:
        data = build()
        self.assertEqual("2.1.9", data["version"])
        self.assertEqual("stable-2.1.9-r10", data["release_id"])
        self.assertEqual("https://github.com/bulutarkan/mac-mcp/releases/download/v2.1.9-r10/"
                         "Mac-MCP-2.1.9-stable-2.1.9-r10-arm64.pkg", data["pkg_url"])
        self.assertEqual("5b25c415a7a71914f17390ae1373ab81272857d2ebcd1e1d1b3caa6a29ecbd1a", data["pkg_sha256"])
        self.assertEqual("99", data["pkg_size_mb"])
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "site"
            build_site.render(data, out)
            page = (out / "index.html").read_text()
            self.assertNotIn("{{", page)
            self.assertIn("Download Mac MCP 2.1.9", page)
            self.assertIn(data["pkg_sha256"], page)
            self.assertNotIn("preview-banner", page)
            checked = build_site.check(out)
            self.assertEqual(data["release_id"], checked["release_id"])
            self.assertFalse((out / "js" / "site.test.mjs").exists())
            # Every local file the page loads is tied to its content, for any CDN in front of a copy.
            refs = re.findall(r'(?:href|src|poster)="((?:css|js|assets)/[^"]+)"', page)
            self.assertTrue(refs)
            for ref in refs:
                self.assertRegex(ref, r"\?v=[0-9a-f]{12}$", ref)
            self.assertRegex((out / "css" / "site.css").read_text(), r'url\("\.\./assets/fonts/sora\.woff2\?v=[0-9a-f]{12}"\)')

    def test_releases_that_must_not_be_advertised_are_refused(self) -> None:
        release = fixture_release()
        cases = {
            "draft": dict(release, draft=True),
            "prerelease": dict(release, prerelease=True),
            "bad tag": dict(release, tag_name="2.1.9"),
            "missing pkg": dict(release, assets=[a for a in release["assets"] if not a["name"].endswith(".pkg")]),
            "digest mismatch": dict(release, assets=[dict(a, digest="sha256:" + "0" * 64) if a["name"].endswith(".pkg") else a
                                                     for a in release["assets"]]),
            "foreign url": dict(release, assets=[dict(a, browser_download_url="https://example.com/x.pkg") if a["name"].endswith(".pkg") else a
                                                 for a in release["assets"]]),
        }
        for name, bad in cases.items():
            with self.subTest(name), self.assertRaises(build_site.SiteBuildError):
                build(release=bad)
        with self.assertRaises(build_site.SiteBuildError):
            build(manifest_at=manifest_for("2.1.9", "stable-2.1.9-r9"))
        with self.assertRaises(build_site.SiteBuildError):
            build(sha="5b25c415  Other-name.pkg\n")
        with self.assertRaises(build_site.SiteBuildError):
            build(sha="not-a-hash  Mac-MCP-2.1.9-stable-2.1.9-r10-arm64.pkg\n")

        def refuse(tag):
            raise build_site.SiteBuildError("signature does not verify")

        with self.assertRaises(build_site.SiteBuildError):
            build(verify=refuse)

    def test_releases_older_than_the_site_are_not_deployed(self) -> None:
        with self.assertRaises(build_site.ReleaseTooOld):
            build(min_release=build_site.MIN_RELEASE)
        self.assertEqual("2.2.0", build_site.MIN_RELEASE)

    def test_offline_preview_matches_the_manifest_and_cannot_deploy(self) -> None:
        data = build_site.offline_data()
        self.assertEqual((MANIFEST["version"], MANIFEST["release_id"]), (data["version"], data["release_id"]))
        self.assertTrue(data["preview"])
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "site"
            build_site.render(data, out)
            self.assertIn("preview-banner", (out / "index.html").read_text())
            with self.assertRaises(build_site.SiteBuildError):
                build_site.check(out)

    def test_values_are_escaped(self) -> None:
        data = build()
        data["release_id"] = '<script>alert(1)</script>'
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "site"
            build_site.render(data, out)
            page = (out / "index.html").read_text()
            self.assertNotIn("<script>alert", page)
            self.assertIn(html.escape(data["release_id"]), page)


class PageTests(unittest.TestCase):
    page = (SITE / "index.html").read_text()

    def test_the_template_names_no_version_and_fills_every_value(self) -> None:
        self.assertIsNone(re.search(r"\b\d+\.\d+\.\d+\b", self.page), "versions come from the release, not the template")
        for key in build_site.PLACEHOLDERS:
            if key != "tag":  # kept in release.json; the page names the release by its ID
                self.assertIn("{{" + key + "}}", self.page, key)

    def test_content_security(self) -> None:
        self.assertIn('http-equiv="Content-Security-Policy"', self.page)
        self.assertIn("script-src 'self'", self.page)
        scripts = re.findall(r"<script([^>]*)>(.*?)</script>", self.page, re.S)
        for attrs, body in scripts:
            if 'type="application/ld+json"' in attrs:
                continue
            self.assertIn("src=", attrs)
            self.assertEqual("", body.strip())
        self.assertNotIn("style=", self.page)
        for value in re.findall(r'\ssrc="([^"]+)"', self.page):
            if value.startswith("https://"):
                # MacNative's official SVG badges are the only external image assets.
                self.assertRegex(value, r"^https://macnative\.io/badge\?kind=listed&amp;theme=(?:light|dark)&amp;slug=mac-mcp&amp;small=1$")
            else:
                self.assertFalse(value.startswith(("http:", "//")), value)
        self.assertNotIn("offers", self.page)
        # The typeface is served from the site itself, with its licence beside it.
        self.assertIn("font-src 'self'", self.page)
        self.assertTrue((SITE / "assets" / "fonts" / "OFL.txt").exists())
        self.assertNotIn("fonts.googleapis", (SITE / "css" / "site.css").read_text())
        # The live star count is the only request that leaves the site.
        self.assertIn("connect-src https://api.github.com;", self.page)

    def test_macnative_small_badge_responds_to_site_theme(self) -> None:
        # Both official embed variants share one link; no third-party script or
        # custom theme toggler is needed because the site already handles Device.
        self.assertIn("img-src 'self' https://macnative.io;", self.page)
        self.assertEqual(1, self.page.count('class="macnative-badge"'))
        self.assertIn('href="https://macnative.io/apps/mac-mcp?utm_source=macnative-badge&amp;utm_medium=referral"', self.page)
        for theme, cls in (("light", "on-light"), ("dark", "on-dark")):
            src = f'https://macnative.io/badge?kind=listed&amp;theme={theme}&amp;slug=mac-mcp&amp;small=1'
            expected = f'<img class="{cls}" src="{src}" alt="Listed on MacNative" width="168" height="44">'
            self.assertIn(expected, self.page)
        styles = (SITE / "css" / "site.css").read_text()
        self.assertIn(':root[data-theme="light"] .on-light { display: block; }', styles)
        self.assertIn(':root:not([data-theme="dark"]):not([data-theme="light"]) .on-light { display: block; }', styles)
        self.assertIn(".macnative-badge {", styles)

    def test_scenes_follow_the_video(self) -> None:
        # Each scene starts where its caption starts in the demo video, with the caption as its title.
        cues = re.findall(r"^00:(\d\d)\.000 --> .*\n(.+)$", (SITE / "assets" / "media" / "demo.en.vtt").read_text(), re.M)
        scenes = re.findall(r'<li class="chapter" data-at="(\d+)"[^>]*>.*?<h3>(.+?)</h3>', self.page, re.S)
        self.assertEqual(6, len(scenes))
        self.assertEqual([(str(int(start)), html.escape(text, quote=False)) for start, text in cues[:6]],
                         [(start, title) for start, title in scenes])

    def test_links_resolve(self) -> None:
        ids = set(re.findall(r'\sid="([^"]+)"', self.page))
        for page_file in SITE.glob("*.html"):
            text = page_file.read_text()
            for value in re.findall(r'\s(?:href|src|poster)="([^"{]+)"', text):
                if value.startswith("#"):
                    self.assertIn(value[1:], ids, value)
                elif value.startswith("https://github.com/bulutarkan/mac-mcp/blob/main/"):
                    path = value.split("/blob/main/", 1)[1]
                    self.assertTrue((ROOT / path).exists(), value)
                elif not value.startswith(("http:", "https:", "mailto:")):
                    target = value.split("#")[0].split("?")[0] or "index.html"
                    resolved = (SITE / target) if not target.endswith("/") else SITE / target / "index.html"
                    if target == "./":
                        resolved = SITE / "index.html"
                    self.assertTrue(resolved.exists(), f"{page_file.name}: {value}")
        for track in re.findall(r'<track[^>]+src="([^"]+)"', self.page):
            self.assertTrue((SITE / track).exists(), track)

    def test_size_budgets(self) -> None:
        # The page draws its demo in HTML and CSS instead of loading video: the first load stays small.
        self.assertLessEqual((SITE / "index.html").stat().st_size, 60_000)
        css = sum(path.stat().st_size for path in (SITE / "css").glob("*.css"))
        self.assertLessEqual(css, 70_000)
        self.assertLessEqual((SITE / "js" / "site.js").stat().st_size, 12_000)
        scripts = sum(path.stat().st_size for path in (SITE / "js").glob("*.js") if not path.name.endswith(".test.mjs"))
        self.assertLessEqual(scripts, 28_000)
        fonts = sum((SITE / "assets" / "fonts" / name).stat().st_size for name in ("sora.woff2", "geist.woff2", "jetbrains-mono.woff2"))
        self.assertLessEqual(fonts, 120_000)
        first_load = css + (SITE / "index.html").stat().st_size + scripts + fonts
        first_load += sum((SITE / "assets" / "img" / name).stat().st_size for name in ("app-icon-256.png",))
        first_load += (SITE / "assets" / "media" / "demo-poster.jpg").stat().st_size
        self.assertLessEqual(first_load, 350_000)
        self.assertLessEqual((SITE / "assets" / "media" / "mac-mcp-demo.mp4").stat().st_size, 4_000_000)
        self.assertLessEqual(sum(path.stat().st_size for path in SITE.rglob("*") if path.is_file()), 6_000_000)

    def test_tokens_match_the_app(self) -> None:
        def variables(text: str) -> list:
            return re.findall(r"(--[a-z-]+):\s*([^;]+);", text)

        onboarding = (ROOT / "menu_app" / "Onboarding" / "onboarding.css").read_text()
        blocks = onboarding.split("* { box-sizing")[0]
        self.assertEqual(variables(blocks), variables((SITE / "css" / "tokens.css").read_text()))


class VideoTests(unittest.TestCase):
    def test_every_label_in_the_video_exists_in_the_app(self) -> None:
        strings = json.loads((ROOT / "video" / "src" / "ui-strings.json").read_text())["strings"]
        sources = "\n".join(path.read_text(errors="ignore") for folder in ("menu_app/Sources", "menu_app/Onboarding")
                            for path in (ROOT / folder).rglob("*") if path.suffix in {".swift", ".html", ".js"})
        scenes = (ROOT / "video" / "src" / "scenes.tsx").read_text()
        for label in strings:
            with self.subTest(label=label):
                self.assertIn(label, sources)
                self.assertIn(label, scenes)

    def test_captions_match_the_scenes(self) -> None:
        captions = re.findall(r"^(?!WEBVTT|\d)(.+)$", (SITE / "assets" / "media" / "demo.en.vtt").read_text(), re.M)
        scenes = (ROOT / "video" / "src" / "scenes.tsx").read_text()
        for caption in captions:
            if caption.startswith("Let your AI apps use your Mac."):
                continue  # the end card shows this in two lines
            self.assertIn(f'text="{caption}"', scenes)

    def test_the_installer_never_needs_node(self) -> None:
        # packaging/npm is the optional npm route (#210); the installer and the .pkg still never need Node.
        npm_route = ROOT / "packaging" / "npm"
        for path in [ROOT / "install.sh", ROOT / "scripts" / "build_pkg.sh", *sorted((ROOT / "packaging").rglob("*"))]:
            if path.is_file() and npm_route not in path.parents:
                text = path.read_text(errors="ignore")
                self.assertNotIn("video/", text, path)
                self.assertNotRegex(text, r"\bnpm\b", path)


class ReadmeTests(unittest.TestCase):
    readme = (ROOT / "README.md").read_text()

    @staticmethod
    def slug(heading: str) -> str:
        text = re.sub(r"[^\w\- ]", "", heading.strip().lower())
        return text.replace(" ", "-")

    def test_front_door(self) -> None:
        first = self.readme.split("\n## ", 1)[0]
        self.assertIn("# Mac MCP\n", first)
        self.assertIn("https://bulutarkan.github.io/mac-mcp/", first)
        self.assertIn("visible, non-focus-stealing", first.lower())
        for promise in ("It works inside your apps", "It browses where you can see it", "It hands off work and reports back"):
            self.assertIn(promise, first)

    def anchors(self, text: str) -> set:
        text = re.sub(r"^```.*?^```", "", text, flags=re.M | re.S)
        return {self.slug(h) for h in re.findall(r"^#{1,6} (.+)$", text, re.M)}

    def test_relative_links_and_anchors_resolve(self) -> None:
        # The README and the task pages it points to (#186): every file and anchor exists.
        pages = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
        for page in pages:
            text = page.read_text()
            for target in re.findall(r"\]\((?!https?:|mailto:)([^)\s]+)\)", text):
                path, _, anchor = target.partition("#")
                file = (page.parent / path).resolve() if path else page
                with self.subTest(page=page.name, target=target):
                    self.assertTrue(file.exists(), target)
                    if anchor and file.suffix == ".md":
                        self.assertIn(anchor, self.anchors(file.read_text()), target)

    def test_readme_is_a_short_front_door(self) -> None:
        # #186: the README starts people off; the manual lives in docs/ and nothing was dropped.
        self.assertLess(self.readme.count("\n"), 120)
        for page in ("connect", "install", "browser", "safety", "menu-app", "agents-and-tasks", "reference"):
            self.assertIn(f"(docs/{page}.md)", self.readme)
            self.assertIn("(../README.md)", (ROOT / "docs" / f"{page}.md").read_text())


if __name__ == "__main__":
    unittest.main()
