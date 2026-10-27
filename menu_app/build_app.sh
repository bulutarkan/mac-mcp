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
# The app icon is part of the repository, so every build has the same artwork (#102).
ICON_SOURCE="${MAC_MCP_ICON_SOURCE:-${SCRIPT_DIR}/Assets/AppIcon.png}"
ARCH="$(uname -m)"
SIGN_IDENTITY="${MAC_MCP_CODESIGN_IDENTITY:--}"
# This Mac's own signing identity (a SHA-1 or name in ~/.mac-mcp/codesign-identity), when nothing else
# is set: Shortcuts and Siri talk only to apps signed with a team, so ad-hoc builds lose the App Intents
# action (#195). Ignored when the identity is not in the keychain.
IDENTITY_FILE="${MAC_MCP_CODESIGN_IDENTITY_FILE:-${HOME}/.mac-mcp/codesign-identity}"
if [[ -z "${MAC_MCP_CODESIGN_IDENTITY:-}" && -r "${IDENTITY_FILE}" ]]; then
  LOCAL_IDENTITY="$(/usr/bin/head -n 1 "${IDENTITY_FILE}" | /usr/bin/tr -d '[:space:]')"
  if [[ -n "${LOCAL_IDENTITY}" ]] && /usr/bin/security find-identity -v -p codesigning | /usr/bin/grep -qF "${LOCAL_IDENTITY}"; then
    SIGN_IDENTITY="${LOCAL_IDENTITY}"
  fi
fi
CODESIGN_EXTRA=()
# Hardened runtime and a timestamp are what notarization needs (Developer ID); a development
# identity signs plainly, like Xcode's local builds.
if [[ "${SIGN_IDENTITY}" != "-" ]] && /usr/bin/security find-identity -v -p codesigning | /usr/bin/grep -F "${SIGN_IDENTITY}" | /usr/bin/grep -q "Developer ID"; then
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
APP_SOURCES=(
  "${SCRIPT_DIR}/Sources/SwiftUICompat.swift"
  "${SCRIPT_DIR}/Sources/VisualTokens.swift"
  "${SCRIPT_DIR}/Sources/AppMotion.swift"
  "${SCRIPT_DIR}/Sources/MacMCPMenuApp.swift"
  "${SCRIPT_DIR}/Sources/LoginItem.swift"
  "${SCRIPT_DIR}/Sources/PrivacyPane.swift"
  "${SCRIPT_DIR}/Sources/McpServersPane.swift"
  "${SCRIPT_DIR}/Sources/MacsPane.swift"
  "${SCRIPT_DIR}/Sources/IPhoneSection.swift"
  "${SCRIPT_DIR}/Sources/McpConnectorsSection.swift"
  "${SCRIPT_DIR}/Sources/MessagesAccessSection.swift"
  "${SCRIPT_DIR}/Sources/ScheduledTasksPane.swift"
  "${SCRIPT_DIR}/Sources/ScheduledTaskWatchdog.swift"
  "${SCRIPT_DIR}/Sources/AutomationPane.swift"
  "${SCRIPT_DIR}/Sources/MemoryReviewView.swift"
  "${SCRIPT_DIR}/Sources/SkillsSection.swift"
  "${SCRIPT_DIR}/Sources/RunReportView.swift"
  "${SCRIPT_DIR}/Sources/LiveIsland.swift"
  "${SCRIPT_DIR}/Sources/IslandHitArea.swift"
  "${SCRIPT_DIR}/Sources/OwnerPromptPlacement.swift"
  "${SCRIPT_DIR}/Sources/OwnerPrompt.swift"
  "${SCRIPT_DIR}/Sources/ConnectAppsBox.swift"
  "${SCRIPT_DIR}/Sources/AgentNotificationController.swift"
  "${SCRIPT_DIR}/Sources/AppState.swift"
  "${SCRIPT_DIR}/Sources/AppModels+Dashboard.swift"
  "${SCRIPT_DIR}/Sources/AppModels+Agents.swift"
  "${SCRIPT_DIR}/Sources/AppModels+Steering.swift"
  "${SCRIPT_DIR}/Sources/AppModels+Updates.swift"
  "${SCRIPT_DIR}/Sources/AppModels+ScheduledTasks.swift"
  "${SCRIPT_DIR}/Sources/AppModels+Live.swift"
  "${SCRIPT_DIR}/Sources/StatusDisplay.swift"
  "${SCRIPT_DIR}/Sources/MenuLiveStatus.swift"
  "${SCRIPT_DIR}/Sources/OnboardingChecklist.swift"
  "${SCRIPT_DIR}/Sources/OnboardingBridge.swift"
  "${SCRIPT_DIR}/Sources/OnboardingWindowController.swift"
  "${SCRIPT_DIR}/Sources/SettingsStore.swift"
  "${SCRIPT_DIR}/Sources/SettingsView.swift"
  "${SCRIPT_DIR}/Sources/UsageComponents.swift"
  "${SCRIPT_DIR}/Sources/ProviderUsageSection.swift"
  "${SCRIPT_DIR}/Sources/TeamUsage.swift"
  "${SCRIPT_DIR}/Sources/TeamUsageSection.swift"
  "${SCRIPT_DIR}/Sources/TeamHistorySection.swift"
  "${SCRIPT_DIR}/Sources/AgentChoice.swift"
  "${SCRIPT_DIR}/Sources/TeamPresets.swift"
  "${SCRIPT_DIR}/Sources/TeamPresetsSection.swift"
  "${SCRIPT_DIR}/Sources/SettingsTabs.swift"
  "${SCRIPT_DIR}/Sources/UsageOverview.swift"
  "${SCRIPT_DIR}/Sources/SavedLoginsView.swift"
  "${SCRIPT_DIR}/Sources/SettingsSearch.swift"
  "${SCRIPT_DIR}/Sources/SettingsSearchLabels.swift"
  "${SCRIPT_DIR}/Sources/SettingsRules.swift"
  "${SCRIPT_DIR}/Sources/RecipeLauncher.swift"
  "${SCRIPT_DIR}/Sources/RecipeIntents.swift"
  "${SCRIPT_DIR}/Sources/DoctorReport.swift"
  "${SCRIPT_DIR}/Sources/HelpDiagnostics.swift"
  "${SCRIPT_DIR}/Sources/SettingsWindowController.swift"
  "${SCRIPT_DIR}/Sources/ChatReliability.swift"
  "${SCRIPT_DIR}/Sources/ChatLibrary.swift"
  "${SCRIPT_DIR}/Sources/ChatWindow.swift"
  "${SCRIPT_DIR}/Sources/ChatShell.swift"
  "${SCRIPT_DIR}/Sources/ChatTranscript.swift"
  "${SCRIPT_DIR}/Sources/ChatMarkdown.swift"
  "${SCRIPT_DIR}/Sources/ChatUsageSection.swift"
  "${SCRIPT_DIR}/Sources/ChatSyntax.swift"
  "${SCRIPT_DIR}/Sources/ToolActivityBubbleController.swift"
  "${SCRIPT_DIR}/Sources/DesktopCursorOverlay.swift"
  "${SCRIPT_DIR}/Sources/BrowserTabPulse.swift"
  "${SCRIPT_DIR}/Sources/KeychainStore.swift"
  "${SCRIPT_DIR}/Sources/AudioDeviceStore.swift"
  "${SCRIPT_DIR}/Sources/SessionDetailPresentation.swift"
  "${SCRIPT_DIR}/Sources/SessionDetailView.swift"
  "${SCRIPT_DIR}/Sources/MenuHoverTip.swift"
  "${SCRIPT_DIR}/Sources/MenuBarView.swift"
)
# Shortcuts and Siri find the app's actions (#195) through App Intents metadata, built from the
# compiler's constant values; the whole module is compiled at once so there is one such file.
INTENTS_WORK="$(mktemp -d)"
trap 'rm -rf "${INTENTS_WORK}"' EXIT
print -r -- '["AppIntent","EntityQuery","AppEntity","TransientEntity","AppEnum","AppShortcutProviding","AppShortcutsProvider","AnyResolverProviding","AppIntentsPackage","DynamicOptionsProvider"]' > "${INTENTS_WORK}/protocols.json"
xcrun swiftc -O -wmo -parse-as-library -module-name MacMCPMenu -target "${ARCH}-apple-macos13.0" \
  -framework SwiftUI -framework AppKit -framework Foundation -framework Security -framework CoreAudio -framework SafariServices -framework CoreImage -framework UserNotifications -framework WebKit -framework ServiceManagement -framework AppIntents \
  -emit-const-values-path "${INTENTS_WORK}/MacMCPMenu.swiftconstvalues" \
  -Xfrontend -const-gather-protocols-file -Xfrontend "${INTENTS_WORK}/protocols.json" \
  "${APP_SOURCES[@]}" \
  -o "${MACOS}/MacMCPMenu"
# Xcode's metadata processor; without Xcode (Command Line Tools only) the app builds the same, just
# without the Shortcuts action.
if INTENTS_PROCESSOR="$(xcrun --find appintentsmetadataprocessor 2>/dev/null)"; then
  print -rl -- "${APP_SOURCES[@]}" > "${INTENTS_WORK}/sources.txt"
  print -r -- "${INTENTS_WORK}/MacMCPMenu.swiftconstvalues" > "${INTENTS_WORK}/constvalues.txt"
  "${INTENTS_PROCESSOR}" --output "${RESOURCES}" --toolchain-dir "$(xcode-select -p)/Toolchains/XcodeDefault.xctoolchain" \
    --module-name MacMCPMenu --sdk-root "$(xcrun --sdk macosx --show-sdk-path)" \
    --xcode-version "$(xcodebuild -version 2>/dev/null | awk '/Build version/ {print $3}')" \
    --platform-family macOS --deployment-target 13.0 --target-triple "${ARCH}-apple-macos13.0" \
    --source-file-list "${INTENTS_WORK}/sources.txt" --swift-const-vals-list "${INTENTS_WORK}/constvalues.txt" \
    --force --no-app-shortcuts-localization >/dev/null 2>&1 \
    || printf 'App Intents metadata was not built; the Shortcuts action will be missing.\n' >&2
fi

# The first-run tour (a local web page; tests stay out of the bundle) and the icon it shows.
/usr/bin/rsync -a --exclude tests "${SCRIPT_DIR}/Onboarding" "${RESOURCES}/"
/bin/mkdir -p "${RESOURCES}/Assets"
/bin/cp "${SCRIPT_DIR}/Assets/AppIcon.png" "${RESOURCES}/Assets/AppIcon.png"
# The monogram: menu header and Live Island (champagne, bronze), menu bar template (@1x, @2x).
for art in BrandMark.png BrandMark.svg BrandMarkBronze.svg BrandMarkLight.png MenuBarMark.png MenuBarMark@2x.png; do
  /bin/cp "${SCRIPT_DIR}/Assets/${art}" "${RESOURCES}/Assets/${art}"
done
# The logos of the services in Settings › MCP Servers › Connect a Service (#260).
/bin/mkdir -p "${RESOURCES}/Assets/Connectors"
/bin/cp "${SCRIPT_DIR}/Assets/Connectors/"*.png "${RESOURCES}/Assets/Connectors/"

# Safari Web Extension: built with the same command-line Swift toolchain so installing
# Mac MCP from GitHub does not require generating or shipping an Xcode project.
xcrun swiftc -O -parse-as-library -application-extension -target "${ARCH}-apple-macos13.0" \
  -module-name MacMCPSafariExtension -framework SafariServices -framework Foundation \
  -Xlinker -e -Xlinker _NSExtensionMain \
  "${SCRIPT_DIR}/SafariExtension/SafariWebExtensionHandler.swift" \
  -o "${EXTENSION_MACOS}/MacMCPSafariExtension"
sed "s/__MAC_MCP_EXTENSION_BUNDLE_ID__/${EXTENSION_BUNDLE_ID}/g" \
  "${SCRIPT_DIR}/SafariExtension/Info.plist" > "${EXTENSION_CONTENTS}/Info.plist"
cp "${SCRIPT_DIR}/BrowserVisualCompanion/manifest.json" "${EXTENSION_RESOURCES}/manifest.json"
cp "${SCRIPT_DIR}/BrowserVisualCompanion/visual.js" "${EXTENSION_RESOURCES}/visual.js"
/usr/bin/plutil -lint "${EXTENSION_CONTENTS}/Info.plist" >/dev/null
# PlugInKit ignores an extension signed by a real identity unless it is sandboxed ("plug-ins must be
# sandboxed"), and Safari then never loads the Visual Companion. Its handler only answers Safari, so the
# sandbox takes nothing away.
/usr/bin/codesign --force "${CODESIGN_EXTRA[@]}" --entitlements "${SCRIPT_DIR}/SafariExtension/SafariExtension.entitlements" \
  --sign "${SIGN_IDENTITY}" "${EXTENSION}" >/dev/null

# Mac MCP Messages Reader (#261): the one app the owner gives Full Disk Access to so agents can read
# Messages; it opens only ~/Library/Messages/chat.db, read-only. Signed like the app, so the grant
# survives rebuilds.
READER="${CONTENTS}/Helpers/Mac MCP Messages Reader.app"
mkdir -p "${READER}/Contents/MacOS"
xcrun swiftc -O -target "${ARCH}-apple-macos13.0" -module-name MacMCPMessagesReader -lsqlite3 \
  "${SCRIPT_DIR}/MessagesReader/main.swift" -o "${READER}/Contents/MacOS/MacMCPMessagesReader"
/usr/bin/plutil -create xml1 "${READER}/Contents/Info.plist"
for pair in "CFBundleExecutable string MacMCPMessagesReader" "CFBundleIdentifier string ${APP_BUNDLE_ID}.messages-reader" \
            "CFBundleName string Mac MCP Messages Reader" "CFBundlePackageType string APPL" \
            "CFBundleShortVersionString string 1.0" "CFBundleVersion string 1" "LSUIElement bool YES" \
            "LSMinimumSystemVersion string 13.0"; do
  read -r key kind value <<<"${pair}"
  /usr/bin/plutil -insert "${key}" -"${kind}" "${value}" "${READER}/Contents/Info.plist"
done
/usr/bin/plutil -lint "${READER}/Contents/Info.plist" >/dev/null
/usr/bin/codesign --force "${CODESIGN_EXTRA[@]}" --sign "${SIGN_IDENTITY}" "${READER}" >/dev/null

# Mac MCP Photos (#45): the one app the owner lets see the Photos library; it lists albums, finds items by
# their metadata and copies chosen originals, through PhotoKit, and never changes the library.
PHOTOS_HELPER="${CONTENTS}/Helpers/Mac MCP Photos.app"
mkdir -p "${PHOTOS_HELPER}/Contents/MacOS"
xcrun swiftc -O -target "${ARCH}-apple-macos13.0" -module-name MacMCPPhotos -framework Photos \
  "${SCRIPT_DIR}/PhotosReader/main.swift" -o "${PHOTOS_HELPER}/Contents/MacOS/MacMCPPhotos"
/usr/bin/plutil -create xml1 "${PHOTOS_HELPER}/Contents/Info.plist"
for pair in "CFBundleExecutable string MacMCPPhotos" "CFBundleIdentifier string ${APP_BUNDLE_ID}.photos" \
            "CFBundleName string Mac MCP Photos" "CFBundlePackageType string APPL" \
            "CFBundleShortVersionString string 1.0" "CFBundleVersion string 1" "LSUIElement bool YES" \
            "LSMinimumSystemVersion string 13.0"; do
  read -r key kind value <<<"${pair}"
  /usr/bin/plutil -insert "${key}" -"${kind}" "${value}" "${PHOTOS_HELPER}/Contents/Info.plist"
done
/usr/bin/plutil -insert NSPhotoLibraryUsageDescription -string \
  "Mac MCP finds photos by date, album and type and copies the ones you ask for. It never changes your library." \
  "${PHOTOS_HELPER}/Contents/Info.plist"
/usr/bin/plutil -lint "${PHOTOS_HELPER}/Contents/Info.plist" >/dev/null
/usr/bin/codesign --force "${CODESIGN_EXTRA[@]}" --sign "${SIGN_IDENTITY}" "${PHOTOS_HELPER}" >/dev/null

if [[ ! -f "${ICON_SOURCE}" ]]; then
  printf 'App icon source is missing: %s\n' "${ICON_SOURCE}" >&2
  exit 1
fi
ICONSET="${BUILD_ROOT}/AppIcon.iconset"; rm -rf "${ICONSET}"; mkdir -p "${ICONSET}"
for spec in "16 icon_16x16.png" "32 icon_16x16@2x.png" "32 icon_32x32.png" "64 icon_32x32@2x.png" "128 icon_128x128.png" "256 icon_128x128@2x.png" "256 icon_256x256.png" "512 icon_256x256@2x.png" "512 icon_512x512.png" "1024 icon_512x512@2x.png"; do
  size="${spec%% *}"; name="${spec#* }"; if [[ -f "${SCRIPT_DIR}/Assets/AppIconSmall/${size}.png" ]]; then /bin/cp "${SCRIPT_DIR}/Assets/AppIconSmall/${size}.png" "${ICONSET}/${name}"; else /usr/bin/sips -z "${size}" "${size}" "${ICON_SOURCE}" --out "${ICONSET}/${name}" >/dev/null; fi
done
/usr/bin/iconutil -c icns "${ICONSET}" -o "${RESOURCES}/AppIcon.icns"; rm -rf "${ICONSET}"
[[ -f "${RESOURCES}/AppIcon.icns" ]] || { printf 'App icon was not built into the bundle.\n' >&2; exit 1; }
/usr/bin/codesign --force "${CODESIGN_EXTRA[@]}" --sign "${SIGN_IDENTITY}" "${APP}" >/dev/null
/usr/bin/plutil -lint "${CONTENTS}/Info.plist" >/dev/null
printf '%s\n' "${APP}"
