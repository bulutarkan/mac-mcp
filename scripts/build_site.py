#!/usr/bin/env python3
"""Build the product site (site/) for GitHub Pages with the exact current release (#101).

The page never hardcodes a version. At deploy time this script reads the latest GitHub
release (the JSON from `gh api repos/<repo>/releases/latest`) and its published
`.pkg.sha256` asset, checks them against the signed release manifest at the release tag,
and renders `{{placeholders}}` in site/*.html into the output folder with a release.json.

Every check fails closed. Releases older than MIN_RELEASE are not deployed, so the page
never describes features that the downloadable release does not have.

  build_site.py --release-json latest.json --sha256-file pkg.sha256 --out _site
  build_site.py --offline --out /tmp/site        # local preview, never deployable
  build_site.py --check _site

The same output is copied to tarkan.cloud/mac-mcp/ and can move to its own domain as it is:
every local URL is relative and carries a content hash.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
MANIFEST = ROOT / "release" / "stable-manifest.json"
REPO = "bulutarkan/mac-mcp"
MIN_RELEASE = "2.2.0"
PLACEHOLDERS = ("version", "release_id", "tag", "pkg_name", "pkg_url", "pkg_sha256", "pkg_size_mb",
                "release_url", "published_date", "preview_banner")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_TAG_RE = re.compile(r"^v(\d+\.\d+\.\d+)-r(\d+)$")
_PAGE_REF = re.compile(r'((?:href|src|poster)=")((?:css|js|assets)/[^"?#]+)(")')
_CSS_REF = re.compile(r'url\("(\.\./[^"?#)]+)"\)')


class SiteBuildError(Exception):
    """A release the site must not advertise, with the reason."""


class ReleaseTooOld(SiteBuildError):
    """The latest release predates the site; nothing is deployed."""


def _version_tuple(version: str) -> tuple:
    return tuple(int(part) for part in version.split("."))


def _git_manifest(tag: str) -> Dict[str, Any]:
    raw = subprocess.check_output(["git", "-C", str(ROOT), "show", f"{tag}:release/stable-manifest.json"])
    return json.loads(raw)


def _verify_signed(tag: str) -> None:
    sys.path.insert(0, str(ROOT))
    from mcp_server import release_trust

    commit = subprocess.check_output(["git", "-C", str(ROOT), "rev-list", "-n", "1", tag], text=True).strip()
    try:
        release_trust.verify_release_commit(
            ROOT, commit, expected_branch="main",
            signers_path=ROOT / "mcp_server" / release_trust.TRUSTED_SIGNERS_FILENAME,
        )
    except release_trust.ReleaseVerificationError as exc:
        raise SiteBuildError(f"the release at {tag} does not verify: {exc}") from exc


def release_data(release: Dict[str, Any], sha256_text: str, *,
                 manifest_at: Callable[[str], Dict[str, Any]] = _git_manifest,
                 verify: Callable[[str], None] = _verify_signed,
                 min_release: str = MIN_RELEASE) -> Dict[str, Any]:
    """The values the page shows, from a GitHub release and its .sha256 asset, all checked."""
    if release.get("draft") or release.get("prerelease"):
        raise SiteBuildError("the latest release is a draft or prerelease")
    tag = str(release.get("tag_name") or "")
    match = _TAG_RE.fullmatch(tag)
    if not match:
        raise SiteBuildError(f"release tag {tag!r} is not v<version>-r<N>")
    version, revision = match.group(1), match.group(2)
    release_id = f"stable-{version}-r{revision}"
    if _version_tuple(version) < _version_tuple(min_release):
        raise ReleaseTooOld(f"release {version} is older than {min_release}; the site is not deployed yet")

    manifest = manifest_at(tag)
    if manifest.get("version") != version or manifest.get("release_id") != release_id:
        raise SiteBuildError(f"the signed manifest at {tag} is {manifest.get('release_id')}, not {release_id}")
    verify(tag)

    pkg_name = f"Mac-MCP-{version}-{release_id}-arm64.pkg"
    assets = {str(item.get("name")): item for item in release.get("assets") or [] if isinstance(item, dict)}
    pkg = assets.get(pkg_name)
    if pkg is None or f"{pkg_name}.sha256" not in assets:
        raise SiteBuildError(f"the release must carry {pkg_name} and {pkg_name}.sha256")

    fields = sha256_text.split()
    if len(fields) != 2 or fields[1].lstrip("*") != pkg_name:
        raise SiteBuildError("the .sha256 asset does not name the package")
    sha = fields[0].lower()
    if not _SHA_RE.fullmatch(sha):
        raise SiteBuildError("the .sha256 asset does not hold a SHA-256")
    if str(pkg.get("digest") or "") != f"sha256:{sha}":
        raise SiteBuildError("GitHub's digest of the package differs from the published .sha256")

    url = str(pkg.get("browser_download_url") or "")
    expected_url = f"https://github.com/{REPO}/releases/download/{tag}/{pkg_name}"
    if url != expected_url:
        raise SiteBuildError(f"unexpected package URL {url!r}")
    return {
        "version": version,
        "release_id": release_id,
        "tag": tag,
        "pkg_name": pkg_name,
        "pkg_url": url,
        "pkg_sha256": sha,
        "pkg_size_mb": f"{int(pkg.get('size') or 0) / 1_000_000:.0f}",
        "release_url": f"https://github.com/{REPO}/releases/tag/{tag}",
        "published_date": str(release.get("published_at") or "")[:10],
        "manifest_payload_sha256": manifest.get("payload_sha256"),
        "preview": False,
    }


def offline_data(root: Path = ROOT) -> Dict[str, Any]:
    """Local preview from the working tree; marked so it can never be deployed."""
    manifest = json.loads((root / "release" / "stable-manifest.json").read_text())
    version, release_id = manifest["version"], manifest["release_id"]
    revision = release_id.rsplit("-r", 1)[-1]
    tag = f"v{version}-r{revision}"
    pkg_name = f"Mac-MCP-{version}-{release_id}-arm64.pkg"
    sha_file = root / "dist" / f"{pkg_name}.sha256"
    sha = sha_file.read_text().split()[0] if sha_file.exists() else "0" * 64
    pkg = root / "dist" / pkg_name
    return {
        "version": version, "release_id": release_id, "tag": tag, "pkg_name": pkg_name,
        "pkg_url": f"https://github.com/{REPO}/releases/download/{tag}/{pkg_name}", "pkg_sha256": sha,
        "pkg_size_mb": f"{pkg.stat().st_size / 1_000_000:.0f}" if pkg.exists() else "90",
        "release_url": f"https://github.com/{REPO}/releases/tag/{tag}", "published_date": "",
        "manifest_payload_sha256": manifest.get("payload_sha256"), "preview": True,
    }


def render(data: Dict[str, Any], out: Path, site: Path = SITE) -> None:
    """Copy the site with every placeholder filled; no {{ may remain in any page."""
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(site, out, ignore=shutil.ignore_patterns("*.test.mjs", ".DS_Store"))
    values = {key: html.escape(str(data.get(key, "")), quote=True) for key in PLACEHOLDERS}
    values["preview_banner"] = (
        '<p class="preview-banner" role="status">Local preview, not the published site.</p>'
        if data.get("preview") else ""
    )
    for page in out.rglob("*.html"):
        text = page.read_text(encoding="utf-8")
        for key, value in values.items():
            text = text.replace("{{" + key + "}}", value)
        if "{{" in text:
            raise SiteBuildError(f"{page.name} still has an unfilled placeholder")
        page.write_text(text, encoding="utf-8")
    _version_assets(out)
    public = {key: data[key] for key in data}
    (out / "release.json").write_text(json.dumps(public, indent=2) + "\n", encoding="utf-8")


def _version_assets(out: Path) -> None:
    """Tie every local stylesheet, script, image and font URL to its content, so a CDN cache in front of
    any copy of the site (GitHub Pages, tarkan.cloud) never serves an old file with a new page."""
    def digest(path: Path) -> str:
        if not path.is_file():
            raise SiteBuildError(f"missing site file {path.relative_to(out)}")
        return hashlib.sha256(path.read_bytes()).hexdigest()[:12]

    for sheet in out.rglob("*.css"):
        text = sheet.read_text(encoding="utf-8")
        sheet.write_text(_CSS_REF.sub(lambda m: f'url("{m.group(1)}?v={digest((sheet.parent / m.group(1)).resolve())}")', text),
                         encoding="utf-8")
    for page in out.rglob("*.html"):
        text = page.read_text(encoding="utf-8")
        page.write_text(_PAGE_REF.sub(lambda m: f"{m.group(1)}{m.group(2)}?v={digest(page.parent / m.group(2))}{m.group(3)}", text),
                        encoding="utf-8")


def check(out: Path) -> Dict[str, Any]:
    """Re-read a built site: deployable, well formed and consistent with its own page."""
    data = json.loads((out / "release.json").read_text())
    if data.get("preview"):
        raise SiteBuildError("a local preview cannot be deployed")
    if not _VERSION_RE.fullmatch(str(data.get("version"))) or not _SHA_RE.fullmatch(str(data.get("pkg_sha256"))):
        raise SiteBuildError("release.json is malformed")
    page = (out / "index.html").read_text(encoding="utf-8")
    for key in ("pkg_sha256", "pkg_url", "release_id"):
        if html.escape(str(data[key]), quote=True) not in page:
            raise SiteBuildError(f"index.html does not show the release {key}")
    return data


def _github_output(**values: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            for key, value in values.items():
                handle.write(f"{key}={value}\n")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--release-json", type=Path)
    parser.add_argument("--sha256-file", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--offline", action="store_true", help="Local preview from the working tree.")
    parser.add_argument("--check", type=Path, help="Validate an already built site folder.")
    parser.add_argument("--min-release", default=MIN_RELEASE)
    args = parser.parse_args(argv)
    try:
        if args.check:
            data = check(args.check)
            print(f"site ok: {data['release_id']} {data['pkg_sha256'][:12]}")
            return 0
        if not args.out:
            parser.error("--out is required")
        if args.offline:
            data = offline_data()
        else:
            if not args.release_json or not args.sha256_file:
                parser.error("--release-json and --sha256-file are required (or --offline)")
            data = release_data(json.loads(args.release_json.read_text()), args.sha256_file.read_text(),
                                min_release=args.min_release)
        render(data, args.out)
    except ReleaseTooOld as exc:
        print(f"not deploying: {exc}")
        _github_output(deploy="false")
        return 0
    except SiteBuildError as exc:
        print(f"site build failed: {exc}", file=sys.stderr)
        return 1
    _github_output(deploy="false" if data.get("preview") else "true")
    print(f"built {args.out} for {data['release_id']}" + (" (local preview)" if data.get("preview") else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
