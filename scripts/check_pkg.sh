#!/bin/bash
# Check a built Mac MCP package before it ships (#189): unsigned as documented, the expected
# payload, a consistent release.json, a valid app signature, and a real install + upgrade into
# a temporary HOME from the package's own contents (tests/test_pkg_staged_install.py, port 8878).
#
#   scripts/check_pkg.sh dist/Mac-MCP-<version>-<release_id>-arm64.pkg [expected_release_id]
#
# CI runs it on every package change and on each published release's asset; run it locally
# before publishing a checkpoint's package.
set -Eeuo pipefail

PKG="${1:?usage: scripts/check_pkg.sh PKG [expected_release_id]}"
EXPECT_RELEASE="${2:-}"
REPO="$(cd "$(dirname "$0")/.." && pwd -P)"
PYTHON="${PYTHON:-python3}"
fail() { echo "check_pkg: $*" >&2; exit 1; }
[[ -f "$PKG" ]] || fail "no package at $PKG"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/mac-mcp-pkg-check.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

echo "==> Signature"
signature="$(pkgutil --check-signature "$PKG" 2>&1 || true)"
grep -q "no signature" <<<"$signature" || fail "expected an unsigned package (no Developer ID yet); got: $signature"

echo "==> Payload"
payload_files="$(pkgutil --payload-files "$PKG")"
for entry in "./Applications/Mac MCP.app" "./.mac-mcp/pkg/staging"; do
  grep -qxF "$entry" <<<"$payload_files" || fail "payload is missing $entry"
done

echo "==> Contents"
pkgutil --expand-full "$PKG" "$WORK/expanded"
PAYLOAD="$(find "$WORK/expanded" -maxdepth 2 -type d -name Payload | head -1)"
[[ -n "$PAYLOAD" && -d "$PAYLOAD/.mac-mcp/pkg/staging" ]] || fail "could not find the staged payload"
codesign --verify --deep --strict "$PAYLOAD/Applications/Mac MCP.app"
"$PYTHON" - "$PAYLOAD/.mac-mcp/pkg/staging" "$EXPECT_RELEASE" <<'PY'
import hashlib, json, re, sys
from pathlib import Path
staging, expected = Path(sys.argv[1]), sys.argv[2]
release = json.loads((staging / "release.json").read_text())
for key in ("release_id", "version", "commit", "source_sha256", "python_version"):
    if not release.get(key):
        sys.exit(f"release.json has no {key}")
if not re.fullmatch(r"[0-9a-f]{40}", release["commit"]):
    sys.exit(f"release.json commit is not a full commit id: {release['commit']}")
if hashlib.sha256((staging / "source.tar").read_bytes()).hexdigest() != release["source_sha256"]:
    sys.exit("source.tar does not match release.json source_sha256")
if expected and release["release_id"] != expected:
    sys.exit(f"release.json names {release['release_id']}, expected {expected}")
for needed in ("pkg_install.py", "python/bin/python3", "wheelhouse"):
    if not (staging / needed).exists():
        sys.exit(f"staging is missing {needed}")
print(f"    {release['release_id']} ({release['version']}, {release['commit'][:8]})")
PY

echo "==> Install and upgrade into a temporary HOME"
cd "$REPO"
MAC_MCP_PKG_STAGE="$PAYLOAD" "$PYTHON" -m unittest tests.test_pkg_staged_install -v
echo "Package check passed: $(basename "$PKG")"
