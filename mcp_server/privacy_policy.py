"""The owner's observation rules (#18, #19), read live from settings.json -> privacy.

- paused: Mac MCP stops looking at and working on this Mac. Page and window reads,
  screenshots, the clipboard, voice and new agents are refused until it is resumed.
- private_mode: work continues, but nothing about it is kept: no usage rows, no
  memories or lessons, no run evidence.
- excluded_sites / excluded_apps: never read, captured or acted in, whatever a caller
  asks. A site matches itself and its subdomains; an app matches its name or bundle id.
- blocked_categories: the owner may also turn whole recognized categories (password
  managers, finance, health) into exclusions. Off by default: recognition can miss.

Every rule is enforced where the capture happens on the server, so no tool argument
can bypass it. Each tool result that read a site or an app carries privacy_source: what
was read, its recognized category and the rule that allowed it, so the owner can see why.
"""
from __future__ import annotations

import time
import unicodedata
from contextvars import ContextVar
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlsplit

from fastapi import HTTPException, status

from .runtime_settings import privacy_setting

_TTL_S = 1.0
_cache: Dict[str, Any] = {"at": -1.0, "value": None}

# Tools refused while observation is paused. Writing a file or running a command is
# not observing the screen, so those keep working; anything that reads what is on
# screen, in a page or on the clipboard, listens, or starts an agent stops.
PAUSED_TOOLS = frozenset({
    "mac_observe", "mac_act", "mac_snapshot", "mac_app", "screenshot", "clipboard_get",
    "browser_observe", "browser_find", "browser_act", "browser_do", "browser_list_tabs",
    "browser_get_html", "browser_get_snapshot", "browser_screenshot", "browser_execute_js",
    "browser_click_selector", "browser_type_selector", "browser_wait_for_selector", "browser_scroll",
    "browser_press_key", "browser_coordinate_click", "browser_open_url", "browser_upload_artifact",
    "browser_sign_in", "computer_plan", "ask_user_voice", "spawn_agent", "spawn_agents",
})
# Tools that keep content Mac MCP would otherwise store; refused in private mode.
PRIVATE_MODE_TOOLS = frozenset({"memory_add", "memory_update", "lesson_record", "lesson_feedback", "lesson_consolidate"})

_SENSITIVE_APPS = {
    "password_manager": ("1password", "bitwarden", "lastpass", "dashlane", "keeper", "passwords",
                         "keychain access", "com.agilebits", "com.bitwarden", "com.apple.passwords",
                         "com.apple.keychainaccess"),
    "finance": ("banking", "bank"),
    "health": ("health", "mychart"),
}
_SENSITIVE_SITES = {
    "password_manager": ("1password.com", "bitwarden.com", "lastpass.com", "dashlane.com", "keepersecurity.com"),
    "finance": ("bank", "paypal.com", "wise.com", "revolut.com", "stripe.com/dashboard", "coinbase.com"),
    "health": ("mychart", "patient", "health", "hospital", "clinic", "e-nabiz"),
}


CATEGORIES = ("password_manager", "finance", "health")

# What the current tool call read, filled by the checks below and attached by main._log.
# A dict is shared, not copied, into worker threads, so reads made there are seen too.
_SOURCE: ContextVar[Optional[Dict[str, Any]]] = ContextVar("mac_mcp_privacy_source", default=None)


def begin_source() -> Any:
    return _SOURCE.set({})


def end_source(token: Any) -> Optional[Dict[str, Any]]:
    found = _SOURCE.get()
    _SOURCE.reset(token)
    return found or None


def _note(kind: str, value: str, category: Optional[str]) -> None:
    found = _SOURCE.get()
    if found is None or not value:
        return
    found[kind] = value
    found["category"] = category
    found["rule"] = "allowed: not excluded" + (f"; {category} is labeled only" if category else "")


class PrivacyBlocked(HTTPException):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(status.HTTP_403_FORBIDDEN, f"{code}: {message}")
        self.code = code


def _strings(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if isinstance(item, str) and str(item).strip()][:500]


def policy(*, fresh: bool = False) -> Dict[str, Any]:
    now = time.monotonic()
    cached = _cache.get("value")
    if not fresh and cached is not None and now - float(_cache["at"]) < _TTL_S:
        return cached
    value = {
        "paused": privacy_setting("observation_paused", False) is True,
        "private_mode": privacy_setting("private_mode", False) is True,
        "excluded_sites": [site for site in (_site_key(item) for item in _strings(privacy_setting("excluded_sites", [])))
                           if site],
        "excluded_apps": [key for key in (_app_key(item) for item in _strings(privacy_setting("excluded_apps", []))) if key],
        "blocked_categories": [item for item in _strings(privacy_setting("blocked_categories", [])) if item in CATEGORIES],
    }
    _cache.update({"at": now, "value": value})
    return value


def _site_key(value: str) -> str:
    text = value.strip().lower()
    if "://" not in text:
        text = "https://" + text
    try:
        host = urlsplit(text).hostname or ""
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _host(url: str) -> str:
    try:
        return (urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""


def excluded_site(url: str) -> Optional[str]:
    host = _host(url)
    if not host:
        return None
    for site in policy()["excluded_sites"]:
        if host == site or host.endswith("." + site):
            return site
    return None


def _app_key(name: Any) -> str:
    """App names as macOS reports them can carry invisible marks (WhatsApp is '\u200eWhatsApp')."""
    text = "".join(char for char in str(name or "") if unicodedata.category(char) != "Cf")
    return " ".join(text.split()).lower()


def excluded_app(*names: Optional[str]) -> Optional[str]:
    wanted = {_app_key(name) for name in names if name and _app_key(name)}
    if not wanted:
        return None
    for app in policy()["excluded_apps"]:
        if app in wanted:
            return app
    return None


def check_tool(tool: str) -> None:
    """The gate every tool call passes (main._log)."""
    current = policy()
    if current["paused"] and tool in PAUSED_TOOLS:
        raise PrivacyBlocked(
            "observation_paused",
            "the owner paused Mac MCP's observation of this Mac (Settings > Privacy). Nothing on screen, in a page, "
            "on the clipboard or from the microphone is read until they resume it; do not retry or work around it.",
        )
    if current["private_mode"] and tool in PRIVATE_MODE_TOOLS:
        raise PrivacyBlocked(
            "private_mode",
            "private mode is on (Settings > Privacy): Mac MCP keeps nothing about this work, so memories and "
            "lessons are not saved.",
        )


def check_site(url: str) -> None:
    if policy()["paused"]:
        check_tool("browser_observe")
    site = excluded_site(url)
    if site:
        raise PrivacyBlocked(
            "privacy_excluded_site",
            f"the owner excluded {site} from Mac MCP (Settings > Privacy): its pages are never read, captured or "
            "acted in. Ask the owner to do this step themselves.",
        )
    category = site_category(url)
    if category and category in policy()["blocked_categories"]:
        raise PrivacyBlocked(
            "privacy_sensitive_site",
            f"{_host(url)} looks like a {category.replace('_', ' ')} site, and the owner chose never to observe "
            "those (Settings > Privacy). Ask the owner to do this step themselves.",
        )
    _note("site", _host(url), category)


def check_app(*names: Optional[str]) -> None:
    if policy()["paused"]:
        check_tool("mac_observe")
    app = excluded_app(*names)
    if app:
        raise PrivacyBlocked(
            "privacy_excluded_app",
            f"the owner excluded {app} from Mac MCP (Settings > Privacy): it is never observed, captured or "
            "operated. Ask the owner to do this step themselves.",
        )
    category = app_category(*names)
    shown = next((str(name) for name in names if name), "")
    if category and category in policy()["blocked_categories"]:
        raise PrivacyBlocked(
            "privacy_sensitive_app",
            f"{shown} looks like a {category.replace('_', ' ')} app, and the owner chose never to observe those "
            "(Settings > Privacy). Ask the owner to do this step themselves.",
        )
    _note("app", shown, category)


def check_screen(visible_owners: Iterable[str]) -> None:
    """A whole-screen capture shows every visible window, so any excluded app on screen blocks it."""
    if policy()["paused"]:
        check_tool("screenshot")
    if not policy()["excluded_apps"]:
        return
    app = excluded_app(*visible_owners)
    if app:
        raise PrivacyBlocked(
            "privacy_excluded_app",
            f"{app} is on screen and the owner excluded it from Mac MCP (Settings > Privacy), so the screen is "
            "not captured. Capture another app's window instead, or ask the owner to hide it.",
        )


def site_category(url: str) -> Optional[str]:
    """Advisory label for a page; never a block on its own."""
    host = _host(url)
    full = str(url or "").lower()
    for category, needles in _SENSITIVE_SITES.items():
        if any(needle in host or ("/" in needle and needle in full) for needle in needles):
            return category
    return None


def app_category(*names: Optional[str]) -> Optional[str]:
    """Advisory label for an app; never a block on its own."""
    text = " ".join(str(name).lower() for name in names if name)
    if not text:
        return None
    for category, needles in _SENSITIVE_APPS.items():
        if any(needle in text for needle in needles):
            return category
    return None


_APP_KEYS = ("app", "app_name", "application", "owner", "process", "process_name", "bundle_id", "name")
_CONTENT_KEYS = ("title", "window_title", "document", "path", "url", "text", "value", "selected_text")


def scrub(value: Any) -> Any:
    """Hide titles, addresses and text of excluded apps and sites inside a listing (mac_snapshot).
    The app or tab stays listed, marked privacy_excluded, so a caller knows why it shows nothing."""
    current = policy()
    if not current["excluded_apps"] and not current["excluded_sites"]:
        return value
    if isinstance(value, list):
        return [scrub(item) for item in value]
    if not isinstance(value, dict):
        return value
    names = [str(value.get(key)) for key in _APP_KEYS if isinstance(value.get(key), str)]
    hit = excluded_app(*names) or (excluded_site(str(value.get("url") or "")) if value.get("url") else None)
    out = {key: scrub(item) for key, item in value.items()}
    if hit:
        for key in _CONTENT_KEYS:
            if key in out and key not in _APP_KEYS:
                out[key] = "[excluded by privacy settings]" if key != "url" else ""
        out["privacy_excluded"] = True
    return out


def keeps_records() -> bool:
    """False in private mode: usage rows, run evidence and other records about the work are skipped."""
    return not policy()["private_mode"]
