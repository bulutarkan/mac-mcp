#!/bin/zsh
set -euo pipefail
SCRIPT_DIR="${0:A:h}"
BUILD_ROOT="${1:-${SCRIPT_DIR}/build}"
APP="${BUILD_ROOT}/Mac MCP.app"
CONTENTS="${APP}/Contents"
MACOS="${CONTENTS}/MacOS"
RESOURCES="${CONTENTS}/Resources"
ICON_SOURCE="${MAC_MCP_ICON_SOURCE:-${HOME}/Downloads/mac-mcp-icon.png}"
ARCH="$(uname -m)"
rm -rf "${APP}"
mkdir -p "${MACOS}" "${RESOURCES}"
cp "${SCRIPT_DIR}/Info.plist" "${CONTENTS}/Info.plist"
xcrun swiftc -O -parse-as-library -target "${ARCH}-apple-macos13.0" \
  -framework SwiftUI -framework AppKit -framework Foundation -framework Security -framework CoreAudio \
  "${SCRIPT_DIR}/Sources/MacMCPMenuApp.swift" \
  "${SCRIPT_DIR}/Sources/AppState.swift" \
  "${SCRIPT_DIR}/Sources/SettingsStore.swift" \
  "${SCRIPT_DIR}/Sources/KeychainStore.swift" \
  "${SCRIPT_DIR}/Sources/AudioDeviceStore.swift" \
  "${SCRIPT_DIR}/Sources/MenuBarView.swift" \
  -o "${MACOS}/MacMCPMenu"
if [[ -f "${ICON_SOURCE}" ]]; then
  ICONSET="${BUILD_ROOT}/AppIcon.iconset"; rm -rf "${ICONSET}"; mkdir -p "${ICONSET}"
  for spec in "16 icon_16x16.png" "32 icon_16x16@2x.png" "32 icon_32x32.png" "64 icon_32x32@2x.png" "128 icon_128x128.png" "256 icon_128x128@2x.png" "256 icon_256x256.png" "512 icon_256x256@2x.png" "512 icon_512x512.png" "1024 icon_512x512@2x.png"; do
    size="${spec%% *}"; name="${spec#* }"; /usr/bin/sips -z "${size}" "${size}" "${ICON_SOURCE}" --out "${ICONSET}/${name}" >/dev/null
  done
  /usr/bin/iconutil -c icns "${ICONSET}" -o "${RESOURCES}/AppIcon.icns"; rm -rf "${ICONSET}"
fi
/usr/bin/codesign --force --deep --sign - "${APP}" >/dev/null
/usr/bin/plutil -lint "${CONTENTS}/Info.plist" >/dev/null
printf '%s\n' "${APP}"
