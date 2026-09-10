#!/bin/bash
set -Eeuo pipefail
IFS=$'\n\t'

# Mac MCP installer
# Designed to be safe when streamed with: curl -fsSL <url> | bash
# Keep this script compatible with the Bash 3.2 shipped by macOS.

REPO_URL="${MAC_MCP_REPO_URL:-https://github.com/bulutarkan/mac-mcp.git}"
BRANCH="${MAC_MCP_BRANCH:-main}"
SOURCE_DIR="${MAC_MCP_SOURCE_DIR:-$HOME/Projects/mac-mcp}"
RUNTIME_DIR="${MAC_MCP_RUNTIME_DIR:-$HOME/mac-mcp}"
BIN_DIR="${MAC_MCP_BIN_DIR:-$HOME/.local/bin}"
CLI_PATH="$BIN_DIR/mac-mcp"
APP_PATH="${MAC_MCP_APP_PATH:-$HOME/Applications/Mac MCP.app}"
STATE_DIR="${MAC_MCP_STATE_DIR:-$HOME/.mac-mcp}"

BREW_BIN=""
GIT_BIN=""
PYTHON_BIN=""
MACOS_MAJOR=""
MAC_ARCH=""
INSTALL_TMP=""
TTY_AVAILABLE=0
CREATED_SOURCE=0
CREATED_RUNTIME=0
CREATED_CLI=0
CREATED_APP=0
BACKED_UP_APP=0
APP_BACKUP_PATH=""

if { exec 3<>/dev/tty; } 2>/dev/null; then
  TTY_AVAILABLE=1
fi

if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
  C_RESET=$'\033[0m'
  C_BOLD=$'\033[1m'
  C_DIM=$'\033[2m'
  C_BLUE=$'\033[34m'
  C_GREEN=$'\033[32m'
  C_YELLOW=$'\033[33m'
  C_RED=$'\033[31m'
else
  C_RESET=""
  C_BOLD=""
  C_DIM=""
  C_BLUE=""
  C_GREEN=""
  C_YELLOW=""
  C_RED=""
fi

print_header() {
  printf '\n%s%sMac MCP Installer%s\n' "$C_BOLD" "$C_BLUE" "$C_RESET"
  printf '%sSecure local macOS control for MCP clients%s\n\n' "$C_DIM" "$C_RESET"
}

section() {
  printf '\n%s%s==>%s %s%s%s\n' "$C_BOLD" "$C_BLUE" "$C_RESET" "$C_BOLD" "$1" "$C_RESET"
}

ok() {
  printf '%sOK%s    %s\n' "$C_GREEN" "$C_RESET" "$1"
}

info() {
  printf '%sINFO%s  %s\n' "$C_BLUE" "$C_RESET" "$1"
}

warn() {
  printf '%sWARN%s  %s\n' "$C_YELLOW" "$C_RESET" "$1"
}

fail() {
  printf '%sERROR%s %s\n' "$C_RED" "$C_RESET" "$1" >&2
  exit 1
}

cleanup() {
  local status=$?
  if [[ -n "${INSTALL_TMP:-}" && -d "$INSTALL_TMP" ]]; then
    /bin/rm -rf "$INSTALL_TMP" || true
  fi
  if [[ "$status" -ne 0 ]]; then
    if [[ "$CREATED_CLI" -eq 1 && -L "$CLI_PATH" ]]; then
      /bin/rm -f "$CLI_PATH" || true
    fi
    if [[ "$CREATED_RUNTIME" -eq 1 && -d "$RUNTIME_DIR" ]]; then
      /bin/rm -rf "$RUNTIME_DIR" || true
    fi
    if [[ "$CREATED_APP" -eq 1 && -d "$APP_PATH" ]]; then
      /bin/rm -rf "$APP_PATH" || true
    elif [[ "$BACKED_UP_APP" -eq 1 && -n "$APP_BACKUP_PATH" && -d "$APP_BACKUP_PATH" ]]; then
      /bin/rm -rf "$APP_PATH" || true
      /bin/mkdir -p "$(/usr/bin/dirname "$APP_PATH")" || true
      /bin/cp -R "$APP_BACKUP_PATH" "$APP_PATH" || true
    fi
    if [[ "$CREATED_SOURCE" -eq 1 && -d "$SOURCE_DIR" ]]; then
      /bin/rm -rf "$SOURCE_DIR" || true
    fi
  fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM

lowercase() {
  printf '%s' "$1" | /usr/bin/tr '[:upper:]' '[:lower:]'
}

ask_yes_no() {
  local prompt="$1"
  local default_answer="$2"
  local override_name="${3:-}"
  local reply=""
  local override_value=""
  local suffix="[y/N]"

  if [[ "$default_answer" == "yes" ]]; then
    suffix="[Y/n]"
  fi

  if [[ -n "$override_name" ]]; then
    override_value="${!override_name:-}"
  fi

  while true; do
    if [[ -n "$override_value" ]]; then
      reply="$(lowercase "$override_value")"
    else
      if [[ "$TTY_AVAILABLE" -ne 1 ]]; then
        fail "An interactive answer is required, but /dev/tty is unavailable. Re-run from an interactive Terminal window."
      fi
      printf '%s%s%s %s ' "$C_BOLD" "$prompt" "$C_RESET" "$suffix" >&3
      if ! IFS= read -r reply <&3; then
        fail "Could not read from /dev/tty. Re-run the installer from an interactive Terminal window."
      fi
      reply="$(lowercase "$reply")"
    fi

    if [[ -z "$reply" ]]; then
      [[ "$default_answer" == "yes" ]]
      return
    fi

    case "$reply" in
      y|yes) return 0 ;;
      n|no) return 1 ;;
      *)
        if [[ -n "$override_value" ]]; then
          fail "Invalid value for $override_name: '$override_value'. Use yes or no."
        fi
        warn "Please answer yes or no."
        ;;
    esac
  done
}
version_at_least_310() {
  "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

resolve_brew() {
  local candidate=""
  for candidate in "${HOMEBREW_BREW_FILE:-}" /opt/homebrew/bin/brew /usr/local/bin/brew; do
    if [[ -n "$candidate" && -x "$candidate" ]]; then
      BREW_BIN="$candidate"
      return 0
    fi
  done
  candidate="$(command -v brew 2>/dev/null || true)"
  if [[ -n "$candidate" && -x "$candidate" ]]; then
    BREW_BIN="$candidate"
    return 0
  fi
  return 1
}

resolve_git() {
  local candidate=""
  for candidate in "${MAC_MCP_GIT:-}" /opt/homebrew/bin/git /usr/local/bin/git /usr/bin/git; do
    if [[ -n "$candidate" && -x "$candidate" ]]; then
      if "$candidate" --version >/dev/null 2>&1; then
        GIT_BIN="$candidate"
        return 0
      fi
    fi
  done
  candidate="$(command -v git 2>/dev/null || true)"
  if [[ -n "$candidate" && -x "$candidate" ]] && "$candidate" --version >/dev/null 2>&1; then
    GIT_BIN="$candidate"
    return 0
  fi
  return 1
}

resolve_python() {
  local candidate=""
  local found=""
  for candidate in \
    "${MAC_MCP_PYTHON:-}" \
    /opt/homebrew/bin/python3 \
    /usr/local/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
    "$(command -v python3 2>/dev/null || true)" \
    /usr/bin/python3; do
    [[ -z "$candidate" ]] && continue
    [[ "$candidate" == "$found" ]] && continue
    found="$candidate"
    if [[ -x "$candidate" ]] && version_at_least_310 "$candidate"; then
      PYTHON_BIN="$candidate"
      return 0
    fi
  done
  return 1
}

install_homebrew() {
  local default_choice="yes"
  local brew_script="$INSTALL_TMP/homebrew-install.sh"

  if [[ "$TTY_AVAILABLE" -ne 1 ]]; then
    fail "Homebrew is needed for a missing prerequisite, but /dev/tty is unavailable. Install the prerequisite manually and re-run."
  fi

  section "Homebrew"
  info "Homebrew is not currently available."
  info "Mac MCP itself does not require Homebrew when all required tools are already installed."

  if [[ "$MACOS_MAJOR" -le 13 ]]; then
    warn "Homebrew's current supported macOS baseline is macOS 14+."
    info "On macOS 13, Mac MCP remains supported when its prerequisites are already installed."
    return 1
  fi
  if [[ "$MAC_ARCH" == "x86_64" ]]; then
    warn "Homebrew currently classifies Intel macOS as Tier 3 and no longer builds new Intel bottles."
    default_choice="no"
  fi

  info "If you continue, the official Homebrew installer will run and may request your macOS administrator password."
  if ! ask_yes_no "Install Homebrew using the official installer?" "$default_choice" "MAC_MCP_INSTALL_HOMEBREW"; then
    return 1
  fi

  info "Downloading the official Homebrew installer."
  /usr/bin/curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh -o "$brew_script" \
    || fail "Could not download the Homebrew installer."
  /bin/chmod 700 "$brew_script"

  # Important for curl|bash: the Homebrew child must also read from the real terminal,
  # not from this installer's piped stdin.
  /bin/bash "$brew_script" <&3 || fail "Homebrew installation failed. Review the output above and re-run this installer."

  if ! resolve_brew; then
    fail "Homebrew finished installing, but 'brew' could not be located. Open a new Terminal window and re-run this installer."
  fi

  eval "$("$BREW_BIN" shellenv)"
  ok "Homebrew is available at $BREW_BIN"
  return 0
}

ensure_required_tools() {
  local need_git=0
  local need_python=0
  local packages=""

  section "System checks"

  [[ "$(/usr/bin/uname -s)" == "Darwin" ]] || fail "This installer only supports macOS."
  local mac_version
  mac_version="$(/usr/bin/sw_vers -productVersion)"
  MACOS_MAJOR="${mac_version%%.*}"
  [[ "$MACOS_MAJOR" =~ ^[0-9]+$ ]] || fail "Could not determine the macOS version."
  (( MACOS_MAJOR >= 13 )) || fail "macOS 13 Ventura or newer is required. Detected: $mac_version"
  ok "macOS $mac_version"

  MAC_ARCH="$(/usr/bin/uname -m)"
  case "$MAC_ARCH" in
    arm64) ok "Apple Silicon ($MAC_ARCH)" ;;
    x86_64) ok "Intel Mac ($MAC_ARCH)" ;;
    *) fail "Unsupported Mac architecture: $MAC_ARCH" ;;
  esac

  if ! /usr/bin/xcode-select -p >/dev/null 2>&1 || ! /usr/bin/xcrun --find swiftc >/dev/null 2>&1; then
    warn "Xcode Command Line Tools with swiftc are required."
    if [[ "$TTY_AVAILABLE" -eq 1 ]]; then
      info "Opening Apple's Command Line Tools installer. Complete it, then re-run Mac MCP installer."
      /usr/bin/xcode-select --install >/dev/null 2>&1 || true
    fi
    fail "Xcode Command Line Tools / swiftc are not ready yet."
  fi
  ok "Xcode Command Line Tools and swiftc"

  if resolve_git; then
    ok "Git: $("$GIT_BIN" --version)"
  else
    need_git=1
    warn "Git is missing."
  fi

  if resolve_python; then
    ok "Python: $("$PYTHON_BIN" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
  else
    need_python=1
    warn "Python 3.10+ is missing."
  fi

  if [[ "$need_git" -eq 1 || "$need_python" -eq 1 ]]; then
    if ! resolve_brew; then
      info "A required dependency is missing and Homebrew is not installed."
      if ! install_homebrew; then
        if [[ "$need_python" -eq 1 ]]; then
          fail "Python 3.10+ is required. Install it from https://www.python.org/downloads/macos/ (or another trusted package manager), then re-run this installer."
        fi
        fail "Git is required. Install Git/Xcode Command Line Tools, then re-run this installer."
      fi
    fi

    [[ "$need_git" -eq 1 ]] && packages="git"
    if [[ "$need_python" -eq 1 ]]; then
      if [[ -n "$packages" ]]; then
        packages="$packages python"
      else
        packages="python"
      fi
    fi

    info "Installing required package(s) with Homebrew: $packages"
    # Intentional word splitting for package names.
    local old_ifs="$IFS"
    IFS=' '
    # shellcheck disable=SC2086
    "$BREW_BIN" install $packages || fail "Homebrew could not install the required package(s): $packages"
    IFS="$old_ifs"

    resolve_git || fail "Git is still unavailable after dependency installation."
    resolve_python || fail "Python 3.10+ is still unavailable after dependency installation."
  fi

  info "Checking Python virtual-environment support."
  "$PYTHON_BIN" -m venv "$INSTALL_TMP/venv-check" >/dev/null 2>&1 \
    || fail "Python is present, but 'python -m venv' failed. Install a complete Python 3.10+ distribution and re-run."
  "$INSTALL_TMP/venv-check/bin/python" -m pip --version >/dev/null 2>&1 \
    || fail "Python venv was created, but pip is unavailable."
  /bin/rm -rf "$INSTALL_TMP/venv-check"
  ok "Python venv and pip"
}

handle_optional_helpers() {
  local missing=""
  local helper=""

  section "Optional helpers"
  for helper in cliclick brightness; do
    if ! command -v "$helper" >/dev/null 2>&1; then
      if [[ -n "$missing" ]]; then
        missing="$missing $helper"
      else
        missing="$helper"
      fi
    fi
  done

  if [[ -z "$missing" ]]; then
    ok "Optional helpers cliclick and brightness are already installed."
    return 0
  fi

  info "cliclick enables coordinate-based mouse and typing fallbacks."
  info "brightness enables the set_brightness tool."
  info "Missing optional helpers: $missing"

  if ! resolve_brew; then
    info "Homebrew is not installed, so optional helpers will not trigger a package-manager installation by themselves."
    info "You can add them later with Homebrew using: brew install cliclick brightness"
    return 0
  fi

  if ask_yes_no "Install the missing optional helpers?" "yes" "MAC_MCP_INSTALL_HELPERS"; then
    local old_ifs="$IFS"
    IFS=' '
    # shellcheck disable=SC2086
    "$BREW_BIN" install $missing || warn "One or more optional helpers could not be installed. Core Mac MCP installation will continue."
    IFS="$old_ifs"
  else
    info "Skipping optional helpers. Core Mac MCP functionality will still be installed."
  fi
}

check_install_targets() {
  section "Install plan"
  printf '  Source checkout: %s\n' "$SOURCE_DIR"
  printf '  Runtime:         %s\n' "$RUNTIME_DIR"
  printf '  CLI:             %s\n' "$CLI_PATH"
  printf '  Menu bar app:    %s\n' "$APP_PATH"
  printf '  Branch:          %s\n' "$BRANCH"
  info "OpenCode and Codex are not installed by this installer."

  if [[ -e "$SOURCE_DIR" || -L "$SOURCE_DIR" ]]; then
    fail "Source path already exists: $SOURCE_DIR. This installer will not overwrite an existing checkout. Use 'mac-mcp update' for an existing installation."
  fi
  if [[ -e "$RUNTIME_DIR" || -L "$RUNTIME_DIR" ]]; then
    fail "Runtime path already exists: $RUNTIME_DIR. This installer will not overwrite an existing runtime."
  fi
  if [[ -e "$CLI_PATH" || -L "$CLI_PATH" ]]; then
    fail "CLI path already exists: $CLI_PATH. Existing commands are never overwritten. Move it aside or choose a different MAC_MCP_BIN_DIR and re-run."
  fi
}

clone_source_and_runtime() {
  local source_stage="$INSTALL_TMP/source"
  local runtime_stage="$INSTALL_TMP/runtime"
  local commit=""

  /bin/mkdir -p "$(/usr/bin/dirname "$SOURCE_DIR")" "$(/usr/bin/dirname "$RUNTIME_DIR")"

  info "Cloning Mac MCP source."
  "$GIT_BIN" clone --quiet --branch "$BRANCH" --single-branch "$REPO_URL" "$source_stage" \
    || fail "Could not clone $REPO_URL (branch: $BRANCH)."
  commit="$("$GIT_BIN" -C "$source_stage" rev-parse HEAD)"
  /bin/mv "$source_stage" "$SOURCE_DIR"
  CREATED_SOURCE=1
  ok "Source cloned at commit ${commit:0:8}."

  info "Creating an isolated runtime copy from the same Git commit."
  /bin/mkdir -p "$runtime_stage"
  "$GIT_BIN" -C "$SOURCE_DIR" archive --format=tar "$commit" | /usr/bin/tar -xf - -C "$runtime_stage" \
    || fail "Could not create the runtime copy."
  /bin/mv "$runtime_stage" "$RUNTIME_DIR"
  CREATED_RUNTIME=1
  ok "Runtime created without Git metadata."

  INSTALLED_COMMIT="$commit"
}

configure_runtime() {
  local env_file="$RUNTIME_DIR/mcp_server/.env"
  local api_key=""

  [[ -f "$RUNTIME_DIR/mcp_server/.env.example" ]] || fail "mcp_server/.env.example is missing from the runtime."
  /bin/cp "$RUNTIME_DIR/mcp_server/.env.example" "$env_file"

  api_key="$($PYTHON_BIN -c 'import secrets; print(secrets.token_urlsafe(48))')"
  [[ -n "$api_key" ]] || fail "Could not generate an MCP API key."

  "$PYTHON_BIN" - "$env_file" "$api_key" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
key = sys.argv[2]
lines = path.read_text(encoding="utf-8").splitlines()
values = {
    "MCP_API_KEY": key,
    "MCP_ALLOW_NO_AUTH": "false",
    "MCP_ALLOW_SHELL": "true",
    "NGROK_DOMAIN": "",
}
out = []
seen = set()
for line in lines:
    if "=" in line and not line.lstrip().startswith("#"):
        name = line.split("=", 1)[0].strip()
        if name in values:
            out.append(f"{name}={values[name]}")
            seen.add(name)
            continue
    out.append(line)
for name, value in values.items():
    if name not in seen:
        out.append(f"{name}={value}")
path.write_text("\n".join(out) + "\n", encoding="utf-8")
PY

  /bin/chmod 600 "$env_file"
  API_KEY="$api_key"
  ok "Generated a strong MCP API key and stored it in mcp_server/.env (mode 600)."
}

install_python_environment() {
  section "Python environment"
  info "Creating the runtime virtual environment with $PYTHON_BIN"
  "$PYTHON_BIN" -m venv "$RUNTIME_DIR/.venv" || fail "Could not create the runtime virtual environment."

  info "Installing Mac MCP and its Python dependencies."
  "$RUNTIME_DIR/.venv/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade pip setuptools wheel \
    || fail "Could not prepare pip/setuptools/wheel."
  "$RUNTIME_DIR/.venv/bin/python" -m pip install --quiet --disable-pip-version-check -e "$RUNTIME_DIR" \
    || fail "Could not install Mac MCP Python dependencies."
  "$RUNTIME_DIR/.venv/bin/python" -m pip check >/dev/null \
    || fail "Python dependency verification failed."
  "$RUNTIME_DIR/.venv/bin/python" -c 'import fastapi, uvicorn, mcp; from pathlib import Path; import sys; assert (Path(sys.prefix).parent / "mcp_server").is_dir()' \
    || fail "Mac MCP Python environment verification failed."
  ok "Python environment verified."
}

install_update_state_and_cli() {
  local profile="$HOME/.zprofile"
  local path_line='export PATH="$HOME/.local/bin:$PATH"'

  /bin/mkdir -p "$BIN_DIR"
  if [[ -e "$CLI_PATH" || -L "$CLI_PATH" ]]; then
    /bin/rm -f "$CLI_PATH"
  fi
  /bin/ln -s "$RUNTIME_DIR/.venv/bin/mac-mcp" "$CLI_PATH"
  CREATED_CLI=1
  [[ -x "$CLI_PATH" ]] || fail "CLI symlink was created but is not executable: $CLI_PATH"
  MAC_MCP_SKIP_MENU_APP_INSTALL=1 MAC_MCP_SKIP_MENU_APP=1 "$CLI_PATH" --help >/dev/null 2>&1 || fail "CLI verification failed."

  if [[ "$BIN_DIR" == "$HOME/.local/bin" ]]; then
    if ! /usr/bin/grep -Fq "$path_line" "$profile" 2>/dev/null; then
      {
        printf '\n# Mac MCP\n'
        printf '%s\n' "$path_line"
      } >> "$profile"
      ok "Added $HOME/.local/bin to PATH in $profile."
    else
      ok "$HOME/.local/bin is already configured in $profile."
    fi
  else
    info "Custom CLI directory used. Ensure it is in PATH: $BIN_DIR"
  fi

  /bin/mkdir -p "$STATE_DIR/update"
  printf '%s\n' "$INSTALLED_COMMIT" > "$STATE_DIR/update/deployed-commit" \
    || fail "Could not record updater state."
  ok "Recorded installed commit for the built-in updater."

  ok "CLI installed: $CLI_PATH"
}

install_menu_app() {
  section "Menu bar app"
  info "Building the native SwiftUI menu bar controller included with Mac MCP."

  if [[ -e "$APP_PATH" || -L "$APP_PATH" ]]; then
    warn "A menu bar app already exists at $APP_PATH"
    if ! ask_yes_no "Replace the existing Mac MCP.app?" "no" "MAC_MCP_REPLACE_APP"; then
      info "Existing Mac MCP.app was preserved."
      return 0
    fi
    APP_BACKUP_PATH="$INSTALL_TMP/existing-Mac-MCP.app"
    /bin/cp -R "$APP_PATH" "$APP_BACKUP_PATH" \
      || fail "Could not create a rollback copy of the existing Mac MCP.app."
    BACKED_UP_APP=1
  fi

  [[ -x "$RUNTIME_DIR/menu_app/install_app.sh" ]] || fail "menu_app/install_app.sh is missing or not executable."
  "$RUNTIME_DIR/menu_app/install_app.sh" "$APP_PATH" \
    || fail "The native menu bar app failed to build or install."
  /usr/bin/codesign --verify --deep --strict "$APP_PATH" \
    || fail "The installed menu bar app failed code-signature verification."
  if [[ "$BACKED_UP_APP" -eq 0 ]]; then
    CREATED_APP=1
  fi
  ok "Menu bar app installed and code-signature verified."
}

optionally_start_server() {
  section "Start Mac MCP"
  if ask_yes_no "Start the local Mac MCP server now?" "no" "MAC_MCP_START_NOW"; then
    if MAC_MCP_SKIP_MENU_APP_INSTALL=1 MAC_MCP_SKIP_MENU_APP=1 "$CLI_PATH" start; then
      ok "Local Mac MCP server started."
    else
      warn "Installation completed, but the local server did not start. Run 'mac-mcp start' after reviewing the reported error."
    fi
  else
    info "Not starting the server automatically. Run 'mac-mcp start' when ready."
  fi
}

print_completion() {
  section "Installation complete"
  printf '  Source:             %s\n' "$SOURCE_DIR"
  printf '  Runtime:            %s\n' "$RUNTIME_DIR"
  printf '  CLI:                %s\n' "$CLI_PATH"
  printf '  Local MCP endpoint: http://127.0.0.1:8000/mcp\n'
  printf '  Dashboard:          http://127.0.0.1:8000/dashboard\n'

  printf '\n%sAuthentication%s\n' "$C_BOLD" "$C_RESET"
  printf '  API key: %s\n' "$API_KEY"
  printf '  Preferred client auth: Authorization: Bearer <API_KEY>\n'
  printf '  Header-limited clients: http://127.0.0.1:8000/mcp?ApiKey=<API_KEY>\n'
  printf '  The key is stored locally in: %s/mcp_server/.env\n' "$RUNTIME_DIR"

  printf '\n'
  info "A public HTTPS endpoint is optional. Install/configure ngrok separately, set NGROK_DOMAIN in mcp_server/.env, then use 'mac-mcp start --ngrok'."
  info "macOS may ask for Accessibility, Screen Recording, Automation, or Microphone permissions when you first use features that need them."

  printf '\n%sSubagents%s\n' "$C_BOLD" "$C_RESET"
  printf '  OpenCode and Codex are not installed by Mac MCP.\n'
  printf '  If you plan to use Subagents, installing OpenCode and/or Codex separately is recommended.\n'

  if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    printf '\n'
    info "Open a new Terminal window (or reload your shell profile) before running 'mac-mcp' by name."
  fi
  printf '%sNext command:%s mac-mcp start\n\n' "$C_BOLD" "$C_RESET"
}

main() {
  if [[ "${EUID:-$(/usr/bin/id -u)}" -eq 0 ]]; then
    fail "Do not run this installer with sudo. Mac MCP is installed for your user account."
  fi

  INSTALL_TMP="$(/usr/bin/mktemp -d "${TMPDIR:-/tmp}/mac-mcp-install.XXXXXX")"
  print_header
  ensure_required_tools
  handle_optional_helpers
  check_install_targets
  clone_source_and_runtime
  configure_runtime
  install_python_environment
  install_menu_app
  install_update_state_and_cli
  optionally_start_server
  print_completion
}

main "$@"
