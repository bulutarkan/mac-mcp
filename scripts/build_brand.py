#!/usr/bin/env python3
"""Render every Mac MCP logo image from the SVG sources in assets/brand (#187, #204).

  build_brand.py           write all outputs into their places in the repository
  build_brand.py --check   render into a temporary folder and fail if any output differs

The sources are hand-authored SVG. This script only renders them (rsvg-convert) and, for the
JPEG social image, encodes it (ImageMagick); both are Homebrew tools on the build Mac. Small
renders get a heavier line so that it never falls below about 1.4 px.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRAND = ROOT / "assets" / "brand"
HEADLINE = "Let your AI apps use your Mac."
SUBLINE = "Mac MCP · Open source, MIT · bulutarkan.github.io/mac-mcp"


def mark(source: str, weight: float, colour: str | None = None) -> str:
    """A mark source with its M at `weight` (and, for the mono source, a solid colour)."""
    text = (BRAND / source).read_text(encoding="utf-8")
    text = re.sub(r'(<path d="M14 49 V15 L32 33 L50 15 V29"[^>]*stroke-width=")[0-9.]+', rf"\g<1>{weight}", text)
    return text.replace("currentColor", colour) if colour else text


def app_icon(weight: float = 4.6, inset: float = 0.68) -> str:
    """The app icon with its mark at `weight` and `inset` of the 824-unit tile (small sizes: heavier, larger)."""
    text = (BRAND / "app-icon.svg").read_text(encoding="utf-8")
    side = 824 * inset
    corner = 100 + (824 - side) / 2
    text = text.replace('<svg x="232" y="232" width="560" height="560"',
                        f'<svg x="{corner:g}" y="{corner:g}" width="{side:g}" height="{side:g}"')
    return re.sub(r'(<path d="M14 49 V15 L32 33 L50 15 V29"[^>]*stroke-width=")[0-9.]+', rf"\g<1>{weight}", text)


def touch_icon() -> str:
    """Home-screen icon (iOS, PWA): an opaque full-bleed obsidian square. iOS fills transparent
    pixels with black and rounds the corners itself, so the macOS margin and shadow stay out."""
    inner = mark("mark.svg", 4.8).replace("<svg ", '<svg x="204" y="204" width="616" height="616" ', 1)
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024"><defs>'
            '<linearGradient id="tile" x1="330" y1="0" x2="694" y2="1024" gradientUnits="userSpaceOnUse">'
            '<stop offset="0" stop-color="#34363d"/><stop offset="1" stop-color="#111216"/></linearGradient></defs>'
            f'<rect width="1024" height="1024" fill="url(#tile)"/>{inner}</svg>')


def social(width: int, height: int) -> str:
    """The social preview card: the mark, the headline and one line of facts on obsidian."""
    inner = mark("mark.svg", 4.2)
    inner = inner.replace("<svg ", f'<svg x="{width / 2 - 70}" y="{height * 0.17:.0f}" width="140" height="140" ', 1)
    title_y, sub_y = height * 0.66, height * 0.66 + 54
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}">'
            f'<defs><radialGradient id="bg" cx="50%" cy="38%" r="75%"><stop offset="0" stop-color="#26282e"/>'
            f'<stop offset="1" stop-color="#0d0e11"/></radialGradient></defs>'
            f'<rect width="{width}" height="{height}" fill="url(#bg)"/>{inner}'
            f'<text x="{width / 2}" y="{title_y:.0f}" text-anchor="middle" font-family="Georgia, \'Times New Roman\', serif" '
            f'font-weight="700" font-size="60" fill="#f4f1ea">{HEADLINE}</text>'
            f'<text x="{width / 2}" y="{sub_y:.0f}" text-anchor="middle" font-family="Helvetica Neue, Helvetica, Arial, sans-serif" '
            f'font-size="24" fill="#a9a59b">{SUBLINE}</text></svg>')


# (output path, how to make its SVG, size in pixels)
OUTPUTS = [
    ("menu_app/Assets/AppIcon.png", app_icon, (1024, 1024)),
    ("menu_app/Assets/BrandMark.png", lambda: mark("mark.svg", 5.0), (64, 64)),
    ("menu_app/Assets/BrandMarkLight.png", lambda: mark("mark-bronze.svg", 5.0), (64, 64)),
    ("menu_app/Assets/MenuBarMark.png", lambda: mark("mark-mono.svg", 5.6, "#000000"), (18, 18)),
    ("menu_app/Assets/MenuBarMark@2x.png", lambda: mark("mark-mono.svg", 5.6, "#000000"), (36, 36)),
    ("site/assets/img/app-icon-256.png", app_icon, (256, 256)),
    ("site/assets/img/apple-touch-icon.png", touch_icon, (180, 180)),
    ("video/public/app-icon.png", app_icon, (512, 512)),
    ("mcp_server/mobile/icon-192.png", touch_icon, (192, 192)),
    ("mcp_server/mobile/icon-512.png", touch_icon, (512, 512)),
    ("assets/brand/social-preview.png", lambda: social(1280, 640), (1280, 640)),
    # Finder lists and small Dock sizes: a heavier line and a larger mark than a shrunk 1024 gives.
    ("menu_app/Assets/AppIconSmall/16.png", lambda: app_icon(7.2, 0.82), (16, 16)),
    ("menu_app/Assets/AppIconSmall/32.png", lambda: app_icon(6.2, 0.78), (32, 32)),
    ("menu_app/Assets/AppIconSmall/64.png", lambda: app_icon(5.2, 0.72), (64, 64)),
    ("site/assets/img/og-image.jpg", lambda: social(1200, 630), (1200, 630)),
]
COPIES = [("assets/brand/mark.svg", "site/assets/img/mark.svg"), ("assets/brand/mark-bronze.svg", "site/assets/img/mark-bronze.svg"), ("assets/brand/mark.svg", "menu_app/Assets/BrandMark.svg"),
          ("assets/brand/mark-bronze.svg", "menu_app/Assets/BrandMarkBronze.svg"),
          ("assets/brand/mark.svg", "video/public/mark.svg")]


def tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        sys.exit(f"build_brand: {name} is not installed (brew install librsvg imagemagick)")
    return path


def render(svg_text: str, size: tuple, out: Path, work: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    source = work / (out.name + ".svg")
    source.write_text(svg_text, encoding="utf-8")
    png = out if out.suffix == ".png" else work / (out.name + ".png")
    subprocess.run([tool("rsvg-convert"), "-w", str(size[0]), "-h", str(size[1]), str(source), "-o", str(png)], check=True)
    if out.suffix == ".jpg":
        # Strip metadata so the same source always gives the same bytes.
        subprocess.run([tool("magick"), str(png), "-strip", "-quality", "88", str(out)], check=True)


def differs(a: Path, b: Path) -> bool:
    if not a.exists():
        return True
    fuzz = ["-fuzz", "3%"] if a.suffix == ".jpg" else []
    proc = subprocess.run([tool("magick"), "compare", *fuzz, "-metric", "AE", str(a), str(b), "null:"],
                          capture_output=True, text=True)
    count = (proc.stderr or proc.stdout).strip().split(" ")[0]
    try:
        return float(count) > 0
    except ValueError:
        return True


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if any rendered file is out of date")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        stale = []
        for relative, make, size in OUTPUTS:
            target = ROOT / relative
            out = work / "out" / relative if args.check else target
            render(make(), size, out, work)
            if args.check and differs(target, out):
                stale.append(relative)
        for source, relative in COPIES:
            target = ROOT / relative
            if args.check:
                if not target.exists() or target.read_bytes() != (ROOT / source).read_bytes():
                    stale.append(relative)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / source, target)
    if stale:
        print("Out of date (run scripts/build_brand.py):\n  " + "\n  ".join(stale))
        return 1
    print("brand images are up to date" if args.check else f"rendered {len(OUTPUTS)} images and {len(COPIES)} copies")
    return 0


if __name__ == "__main__":
    sys.exit(main())
