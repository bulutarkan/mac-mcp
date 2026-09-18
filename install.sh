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
RELEASE_TRUSTED_SIGNER='mac-mcp-release ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMllSlrqFdnIb1ggvo72blY/JEQkOELwqwtvB7qCY8S2'
RELEASE_SIGNATURE_IDENTITY="mac-mcp-release"
RELEASE_SIGNATURE_NAMESPACE="mac-mcp-release"
RELEASE_BOOTSTRAP_VERIFIER_PATH="scripts/installer_release_verify.py"
RELEASE_BOOTSTRAP_VERIFIER_SHA256="9bdf7691f2f30c501554fcf6d1a29498759d30e4b998a40dd2793a5e04ea48d3"
VERIFIED_RELEASE_ID=""
VERIFIED_RELEASE_VERSION=""
VERIFIED_RELEASE_PAYLOAD=""
VERIFIED_RELEASE_COMMIT=""
CHATGPT_CLI_REPO_URL="${MAC_MCP_CHATGPT_CLI_REPO_URL:-https://github.com/bulutarkan/chatgpt-web-cli.git}"
CHATGPT_CLI_SOURCE_DIR="${MAC_MCP_CHATGPT_CLI_SOURCE_DIR:-$HOME/Projects/chatgpt-web-cli}"
CHATGPT_CLI_LINK="$BIN_DIR/chatgpt-web"

BREW_BIN=""
GIT_BIN=""
PYTHON_BIN=""
NODE_BIN=""
NPM_BIN=""
CHATGPT_CLI_BINARY=""
CHATGPT_PROVIDER_ENABLED=0
PUBLIC_ENDPOINT_MODE="none"
PUBLIC_ENDPOINT_URL=""
PUBLIC_ENDPOINT_CONFIGURED=0
PUBLIC_PROVIDER_AVAILABLE=1
PUBLIC_PROVIDER_BIN=""
NGROK_DOMAIN_INPUT=""
TEXT_REPLY=""
SECRET_REPLY=""
MACOS_MAJOR=""
MAC_ARCH=""
INSTALL_TMP=""
TTY_AVAILABLE=0
CREATED_SOURCE=0
CREATED_RUNTIME=0
CREATED_CLI=0
CREATED_APP=0
CREATED_CHATGPT_SOURCE=0
CREATED_CHATGPT_LINK=0
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
    if [[ "$CREATED_CHATGPT_LINK" -eq 1 && -L "$CHATGPT_CLI_LINK" ]]; then
      /bin/rm -f "$CHATGPT_CLI_LINK" || true
    fi
    if [[ "$CREATED_CHATGPT_SOURCE" -eq 1 && -d "$CHATGPT_CLI_SOURCE_DIR" ]]; then
      /bin/rm -rf "$CHATGPT_CLI_SOURCE_DIR" || true
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
ask_text() {
  local prompt="$1"
  local override_name="${2:-}"
  local override_value=""
  TEXT_REPLY=""

  if [[ -n "$override_name" ]]; then
    override_value="${!override_name:-}"
  fi
  if [[ -n "$override_value" ]]; then
    TEXT_REPLY="$override_value"
    return 0
  fi
  if [[ "$TTY_AVAILABLE" -ne 1 ]]; then
    return 1
  fi
  printf '%s%s%s ' "$C_BOLD" "$prompt" "$C_RESET" >&3
  IFS= read -r TEXT_REPLY <&3 || return 1
}

read_secret() {
  local prompt="$1"
  SECRET_REPLY=""
  if [[ "$TTY_AVAILABLE" -ne 1 ]]; then
    return 1
  fi
  printf '%s%s%s ' "$C_BOLD" "$prompt" "$C_RESET" >&3
  IFS= read -r -s SECRET_REPLY <&3 || return 1
  printf '\n' >&3
}

choose_public_endpoint_mode() {
  local requested="${MAC_MCP_INSTALL_PUBLIC_MODE:-}"
  local reply=""
  section "Public endpoint"
  info "Choose how Mac MCP should be reachable. Local only is the safest default and can be changed later in Settings."

  while true; do
    if [[ -n "$requested" ]]; then
      reply="$(lowercase "$requested")"
    else
      if [[ "$TTY_AVAILABLE" -ne 1 ]]; then
        PUBLIC_ENDPOINT_MODE="none"
        info "No interactive terminal is available; defaulting to Local only."
        return 0
      fi
      printf '  1) Local only (default)\n' >&3
      printf '  2) Cloudflare Tunnel\n' >&3
      printf '  3) ngrok\n' >&3
      printf '  4) Custom HTTPS\n' >&3
      printf '%sSelect public endpoint [1]:%s ' "$C_BOLD" "$C_RESET" >&3
      IFS= read -r reply <&3 || fail "Could not read public endpoint selection from /dev/tty."
      [[ -n "$reply" ]] || reply="1"
    fi

    case "$reply" in
      1|none|local|local-only|local_only) PUBLIC_ENDPOINT_MODE="none"; return 0 ;;
      2|cloudflare|cloudflare-tunnel|cloudflare_tunnel) PUBLIC_ENDPOINT_MODE="cloudflare"; return 0 ;;
      3|ngrok) PUBLIC_ENDPOINT_MODE="ngrok"; return 0 ;;
      4|custom|custom-https|custom_https) PUBLIC_ENDPOINT_MODE="custom"; return 0 ;;
      *)
        if [[ -n "$requested" ]]; then
          fail "Invalid MAC_MCP_INSTALL_PUBLIC_MODE: '$requested'. Use local, cloudflare, ngrok, or custom."
        fi
        warn "Choose 1, 2, 3, or 4."
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

resolve_node() {
  local candidate=""
  for candidate in "${MAC_MCP_NODE:-}" /opt/homebrew/bin/node /usr/local/bin/node "$(command -v node 2>/dev/null || true)"; do
    [[ -z "$candidate" ]] && continue
    if [[ -x "$candidate" ]] && "$candidate" --version >/dev/null 2>&1; then
      NODE_BIN="$candidate"
      break
    fi
  done
  [[ -n "$NODE_BIN" ]] || return 1
  for candidate in "${MAC_MCP_NPM:-}" /opt/homebrew/bin/npm /usr/local/bin/npm "$(command -v npm 2>/dev/null || true)"; do
    [[ -z "$candidate" ]] && continue
    if [[ -x "$candidate" ]] && "$candidate" --version >/dev/null 2>&1; then
      NPM_BIN="$candidate"
      return 0
    fi
  done
  NODE_BIN=""
  return 1
}

resolve_chatgpt_cli() {
  local candidate=""
  for candidate in \
    "${CHATGPT_WEB_CLI_BINARY:-}" \
    "$(command -v chatgpt-web 2>/dev/null || true)" \
    "$CHATGPT_CLI_SOURCE_DIR/bin/chatgpt"; do
    [[ -z "$candidate" ]] && continue
    if [[ -x "$candidate" ]]; then
      CHATGPT_CLI_BINARY="$candidate"
      return 0
    fi
  done
  CHATGPT_CLI_BINARY=""
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

resolve_public_provider_binary() {
  local name="$1"
  local candidate=""
  PUBLIC_PROVIDER_BIN=""
  for candidate in \
    "$(command -v "$name" 2>/dev/null || true)" \
    "/opt/homebrew/bin/$name" \
    "/usr/local/bin/$name"; do
    [[ -z "$candidate" ]] && continue
    if [[ -x "$candidate" ]]; then
      PUBLIC_PROVIDER_BIN="$candidate"
      return 0
    fi
  done
  return 1
}

install_selected_public_provider() {
  local package=""
  local override=""
  PUBLIC_PROVIDER_AVAILABLE=1
  case "$PUBLIC_ENDPOINT_MODE" in
    cloudflare) package="cloudflared"; override="MAC_MCP_INSTALL_CLOUDFLARED" ;;
    ngrok) package="ngrok"; override="MAC_MCP_INSTALL_NGROK" ;;
    *) return 0 ;;
  esac

  section "Public endpoint provider"
  if resolve_public_provider_binary "$package"; then
    ok "$package is already available at $PUBLIC_PROVIDER_BIN"
    return 0
  fi

  warn "$package is required for the selected public endpoint mode but is not installed."
  if ! resolve_brew; then
    info "Homebrew can install $package for you."
    if ! install_homebrew; then
      PUBLIC_PROVIDER_AVAILABLE=0
      warn "$package was not installed. Mac MCP core installation will continue in Local only mode."
      return 0
    fi
  fi

  if ask_yes_no "Install $package with Homebrew?" "yes" "$override"; then
    if "$BREW_BIN" install "$package"; then
      resolve_public_provider_binary "$package" || true
    fi
  fi
  if [[ -z "$PUBLIC_PROVIDER_BIN" ]]; then
    PUBLIC_PROVIDER_AVAILABLE=0
    warn "$package is still unavailable. Mac MCP core installation will continue in Local only mode."
    return 0
  fi
  ok "$package installed and available at $PUBLIC_PROVIDER_BIN"
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

handle_optional_chatgpt_cli() {
  section "Optional ChatGPT Web CLI"
  warn "This experimental provider automates chatgpt.com through your own authenticated browser session."
  warn "It is not an official OpenAI CLI or API integration. Use it at your own risk."

  if resolve_chatgpt_cli; then
    ok "Detected ChatGPT Web CLI: $CHATGPT_CLI_BINARY"
    if ask_yes_no "Enable the detected ChatGPT Web CLI for Mac MCP Subagents?" "yes" "MAC_MCP_ENABLE_CHATGPT_CLI"; then
      CHATGPT_PROVIDER_ENABLED=1
    else
      CHATGPT_PROVIDER_ENABLED=0
      info "ChatGPT Web CLI will remain disabled and hidden from the Subagent catalog."
    fi
    return 0
  fi

  info "ChatGPT Web CLI is not installed."
  if ! ask_yes_no "Install the experimental ChatGPT Web CLI? (Use it at your own risk)" "no" "MAC_MCP_INSTALL_CHATGPT_CLI"; then
    CHATGPT_PROVIDER_ENABLED=0
    info "Skipping ChatGPT Web CLI. The provider will be disabled and hidden from the Subagent catalog."
    return 0
  fi

  if ! resolve_node; then
    if resolve_brew && ask_yes_no "Node.js is required. Install Node.js with Homebrew?" "yes" "MAC_MCP_INSTALL_NODE"; then
      "$BREW_BIN" install node || { warn "Node.js installation failed. ChatGPT Web CLI will remain disabled."; CHATGPT_PROVIDER_ENABLED=0; return 0; }
      resolve_node || { warn "Node.js/npm could not be detected after installation. ChatGPT Web CLI will remain disabled."; CHATGPT_PROVIDER_ENABLED=0; return 0; }
    else
      warn "Node.js and npm are required for ChatGPT Web CLI. Provider will remain disabled."
      CHATGPT_PROVIDER_ENABLED=0
      return 0
    fi
  fi

  if [[ -e "$CHATGPT_CLI_SOURCE_DIR" || -L "$CHATGPT_CLI_SOURCE_DIR" ]]; then
    warn "ChatGPT Web CLI target already exists but no executable was detected: $CHATGPT_CLI_SOURCE_DIR"
    warn "The installer will not overwrite it. Provider will remain disabled."
    CHATGPT_PROVIDER_ENABLED=0
    return 0
  fi

  /bin/mkdir -p "$(/usr/bin/dirname "$CHATGPT_CLI_SOURCE_DIR")"
  info "Cloning the optional ChatGPT Web CLI."
  if ! "$GIT_BIN" clone --quiet "$CHATGPT_CLI_REPO_URL" "$CHATGPT_CLI_SOURCE_DIR"; then
    warn "ChatGPT Web CLI could not be downloaded. Mac MCP installation will continue with that provider disabled."
    /bin/rm -rf "$CHATGPT_CLI_SOURCE_DIR" || true
    CHATGPT_PROVIDER_ENABLED=0
    return 0
  fi
  CREATED_CHATGPT_SOURCE=1

  if ! (cd "$CHATGPT_CLI_SOURCE_DIR" && "$NPM_BIN" install --omit=dev --ignore-scripts --no-audit --no-fund); then
    warn "ChatGPT Web CLI dependencies could not be installed. Provider will remain disabled."
    /bin/rm -rf "$CHATGPT_CLI_SOURCE_DIR" || true
    CREATED_CHATGPT_SOURCE=0
    CHATGPT_PROVIDER_ENABLED=0
    return 0
  fi

  /bin/chmod +x "$CHATGPT_CLI_SOURCE_DIR/bin/chatgpt" 2>/dev/null || true
  if ! resolve_chatgpt_cli; then
    warn "ChatGPT Web CLI installed but its executable was not found. Provider will remain disabled."
    CHATGPT_PROVIDER_ENABLED=0
    return 0
  fi
  CHATGPT_PROVIDER_ENABLED=1
  ok "ChatGPT Web CLI installed. Run '$CHATGPT_CLI_BINARY login' once before first use."
}

configure_subagent_provider_settings() {
  local settings_file="$STATE_DIR/settings.json"
  /bin/mkdir -p "$STATE_DIR"
  "$PYTHON_BIN" - "$settings_file" "$CHATGPT_PROVIDER_ENABLED" "$CHATGPT_CLI_BINARY" <<'PYSETTINGS'
import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
enabled = sys.argv[2] == "1"
binary = sys.argv[3].strip()
try:
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
except (OSError, json.JSONDecodeError):
    data = {}
if not isinstance(data, dict):
    data = {}
server = data.setdefault("server", {})
if not isinstance(server, dict):
    server = data["server"] = {}
server.setdefault("port", 8000)
server.setdefault("cli_path", "")
server.setdefault("ngrok_on_start", False)
server.setdefault("public_endpoint_mode", "ngrok" if bool(server.get("ngrok_on_start")) else "none")
server.setdefault("public_url", "")
server.setdefault("cloudflare_tunnel", "")
subagents = data.setdefault("subagents", {})
if not isinstance(subagents, dict):
    subagents = data["subagents"] = {}
providers = subagents.setdefault("providers", {})
if not isinstance(providers, dict):
    providers = subagents["providers"] = {}
providers.setdefault("opencode", {}).setdefault("enabled", True)
providers.setdefault("codex", {}).setdefault("enabled", True)
chatgpt = providers.setdefault("chatgpt", {})
chatgpt["enabled"] = enabled
if binary:
    chatgpt["binary_path"] = binary
else:
    chatgpt.pop("binary_path", None)
chatgpt.setdefault("default_project", "")
tmp = path.with_name(path.name + ".tmp")
tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
os.chmod(tmp, 0o600)
os.replace(tmp, path)
PYSETTINGS
  /bin/chmod 600 "$settings_file"
  if [[ "$CHATGPT_PROVIDER_ENABLED" -eq 1 ]]; then
    ok "ChatGPT Web CLI provider enabled in local Subagent settings."
  else
    info "ChatGPT Web CLI provider disabled in local Subagent settings."
  fi
}

check_install_targets() {
  section "Install plan"
  printf '  Source checkout: %s\n' "$SOURCE_DIR"
  printf '  Runtime:         %s\n' "$RUNTIME_DIR"
  printf '  CLI:             %s\n' "$CLI_PATH"
  printf '  Menu bar app:    %s\n' "$APP_PATH"
  printf '  Branch:          %s\n' "$BRANCH"
  info "OpenCode and Codex are not installed by this installer. ChatGPT Web CLI is optional and handled separately."

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

verify_release_checkout() {
  local checkout="$1"
  local commit="$2"
  local allowed_signers="$INSTALL_TMP/release-trusted-signers"
  local verifier_file="$INSTALL_TMP/installer-release-verify.py"
  local verifier_sha=""
  local verified=""

  [[ -x /usr/bin/ssh-keygen ]] || fail "ssh-keygen is required to verify Mac MCP releases."
  [[ -x /usr/bin/shasum ]] || fail "shasum is required to verify Mac MCP releases."
  printf '%s\n' "$RELEASE_TRUSTED_SIGNER" > "$allowed_signers"
  /bin/chmod 600 "$allowed_signers" || fail "Could not secure the release trust file."

  "$GIT_BIN" -C "$checkout" show "$commit:$RELEASE_BOOTSTRAP_VERIFIER_PATH" > "$verifier_file" 2>/dev/null \
    || fail "The selected commit is missing the pinned release verifier."
  verifier_sha="$(/usr/bin/shasum -a 256 "$verifier_file" | /usr/bin/awk '{print $1}')"
  [[ "$verifier_sha" == "$RELEASE_BOOTSTRAP_VERIFIER_SHA256" ]] \
    || fail "Release verifier hash mismatch. Source/runtime were not installed."
  /bin/chmod 700 "$verifier_file" || fail "Could not secure the release verifier."

  verified="$("$PYTHON_BIN" "$verifier_file" \
    --repo "$checkout" \
    --commit "$commit" \
    --signers "$allowed_signers" \
    --branch "$BRANCH")" \
    || fail "Mac MCP signed release verification failed. Source/runtime were not installed."

  VERIFIED_RELEASE_ID="$("$PYTHON_BIN" -c 'import json,sys; print(json.loads(sys.argv[1])["release_id"])' "$verified")"
  VERIFIED_RELEASE_VERSION="$("$PYTHON_BIN" -c 'import json,sys; print(json.loads(sys.argv[1])["version"])' "$verified")"
  VERIFIED_RELEASE_PAYLOAD="$("$PYTHON_BIN" -c 'import json,sys; print(json.loads(sys.argv[1])["payload_sha256"])' "$verified")"
  [[ -n "$VERIFIED_RELEASE_ID" && -n "$VERIFIED_RELEASE_VERSION" && -n "$VERIFIED_RELEASE_PAYLOAD" ]] \
    || fail "Verified release metadata was incomplete."
  ok "Verified signed release: $VERIFIED_RELEASE_ID (v$VERIFIED_RELEASE_VERSION)."
}

select_verified_release_commit() {
  local checkout="$1"
  local branch_tip="$2"
  local candidate=""
  local has_manifest=0
  local has_signature=0
  local scanned=0
  local changed_markers=""

  while IFS= read -r candidate; do
    [[ -n "$candidate" ]] || continue
    scanned=$((scanned + 1))
    has_manifest=0
    has_signature=0
    changed_markers="$("$GIT_BIN" -C "$checkout" diff-tree --no-commit-id --name-only -r "$candidate" -- release/stable-manifest.json release/stable-manifest.json.sig)"
    printf '%s\n' "$changed_markers" | /usr/bin/grep -qx 'release/stable-manifest.json' && has_manifest=1 || true
    printf '%s\n' "$changed_markers" | /usr/bin/grep -qx 'release/stable-manifest.json.sig' && has_signature=1 || true
    if [[ "$has_manifest" -eq 0 && "$has_signature" -eq 0 ]]; then
      continue
    fi
    if [[ "$has_manifest" -ne "$has_signature" ]]; then
      fail "Verified release channel is blocked at ${candidate:0:8}: manifest/signature pair is incomplete."
    fi
    verify_release_checkout "$checkout" "$candidate"
    VERIFIED_RELEASE_COMMIT="$candidate"
    return 0
  done < <("$GIT_BIN" -C "$checkout" rev-list --first-parent --max-count=512 "$branch_tip")

  fail "No verified stable Mac MCP release was found within the newest $scanned commits."
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
  select_verified_release_commit "$source_stage" "$commit"
  commit="$VERIFIED_RELEASE_COMMIT"
  "$GIT_BIN" -C "$source_stage" reset --hard --quiet "$commit" \
    || fail "Could not check out the verified stable release."
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
    "MAC_MCP_PERMISSION_PROFILE": "standard",
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
  info "Secure bootstrap profile: standard. Enable Trusted/Full Access explicitly in Settings when needed."
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

prepare_chrome_companion() {
  section "Chrome Background Companion"
  MAC_MCP_STATE_DIR="$STATE_DIR" MAC_MCP_PORT="${MAC_MCP_PORT:-8000}" PYTHONPATH="$RUNTIME_DIR" \
    "$RUNTIME_DIR/.venv/bin/python" -c 'from mcp_server.chrome_background_bridge import ensure_chrome_companion_config; p=ensure_chrome_companion_config(); assert p and p.is_file()' \
    || fail "Could not prepare the Chrome background companion configuration."
  /bin/chmod 600 "$STATE_DIR/chrome-companion-token" "$RUNTIME_DIR/menu_app/ChromeVisualCompanion/bridge_config.js" \
    || fail "Could not secure the Chrome background companion credentials."
  ok "Chrome background companion configured with a dedicated local credential."
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
  local safari_extension="$APP_PATH/Contents/PlugIns/Mac MCP Safari Visual Companion.appex"
  [[ -d "$safari_extension" ]] \
    || fail "The bundled Safari Visual Companion extension is missing from Mac MCP.app."
  /usr/bin/codesign --verify --strict "$safari_extension" \
    || fail "The bundled Safari Visual Companion extension failed code-signature verification."
  if [[ "$BACKED_UP_APP" -eq 0 ]]; then
    CREATED_APP=1
  fi
  ok "Menu bar app and bundled Safari Visual Companion installed and code-signature verified."
}

persist_public_endpoint_config() {
  local mode="$1"
  local public_url="$2"
  local ngrok_domain="$3"
  local settings_file="$STATE_DIR/settings.json"
  local env_file="$RUNTIME_DIR/mcp_server/.env"

  "$PYTHON_BIN" - "$settings_file" "$env_file" "$mode" "$public_url" "$ngrok_domain" <<'PYPUBLIC'
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

settings_path = Path(sys.argv[1])
env_path = Path(sys.argv[2])
mode = sys.argv[3]
public_url = sys.argv[4].strip()
ngrok_domain = sys.argv[5].strip()

if mode not in {"none", "ngrok", "cloudflare", "custom"}:
    raise SystemExit("invalid public endpoint mode")

if public_url:
    parts = urlsplit(public_url)
    if parts.scheme.lower() != "https" or not parts.netloc or parts.username or parts.password or parts.query or parts.fragment:
        raise SystemExit("public URL must be a plain https:// hostname without credentials, query, or fragment")
    path = parts.path.rstrip("/")
    if not path:
        path = "/mcp"
    public_url = urlunsplit(("https", parts.netloc, path, "", ""))

if mode == "ngrok":
    normalized_domain = ngrok_domain.lower().rstrip("/")
    if (not normalized_domain or "." not in normalized_domain or normalized_domain.startswith("http://")
            or normalized_domain.startswith("https://") or "/" in normalized_domain):
        raise SystemExit("ngrok domain must contain only a hostname such as example.ngrok-free.app")
    ngrok_domain = normalized_domain

try:
    data = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
except (OSError, json.JSONDecodeError):
    data = {}
if not isinstance(data, dict):
    data = {}
server = data.setdefault("server", {})
if not isinstance(server, dict):
    server = data["server"] = {}
server["public_endpoint_mode"] = mode
server["public_url"] = public_url
server["ngrok_on_start"] = mode == "ngrok"
server.setdefault("cloudflare_tunnel", "")
tmp = settings_path.with_name(settings_path.name + ".tmp")
tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
os.chmod(tmp, 0o600)
os.replace(tmp, settings_path)

if env_path.exists():
    lines = env_path.read_text(encoding="utf-8").splitlines()
else:
    lines = []
out = []
seen = False
for line in lines:
    if line.startswith("NGROK_DOMAIN="):
        out.append("NGROK_DOMAIN=" + ngrok_domain)
        seen = True
    else:
        out.append(line)
if not seen:
    out.append("NGROK_DOMAIN=" + ngrok_domain)
env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
os.chmod(env_path, 0o600)
PYPUBLIC
}

configure_public_endpoint() {
  local url=""
  local domain=""
  local port="8000"
  PUBLIC_ENDPOINT_CONFIGURED=0

  section "Configure public endpoint"
  case "$PUBLIC_ENDPOINT_MODE" in
    none)
      persist_public_endpoint_config "none" "" ""
      PUBLIC_ENDPOINT_CONFIGURED=1
      ok "Local only selected. No public tunnel provider will start."
      return 0
      ;;
    custom)
      if ! ask_text "Custom HTTPS MCP URL (for example https://mac.example.com; blank = configure later):" "MAC_MCP_INSTALL_PUBLIC_URL"; then
        TEXT_REPLY=""
      fi
      url="$TEXT_REPLY"
      if [[ -z "$url" ]]; then
        persist_public_endpoint_config "none" "" ""
        warn "Custom HTTPS configuration deferred. Local only will remain active."
        return 0
      fi
      if ! persist_public_endpoint_config "custom" "$url" ""; then
        persist_public_endpoint_config "none" "" ""
        warn "Invalid custom HTTPS URL. Local only will remain active; configure it later in Settings."
        return 0
      fi
      PUBLIC_ENDPOINT_URL="$url"
      PUBLIC_ENDPOINT_CONFIGURED=1
      ok "Custom HTTPS endpoint configured."
      return 0
      ;;
    ngrok)
      if [[ "$PUBLIC_PROVIDER_AVAILABLE" -ne 1 ]]; then
        persist_public_endpoint_config "none" "" ""
        warn "ngrok is unavailable. Local only will remain active."
        return 0
      fi
      if ! ask_text "ngrok domain (for example example.ngrok-free.app; blank = configure later):" "MAC_MCP_INSTALL_NGROK_DOMAIN"; then
        TEXT_REPLY=""
      fi
      domain="$TEXT_REPLY"
      if [[ -z "$domain" ]]; then
        persist_public_endpoint_config "none" "" ""
        warn "ngrok configuration deferred. Local only will remain active."
        return 0
      fi
      persist_public_endpoint_config "ngrok" "" "$domain"
      NGROK_DOMAIN_INPUT="$domain"
      PUBLIC_ENDPOINT_CONFIGURED=1
      ok "ngrok public endpoint configured."
      return 0
      ;;
    cloudflare)
      if [[ "$PUBLIC_PROVIDER_AVAILABLE" -ne 1 ]]; then
        persist_public_endpoint_config "none" "" ""
        warn "cloudflared is unavailable. Local only will remain active."
        return 0
      fi
      port="$($PYTHON_BIN - "$STATE_DIR/settings.json" <<'PYPORT'
import json, sys
from pathlib import Path
p=Path(sys.argv[1])
try:
    d=json.loads(p.read_text())
    print(int(d.get("server",{}).get("port",8000)))
except Exception:
    print(8000)
PYPORT
)"
      info "Cloudflare setup (remotely-managed tunnel):"
      info "1. In Cloudflare Dashboard, go to Networking > Tunnels and create/select a tunnel."
      info "2. In the tunnel Routes tab, Add route > Published application."
      info "3. Choose your subdomain/domain and set Service URL to http://localhost:$port."
      info "4. Copy the tunnel token from the generated cloudflared command (or Add a replica). Do not run Cloudflare's service-install command; Mac MCP manages cloudflared itself."
      info "Official guide: https://developers.cloudflare.com/tunnel/get-started/"

      if ! ask_text "Public hostname (for example https://mac.example.com; blank = configure later):" "MAC_MCP_INSTALL_PUBLIC_URL"; then
        TEXT_REPLY=""
      fi
      url="$TEXT_REPLY"
      if [[ -z "$url" ]]; then
        persist_public_endpoint_config "none" "" ""
        warn "Cloudflare configuration deferred. Local only will remain active; finish it later in Settings > Advanced."
        return 0
      fi
      if ! ask_yes_no "Paste the Cloudflare tunnel token securely now?" "yes" "MAC_MCP_INSTALL_CLOUDFLARE_TOKEN_NOW"; then
        persist_public_endpoint_config "none" "" ""
        warn "Cloudflare token was not saved. Local only will remain active; paste it later in Settings > Advanced."
        return 0
      fi
      if ! read_secret "Cloudflare tunnel token (input hidden):"; then
        persist_public_endpoint_config "none" "" ""
        warn "No interactive secret input was available. Local only will remain active."
        return 0
      fi
      if [[ -z "$SECRET_REPLY" ]]; then
        persist_public_endpoint_config "none" "" ""
        warn "Empty Cloudflare token. Local only will remain active."
        return 0
      fi
      if ! printf '%s\n' "$SECRET_REPLY" | "$CLI_PATH" credential cloudflare save >/dev/null; then
        SECRET_REPLY=""
        persist_public_endpoint_config "none" "" ""
        warn "Cloudflare credential could not be saved. Local only will remain active."
        return 0
      fi
      SECRET_REPLY=""
      if ! persist_public_endpoint_config "cloudflare" "$url" ""; then
        persist_public_endpoint_config "none" "" ""
        warn "Invalid Cloudflare public hostname. Credential was saved, but Local only remains active until the URL is corrected in Settings."
        return 0
      fi
      PUBLIC_ENDPOINT_URL="$url"
      PUBLIC_ENDPOINT_CONFIGURED=1
      ok "Cloudflare Tunnel configured. Mac MCP will run it with --token-file under a KeepAlive user LaunchAgent."
      return 0
      ;;
  esac
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
  printf '  Verified release:   %s (v%s)\n' "$VERIFIED_RELEASE_ID" "$VERIFIED_RELEASE_VERSION"
  printf '  Local MCP endpoint: http://127.0.0.1:8000/mcp\n'
  printf '  Dashboard:          mac-mcp dashboard (authenticated local launch)\n'

  printf '\n%sAuthentication%s\n' "$C_BOLD" "$C_RESET"
  printf '  API key: %s\n' "$API_KEY"
  printf '  Preferred client auth: Authorization: Bearer <API_KEY>\n'
  printf '  Header-limited clients: http://127.0.0.1:8000/mcp?ApiKey=<API_KEY>\n'
  printf '  The key is stored locally in: %s/mcp_server/.env\n' "$RUNTIME_DIR"

  printf '\n'
  info "Public endpoint selection is part of this installer and can be changed later in Mac MCP Settings."
  info "Cloudflare users can paste the tunnel token once during install or later in Settings > Advanced; it is stored in an owner-only credential file and used via --token-file."
  info "CLI users can use --public-mode ngrok/cloudflare/custom/none; the legacy --ngrok flag remains supported."
  info "macOS may ask for Accessibility, Screen Recording, Automation, or Microphone permissions when you first use features that need them."

  printf '\n%sSafari Visual Companion%s\n' "$C_BOLD" "$C_RESET"
  printf '  The Safari extension is bundled inside Mac MCP.app.\n'
  if /usr/bin/codesign -dv --verbose=2 "$APP_PATH" 2>&1 | /usr/bin/grep -q '^Signature=adhoc$'; then
    printf '  This source install is ad-hoc signed, so Safari will not register the bundled extension as a normal installed extension.\n'
    printf '  In Mac MCP.app choose "Developer Setup…", then in Safari enable Develop → Allow Unsigned Extensions and use Develop → Add Temporary Extension….\n'
    printf '  Select: %s/menu_app/BrowserVisualCompanion\n' "$RUNTIME_DIR"
  else
    printf '  Open the Mac MCP menu and choose "Enable in Safari…" once, then allow website access in Safari.\n'
  fi
  printf '\n%sChrome Background Companion%s\n' "$C_BOLD" "$C_RESET"
  printf '  For true non-focus-stealing background tabs, open chrome://extensions, enable Developer mode, choose Load unpacked, and select:\n'
  printf '  %s/menu_app/ChromeVisualCompanion\n' "$RUNTIME_DIR"
  printf '  The companion also provides Chrome DOM/page execution and background-safe visual capture; no Apple Events JavaScript toggle is required while it is connected.\n'
  printf '  If the companion is unavailable, background tab creation fails closed instead of bringing Chrome to the front.\n'
  printf '  The page overlay is activity feedback only; it is not a security or trust indicator.\n'

  printf '\n%sSubagents%s\n' "$C_BOLD" "$C_RESET"
  printf '  OpenCode and Codex are not installed by Mac MCP.\n'
  printf '  If you plan to use Subagents, installing OpenCode and/or Codex separately is recommended.\n'
  if [[ "$CHATGPT_PROVIDER_ENABLED" -eq 1 ]]; then
    printf '  Optional web provider: enabled locally.\n'
  else
    printf '  Optional web provider: disabled locally.\n'
  fi

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
  choose_public_endpoint_mode
  install_selected_public_provider
  handle_optional_helpers
  check_install_targets
  handle_optional_chatgpt_cli
  clone_source_and_runtime
  configure_runtime
  configure_subagent_provider_settings
  install_python_environment
  prepare_chrome_companion
  install_menu_app
  install_update_state_and_cli
  configure_public_endpoint
  optionally_start_server
  print_completion
}

if [[ "${MAC_MCP_INSTALLER_LIBRARY_ONLY:-0}" != "1" ]]; then
  main "$@"
fi
