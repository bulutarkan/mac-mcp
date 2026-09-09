#!/bin/zsh
set -euo pipefail
SCRIPT_DIR="${0:A:h}"
DEST="${1:-${HOME}/Applications/Mac MCP.app}"
TMP="$(mktemp -d /tmp/mac-mcp-menu-build.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT
"${SCRIPT_DIR}/build_app.sh" "$TMP" >/dev/null
mkdir -p "${DEST:h}"
rm -rf "$DEST"
cp -R "$TMP/Mac MCP.app" "$DEST"
/usr/bin/codesign --verify --deep --strict "$DEST"
printf 'Installed: %s\n' "$DEST"
