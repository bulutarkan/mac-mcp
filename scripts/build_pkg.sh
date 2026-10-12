#!/bin/bash
# Build the unsigned Apple Silicon Mac MCP package from one commit (default HEAD).
# Normal use: on a signed stable checkpoint. --allow-unverified is for local tests only.
set -Eeuo pipefail

PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20260924/cpython-3.14.7%2B20260924-aarch64-apple-darwin-install_only_stripped.tar.gz"
PY_SHA256="f0fb6b233971672dd7add4efa90f8c209a63253e457ffabd0148b9f734638319"
PY_VERSION="3.14.7"
PKG_ID="com.bulutarkan.mac-mcp.pkg"

REPO="$(cd "$(dirname "$0")/.." && pwd -P)"
COMMIT="HEAD"; ALLOW_UNVERIFIED=0; STAGE_ONLY=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --commit) COMMIT="$2"; shift 2 ;;
    --allow-unverified) ALLOW_UNVERIFIED=1; shift ;;
    --stage-only) STAGE_ONLY="$2"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
[[ "$(uname -m)" == "arm64" ]] || { echo "build on an Apple Silicon Mac" >&2; exit 1; }
COMMIT="$(git -C "$REPO" rev-parse --verify "$COMMIT^{commit}")"
SHORT="${COMMIT:0:8}"
VERSION="$(git -C "$REPO" show "$COMMIT:pyproject.toml" | /usr/bin/awk -F'"' '/^version *=/{print $2; exit}')"

if [[ "$ALLOW_UNVERIFIED" -eq 1 ]]; then
  RELEASE_ID="unverified-$SHORT"; PAYLOAD_SHA="unverified"
else
  python3 "$REPO/scripts/verify_release_manifest.py" --commit "$COMMIT" --branch main >/dev/null \
    || { echo "$SHORT is not a verified stable checkpoint (use --allow-unverified for local tests)" >&2; exit 1; }
  MANIFEST="$(git -C "$REPO" show "$COMMIT:release/stable-manifest.json")"
  RELEASE_ID="$(printf '%s' "$MANIFEST" | python3 -c 'import json,sys; print(json.load(sys.stdin)["release_id"])')"
  PAYLOAD_SHA="$(printf '%s' "$MANIFEST" | python3 -c 'import json,sys; print(json.load(sys.stdin)["payload_sha256"])')"
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/mac-mcp-pkg.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
ROOT="$WORK/root"; STAGING="$ROOT/.mac-mcp/pkg/staging"
mkdir -p "$WORK/src" "$STAGING" "$ROOT/Applications"

echo "==> Source $SHORT ($RELEASE_ID)"
git -C "$REPO" archive --format=tar "$COMMIT" > "$STAGING/source.tar"
tar -xf "$STAGING/source.tar" -C "$WORK/src"
[[ -f "$WORK/src/packaging/pkg/pkg_install.py" ]] || { echo "$SHORT has no packaging/pkg; build a newer checkpoint" >&2; exit 1; }
cp "$WORK/src/packaging/pkg/pkg_install.py" "$STAGING/pkg_install.py"

echo "==> App"
"$WORK/src/menu_app/build_app.sh" "$WORK/app" >/dev/null
codesign --verify --deep --strict "$WORK/app/Mac MCP.app"
ditto "$WORK/app/Mac MCP.app" "$ROOT/Applications/Mac MCP.app"

echo "==> Python $PY_VERSION"
CACHE="$REPO/dist/cache"; mkdir -p "$CACHE"
PY_TGZ="$CACHE/cpython-$PY_VERSION-20260924-aarch64-apple-darwin-install_only_stripped.tar.gz"
if [[ ! -f "$PY_TGZ" ]]; then
  curl -fsSL "$PY_URL" -o "$PY_TGZ.part"
  mv -f "$PY_TGZ.part" "$PY_TGZ"
fi
echo "$PY_SHA256  $PY_TGZ" | shasum -a 256 -c - >/dev/null || { echo "Python archive checksum mismatch" >&2; exit 1; }
tar -xzf "$PY_TGZ" -C "$STAGING"   # creates $STAGING/python
"$STAGING/python/bin/python3" -c 'import sys; assert sys.version.startswith("'"$PY_VERSION"'")'

echo "==> Wheels"
plats=(); for v in 11_0 12_0 13_0 14_0; do plats+=(--platform "macosx_${v}_arm64" --platform "macosx_${v}_universal2"); done
"$STAGING/python/bin/python3" -m pip download --quiet --disable-pip-version-check --only-binary=:all: \
  "${plats[@]}" --platform any --python-version 3.14 --implementation cp --abi cp314 --abi abi3 --abi none \
  --require-hashes -r "$WORK/src/requirements.lock" -d "$STAGING/wheelhouse"
find "$STAGING/python" -name __pycache__ -type d -prune -exec rm -rf {} +

python3 - "$STAGING" "$RELEASE_ID" "$VERSION" "$COMMIT" "$PAYLOAD_SHA" "$PY_VERSION" <<'PY'
import hashlib, json, sys, time
from pathlib import Path
staging, release_id, version, commit, payload, py = sys.argv[1:]
src = Path(staging) / "source.tar"
(Path(staging) / "release.json").write_text(json.dumps({
    "release_id": release_id, "version": version, "commit": commit, "payload_sha256": payload,
    "source_sha256": hashlib.sha256(src.read_bytes()).hexdigest(), "python_version": py, "arch": "arm64",
    "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
}, indent=2, sort_keys=True) + "\n")
PY

if [[ -n "$STAGE_ONLY" ]]; then
  mkdir -p "$STAGE_ONLY"
  ditto "$ROOT" "$STAGE_ONLY"
  echo "Staged home tree: $STAGE_ONLY"
  exit 0
fi

echo "==> Package"
pkgbuild --analyze --root "$ROOT" "$WORK/component.plist" >/dev/null
# Never let Installer "relocate" the payload onto another copy of the app found elsewhere.
python3 - "$WORK/component.plist" <<'PLIST'
import plistlib, sys
path = sys.argv[1]
with open(path, "rb") as handle:
    bundles = plistlib.load(handle)
for bundle in bundles:
    bundle["BundleIsRelocatable"] = False
with open(path, "wb") as handle:
    plistlib.dump(bundles, handle)
PLIST
chmod 755 "$WORK/src/packaging/pkg/scripts/preinstall" "$WORK/src/packaging/pkg/scripts/postinstall"
pkgbuild --quiet --root "$ROOT" --component-plist "$WORK/component.plist" \
  --scripts "$WORK/src/packaging/pkg/scripts" --identifier "$PKG_ID" --version "$VERSION" \
  --install-location / "$WORK/mac-mcp-component.pkg"
sed -e "s/@VERSION@/$VERSION/" -e "s/@COMPONENT_PKG@/mac-mcp-component.pkg/" \
  "$WORK/src/packaging/pkg/distribution.xml" > "$WORK/distribution.xml"
mkdir -p "$REPO/dist"
OUT="$REPO/dist/Mac-MCP-$VERSION-$RELEASE_ID-arm64.pkg"
productbuild --quiet --distribution "$WORK/distribution.xml" --package-path "$WORK" "$OUT"
(cd "$REPO/dist" && shasum -a 256 "$(basename "$OUT")" > "$(basename "$OUT").sha256")
du -h "$OUT"
echo "$OUT"
