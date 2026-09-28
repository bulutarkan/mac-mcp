#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
DEST="${1:-${HOME}/Applications/Mac MCP.app}"
TMP="$(mktemp -d /tmp/mac-mcp-menu-build.XXXXXX)"
BUILD_APP="${TMP}/Mac MCP.app"
PREVIOUS_APP="${TMP}/Previous Mac MCP.app"
MANAGE_LIFECYCLE=1
WAS_RUNNING=0

if [[ "${MAC_MCP_MENU_APP_LIFECYCLE_EXTERNAL:-0}" == "1" || "${MAC_MCP_SKIP_MENU_APP_INSTALL:-0}" == "1" ]]; then
  MANAGE_LIFECYCLE=0
fi

cleanup() {
  rm -rf "$TMP"
}
trap cleanup EXIT

canonical_executable() {
  local executable="$DEST/Contents/MacOS/MacMCPMenu"
  if [[ -e "$executable" ]]; then
    local executable_dir
    executable_dir="$(cd "${executable:h}" && /bin/pwd -P)"
    executable="$executable_dir/${executable:t}"
  fi
  printf '%s\n' "$executable"
}

menu_pids() {
  local executable
  executable="$(canonical_executable)"
  /bin/ps -axo pid=,command= | /usr/bin/awk -v target="$executable" '
    {
      pid=$1
      $1=""
      sub(/^[[:space:]]+/, "", $0)
      if ($0 == target) print pid
    }
  '
}

stop_menu() {
  local pids pid attempt=0
  pids="$(menu_pids)"
  [[ -z "$pids" ]] && return 0
  WAS_RUNNING=1
  for pid in ${=pids}; do
    /bin/kill -TERM "$pid" 2>/dev/null || true
  done
  while (( attempt < 20 )); do
    [[ -z "$(menu_pids)" ]] && return 0
    /bin/sleep 0.1
    attempt=$((attempt + 1))
  done
  pids="$(menu_pids)"
  for pid in ${=pids}; do
    /bin/kill -KILL "$pid" 2>/dev/null || true
  done
}

start_menu() {
  local attempt=0
  /usr/bin/open -g -n "$DEST" >/dev/null 2>&1 || return 1
  while (( attempt < 50 )); do
    [[ -n "$(menu_pids)" ]] && return 0
    /bin/sleep 0.2
    attempt=$((attempt + 1))
  done
  return 1
}

restore_previous() {
  rm -rf "$DEST"
  if [[ -d "$PREVIOUS_APP" ]]; then
    mkdir -p "${DEST:h}"
    /usr/bin/ditto "$PREVIOUS_APP" "$DEST"
    if (( WAS_RUNNING == 1 )); then
      /usr/bin/open -g -n "$DEST" >/dev/null 2>&1 || true
    fi
  fi
}

"${SCRIPT_DIR}/build_app.sh" "$TMP" >/dev/null
/usr/bin/codesign --verify --deep --strict "$BUILD_APP"

mkdir -p "${DEST:h}"
if [[ -d "$DEST" ]]; then
  /usr/bin/ditto "$DEST" "$PREVIOUS_APP"
fi

if (( MANAGE_LIFECYCLE == 1 )); then
  stop_menu
fi

rm -rf "$DEST"
/usr/bin/ditto "$BUILD_APP" "$DEST"
/usr/bin/codesign --verify --deep --strict "$DEST"

if (( MANAGE_LIFECYCLE == 1 )); then
  if ! start_menu; then
    restore_previous
    printf 'Mac MCP menu bar app failed to start after installation.\n' >&2
    exit 1
  fi
fi

printf 'Installed: %s\n' "$DEST"
