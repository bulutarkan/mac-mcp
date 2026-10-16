#!/bin/zsh
# Builds "Mac MCP Server.app", the launcher that holds Mac MCP's macOS permissions
# (docs/superpowers/specs/2026-10-12-server-permission-identity-design.md).
# An ad-hoc signature ties the owner's grants to the exact binary, so the bundle is
# rebuilt only when its inputs (launcher source, Info.plist, icon) change; a new
# toolchain alone never rebuilds it.
set -euo pipefail
SCRIPT_DIR="${0:A:h}"
SRC="${MAC_MCP_HELPER_SRC:-${SCRIPT_DIR}}"
# Its own frozen icon: a new icon would rebuild the bundle and drop the owner's grants.
ICON="${MAC_MCP_HELPER_ICON:-${SCRIPT_DIR}/AppIcon.png}"
OUT="${1:?usage: build_helper.sh OUTPUT_DIR}"
APP="${OUT}/Mac MCP Server.app"
STAMP="${OUT}/.source-sha256"

inputs="$(/bin/cat "${SRC}/launcher.c" "${SRC}/Info.plist" "${ICON}" | /usr/bin/shasum -a 256 | /usr/bin/awk '{print $1}')"
if [[ -f "${STAMP}" && "$(/bin/cat "${STAMP}")" == "${inputs}" ]] \
    && /usr/bin/codesign --verify --strict "${APP}" >/dev/null 2>&1; then
  print "Mac MCP Server is up to date: ${APP}"
  exit 0
fi

WORK="$(/usr/bin/mktemp -d "${TMPDIR:-/tmp}/mac-mcp-helper.XXXXXX")"
trap '/bin/rm -rf "${WORK}"' EXIT
STAGE="${WORK}/Mac MCP Server.app"
/bin/mkdir -p "${STAGE}/Contents/MacOS" "${STAGE}/Contents/Resources"
/bin/cp "${SRC}/Info.plist" "${STAGE}/Contents/Info.plist"
/usr/bin/xcrun clang -O2 -framework ApplicationServices -framework CoreGraphics -framework CoreFoundation \
  -o "${STAGE}/Contents/MacOS/mac-mcp-server" "${SRC}/launcher.c"
ICONSET="${WORK}/AppIcon.iconset"
/bin/mkdir -p "${ICONSET}"
for spec in "16 icon_16x16.png" "32 icon_16x16@2x.png" "32 icon_32x32.png" "64 icon_32x32@2x.png" "128 icon_128x128.png" "256 icon_128x128@2x.png" "256 icon_256x256.png" "512 icon_256x256@2x.png" "512 icon_512x512.png" "1024 icon_512x512@2x.png"; do
  size="${spec%% *}"; name="${spec#* }"
  /usr/bin/sips -z "${size}" "${size}" "${ICON}" --out "${ICONSET}/${name}" >/dev/null
done
/usr/bin/iconutil -c icns "${ICONSET}" -o "${STAGE}/Contents/Resources/AppIcon.icns"
/usr/bin/codesign --force --sign - "${STAGE}" >/dev/null
/usr/bin/codesign --verify --strict "${STAGE}"
/bin/mkdir -p "${OUT}"
/bin/rm -rf "${APP}"
/bin/mv "${STAGE}" "${APP}"
print -r -- "${inputs}" > "${STAMP}"
print "Built Mac MCP Server: ${APP}"
