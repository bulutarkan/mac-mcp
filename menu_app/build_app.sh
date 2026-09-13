#!/bin/zsh
set -euo pipefail
SCRIPT_DIR="${0:A:h}"
BUILD_ROOT="${1:-${SCRIPT_DIR}/build}"
APP="${BUILD_ROOT}/Mac MCP.app"
CONTENTS="${APP}/Contents"
MACOS="${CONTENTS}/MacOS"
RESOURCES="${CONTENTS}/Resources"
PLUGINS="${CONTENTS}/PlugIns"
APP_BUNDLE_ID="${MAC_MCP_APP_BUNDLE_ID:-com.bulutarkan.mac-mcp.menu}"
EXTENSION_BUNDLE_ID="${APP_BUNDLE_ID}.safari"
EXTENSION="${PLUGINS}/Mac MCP Safari Visual Companion.appex"
EXTENSION_CONTENTS="${EXTENSION}/Contents"
EXTENSION_MACOS="${EXTENSION_CONTENTS}/MacOS"
EXTENSION_RESOURCES="${EXTENSION_CONTENTS}/Resources"
ICON_SOURCE="${MAC_MCP_ICON_SOURCE:-${HOME}/Downloads/mac-mcp-icon.png}"
ARCH="$(uname -m)"
SIGN_IDENTITY="${MAC_MCP_CODESIGN_IDENTITY:--}"
CODESIGN_EXTRA=()
if [[ "${SIGN_IDENTITY}" != "-" ]]; then
  CODESIGN_EXTRA+=(--options runtime --timestamp)
fi
rm -rf "${APP}"
mkdir -p "${MACOS}" "${RESOURCES}" "${EXTENSION_MACOS}" "${EXTENSION_RESOURCES}"
cp "${SCRIPT_DIR}/Info.plist" "${CONTENTS}/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier ${APP_BUNDLE_ID}" "${CONTENTS}/Info.plist" >/dev/null
if [[ -n "${MAC_MCP_APP_DISPLAY_NAME:-}" ]]; then
  /usr/libexec/PlistBuddy -c "Set :CFBundleName ${MAC_MCP_APP_DISPLAY_NAME}" "${CONTENTS}/Info.plist" >/dev/null
  /usr/libexec/PlistBuddy -c "Set :CFBundleDisplayName ${MAC_MCP_APP_DISPLAY_NAME}" "${CONTENTS}/Info.plist" >/dev/null
fi
xcrun swiftc -O -parse-as-library -target "${ARCH}-apple-macos13.0" \
  -framework SwiftUI -framework AppKit -framework Foundation -framework Security -framework CoreAudio -framework SafariServices \
  "${SCRIPT_DIR}/Sources/MacMCPMenuApp.swift" \
  "${SCRIPT_DIR}/Sources/AppState.swift" \
  "${SCRIPT_DIR}/Sources/SettingsStore.swift" \
  "${SCRIPT_DIR}/Sources/KeychainStore.swift" \
  "${SCRIPT_DIR}/Sources/AudioDeviceStore.swift" \
  "${SCRIPT_DIR}/Sources/MenuBarView.swift" \
  -o "${MACOS}/MacMCPMenu"

# Safari Web Extension: built with the same command-line Swift toolchain so installing
# Mac MCP from GitHub does not require generating or shipping an Xcode project.
xcrun swiftc -O -parse-as-library -application-extension -target "${ARCH}-apple-macos13.0" \
  -module-name MacMCPSafariExtension -framework SafariServices -framework Foundation \
  -Xlinker -e -Xlinker _NSExtensionMain \
  "${SCRIPT_DIR}/SafariExtension/SafariWebExtensionHandler.swift" \
  -o "${EXTENSION_MACOS}/MacMCPSafariExtension"
sed "s/__MAC_MCP_EXTENSION_BUNDLE_ID__/${EXTENSION_BUNDLE_ID}/g" \
  "${SCRIPT_DIR}/SafariExtension/Info.plist" > "${EXTENSION_CONTENTS}/Info.plist"
cp "${SCRIPT_DIR}/SafariExtension/manifest.json" "${EXTENSION_RESOURCES}/manifest.json"
cp "${SCRIPT_DIR}/SafariExtension/visual.js" "${EXTENSION_RESOURCES}/visual.js"
/usr/bin/plutil -lint "${EXTENSION_CONTENTS}/Info.plist" >/dev/null
/usr/bin/codesign --force "${CODESIGN_EXTRA[@]}" --sign "${SIGN_IDENTITY}" "${EXTENSION}" >/dev/null

if [[ -f "${ICON_SOURCE}" ]]; then
  ICONSET="${BUILD_ROOT}/AppIcon.iconset"; rm -rf "${ICONSET}"; mkdir -p "${ICONSET}"
  for spec in "16 icon_16x16.png" "32 icon_16x16@2x.png" "32 icon_32x32.png" "64 icon_32x32@2x.png" "128 icon_128x128.png" "256 icon_128x128@2x.png" "256 icon_256x256.png" "512 icon_256x256@2x.png" "512 icon_512x512.png" "1024 icon_512x512@2x.png"; do
    size="${spec%% *}"; name="${spec#* }"; /usr/bin/sips -z "${size}" "${size}" "${ICON_SOURCE}" --out "${ICONSET}/${name}" >/dev/null
  done
  /usr/bin/iconutil -c icns "${ICONSET}" -o "${RESOURCES}/AppIcon.icns"; rm -rf "${ICONSET}"
fi
/usr/bin/codesign --force "${CODESIGN_EXTRA[@]}" --sign "${SIGN_IDENTITY}" "${APP}" >/dev/null
/usr/bin/plutil -lint "${CONTENTS}/Info.plist" >/dev/null
printf '%s\n' "${APP}"
