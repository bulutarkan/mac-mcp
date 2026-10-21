"""Named workspaces: save which apps, web pages and Finder folders belong together, and open them again (#69).

save reads what is open now (apps with a window, Safari/Chrome tabs, the Finder folder and selection) or
takes the lists the person gives, keeps only the parts named in include, and stores the profile in
Mac MCP's own state, readable by the owner only. Apps and sites the owner excluded in Privacy are never
saved; Mac MCP itself is left out, and Finder is kept as folders.

restore opens what is missing without taking focus: apps start in the background (open -g), pages
open as background tabs, folders as Finder windows behind the current one; apps already running are
left as they are, so no window moves. With a desktop (a Mission Control space id, saved or given) it
opens onto that desktop instead. Apps that are not installed and pages that are refused are reported.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from fastapi import HTTPException, status

from . import paths, privacy_policy
from .private_storage import ensure_private_dir

PARTS = ("apps", "urls", "finder")
MAX_WORKSPACES = 50
MAX_ITEMS = 40
_NAME = re.compile(r"^[^\x00-\x1f/]{1,60}$")
_LEFT_OUT = {"com.bulutarkan.mac-mcp.menu", "com.apple.finder"}
_BROWSERS = {"safari": "Safari", "com.apple.safari": "Safari", "chrome": "Google Chrome",
             "google chrome": "Google Chrome", "com.google.chrome": "Google Chrome"}
_BROWSER_BUNDLES = {"Safari": "com.apple.Safari", "Google Chrome": "com.google.Chrome"}


def _store() -> Path:
    return ensure_private_dir(paths.state_dir()) / "workspaces.json"


def _load() -> Dict[str, Any]:
    try:
        data = json.loads(_store().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_all(data: Dict[str, Any]) -> None:
    target = _store()
    temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, target)


def _name(value: Optional[str]) -> str:
    name = " ".join(str(value or "").split())
    if not _NAME.fullmatch(name):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "name is a short workspace name, such as Research.")
    return name


def _find(data: Dict[str, Any], name: str) -> Optional[str]:
    return next((key for key in data if key.casefold() == name.casefold()), None)


def _osascript(script: str, timeout_s: float = 8.0) -> str:
    proc = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True,
                          timeout=timeout_s, check=False)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "osascript failed").strip()[:300])
    return proc.stdout.strip()


def running_apps() -> List[Dict[str, str]]:
    """Apps with a Dock presence, as name and bundle id (a bundle id reopens the same app)."""
    raw = _osascript('''
set rs to ASCII character 30
set us to ASCII character 31
set out to ""
tell application "System Events"
    repeat with p in (every application process whose background only is false)
        try
            set out to out & (name of p) & us & (bundle identifier of p) & rs
        end try
    end repeat
end tell
return out''')
    rows = []
    for line in raw.split(chr(30)):
        name, _, bundle = line.partition(chr(31))
        if name.strip() and bundle.strip() and bundle.strip() != "missing value":
            rows.append({"name": name.strip(), "bundle_id": bundle.strip()})
    return rows


def _current_tabs(settings: Any) -> List[Dict[str, str]]:
    from .tools_snapshot import _read_browser_tabs

    try:
        tabs = _read_browser_tabs(settings, browser_tab_limit=MAX_ITEMS).get("tabs") or []
    except RuntimeError:
        return []
    return [{"browser": _BROWSERS.get(str(tab.get("browser") or "").lower(), str(tab.get("browser") or "Safari")),
             "url": str(tab["url"])} for tab in tabs if str(tab.get("url") or "").startswith(("http://", "https://"))]


def _current_finder(settings: Any) -> List[str]:
    from .tools_snapshot import _read_selected_context

    try:
        context = _read_selected_context(settings, selected_file_limit=MAX_ITEMS)
    except (RuntimeError, subprocess.SubprocessError):
        return []
    folders = [context["folder"]] if context.get("folder") else []
    return folders + list(context.get("selected_paths") or [])


def _app_rows(apps: Iterable[Any]) -> List[Dict[str, str]]:
    rows = []
    for app in apps:
        if isinstance(app, dict):
            row = {"name": str(app.get("name") or app.get("bundle_id") or "").strip(),
                   "bundle_id": str(app.get("bundle_id") or "").strip()}
        else:
            text = str(app or "").strip()
            row = {"name": text, "bundle_id": text if re.fullmatch(r"[\w-]+(\.[\w-]+)+", text) else ""}
        if row["name"]:
            rows.append(row)
    return rows


def _url_rows(urls: Iterable[Any]) -> List[Dict[str, str]]:
    rows = []
    for item in urls:
        url = str(item.get("url") if isinstance(item, dict) else item or "").strip()
        browser = str(item.get("browser") or "") if isinstance(item, dict) else ""
        if not url.startswith(("http://", "https://")):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Only http(s) addresses are saved: {url[:80]}")
        rows.append({"browser": _BROWSERS.get(browser.lower(), "Safari"), "url": url})
    return rows


def save(settings: Any, name: str, *, include: Optional[List[str]] = None, apps: Optional[List[Any]] = None,
         urls: Optional[List[Any]] = None, finder_paths: Optional[List[str]] = None,
         exclude_apps: Optional[List[str]] = None, desktop: Optional[int] = None,
         now: Optional[float] = None) -> Dict[str, Any]:
    name = _name(name)
    parts = list(dict.fromkeys(str(part).strip().lower() for part in (include or PARTS)))
    unknown = [part for part in parts if part not in PARTS]
    if unknown or not parts:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "include lists apps, urls and/or finder.")
    skipped: List[str] = []
    profile: Dict[str, Any] = {"name": name, "saved_at": time.time() if now is None else now, "include": parts}
    if "apps" in parts:
        rows = _app_rows(apps) if apps is not None else running_apps()
        leave = {str(item).strip().casefold() for item in exclude_apps or []}
        kept = []
        for row in rows:
            if row["bundle_id"].lower() in _LEFT_OUT or row["name"].casefold() in leave or row["bundle_id"].casefold() in leave:
                continue
            if privacy_policy.excluded_app(row["name"], row["bundle_id"]):
                skipped.append(f"app excluded in Privacy: {row['name']}")
                continue
            if row not in kept:
                kept.append(row)
        profile["apps"] = kept[:MAX_ITEMS]
    if "urls" in parts:
        kept = []
        for row in _url_rows(urls) if urls is not None else _current_tabs(settings):
            if privacy_policy.excluded_site(row["url"]):
                skipped.append("a page on a site excluded in Privacy")
                continue
            if row not in kept:
                kept.append(row)
        profile["urls"] = kept[:MAX_ITEMS]
    if "finder" in parts:
        chosen = finder_paths if finder_paths is not None else _current_finder(settings)
        profile["finder"] = list(dict.fromkeys(str(Path(str(p)).expanduser()) for p in chosen if str(p).strip()))[:MAX_ITEMS]
    if desktop is not None:
        profile["desktop"] = int(desktop)
    data = _load()
    existing = _find(data, name)
    if existing is None and len(data) >= MAX_WORKSPACES:
        raise HTTPException(status.HTTP_409_CONFLICT, f"At most {MAX_WORKSPACES} workspaces are kept; delete one first.")
    if existing is not None:
        del data[existing]
    data[name] = profile
    _save_all(data)
    return {"ok": True, "action": "replaced" if existing else "saved", "workspace": profile, "skipped": skipped,
            "stored_in": str(_store())}


def list_workspaces() -> Dict[str, Any]:
    rows = []
    for name, profile in sorted(_load().items(), key=lambda item: item[0].casefold()):
        rows.append({"name": name, "saved_at": profile.get("saved_at"), "apps": len(profile.get("apps") or []),
                     "urls": len(profile.get("urls") or []), "finder": len(profile.get("finder") or []),
                     "desktop": profile.get("desktop")})
    return {"ok": True, "count": len(rows), "workspaces": rows}


def inspect(name: str) -> Dict[str, Any]:
    data = _load()
    key = _find(data, _name(name))
    if key is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No workspace has that name; action=list shows them.")
    return {"ok": True, "workspace": data[key]}


def delete(name: str) -> Dict[str, Any]:
    data = _load()
    key = _find(data, _name(name))
    if key is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No workspace has that name.")
    del data[key]
    _save_all(data)
    return {"ok": True, "deleted": key}


def _open(args: List[str]) -> Optional[str]:
    """Runs /usr/bin/open; returns its error, or None."""
    try:
        proc = subprocess.run(["/usr/bin/open", *args], capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return type(exc).__name__
    return None if proc.returncode == 0 else (proc.stderr or "could not open").strip()[:200]


def restore(settings: Any, name: str, *, desktop: Optional[int] = None,
            open_tab: Optional[Callable[[str, str], Dict[str, Any]]] = None,
            on_desktop: Optional[Callable[[int, str, List[str]], Dict[str, Any]]] = None) -> Dict[str, Any]:
    profile = inspect(name)["workspace"]
    space = desktop if desktop is not None else profile.get("desktop")
    if on_desktop is None:
        from .desktops import open_on_desktop

        on_desktop = open_on_desktop
    if open_tab is None:
        from .tools_browser import browser_open_url

        def open_tab(browser: str, url: str) -> Dict[str, Any]:
            return browser_open_url(settings, browser=browser, url=url, new_tab=True, background=True)

    opened: List[str] = []
    already: List[str] = []
    missing: List[Dict[str, str]] = []
    try:
        running = {row["bundle_id"].lower() for row in running_apps()}
    except (RuntimeError, subprocess.SubprocessError):
        running = set()

    def launch(app: Dict[str, str], targets: List[str]) -> Optional[str]:
        if space is not None:
            result = on_desktop(int(space), app["bundle_id"] or app["name"], targets)
            return None if result.get("ok") else str(result.get("error") or "could not open on that desktop")
        selector = ["-b", app["bundle_id"]] if app["bundle_id"] else ["-a", app["name"]]
        return _open(["-g", *selector, *targets])

    for app in profile.get("apps") or []:
        if privacy_policy.excluded_app(app["name"], app["bundle_id"]):
            missing.append({"item": app["name"], "reason": "excluded in Privacy"})
            continue
        if app["bundle_id"].lower() in running and space is None:
            already.append(app["name"])
            continue
        error = launch(app, [])
        if error:
            missing.append({"item": app["name"], "reason": "not installed" if "Unable to find" in error else error})
        else:
            opened.append(app["name"])
            running.add(app["bundle_id"].lower())

    by_browser: Dict[str, List[str]] = {}
    for row in profile.get("urls") or []:
        if privacy_policy.excluded_site(row["url"]):
            missing.append({"item": row["url"], "reason": "site excluded in Privacy"})
            continue
        by_browser.setdefault(row["browser"], []).append(row["url"])
    for browser, urls in by_browser.items():
        bundle = _BROWSER_BUNDLES.get(browser, "com.apple.Safari")
        if space is not None or bundle.lower() not in running:
            # Not running yet, or going onto another desktop: the browser starts with these pages, in the
            # background. Chrome opens one address per window on another desktop.
            groups = [[url] for url in urls] if space is not None and browser == "Google Chrome" else [urls]
            for group in groups:
                error = launch({"name": browser, "bundle_id": bundle}, group)
                if error:
                    missing.extend({"item": url, "reason": error} for url in group)
                else:
                    opened.extend(group)
            running.add(bundle.lower())
            continue
        for url in urls:
            try:
                result = open_tab(browser, url)
            except HTTPException as exc:
                result = {"ok": False, "error": str(exc.detail)[:200]}
            if result.get("ok"):
                opened.append(url)
            else:
                missing.append({"item": url, "reason": str(result.get("error") or "not opened")})

    for item in profile.get("finder") or []:
        if not os.path.exists(item):
            missing.append({"item": item, "reason": "no longer there"})
            continue
        # A folder opens as a Finder window; a file is shown selected in its folder, not opened.
        folder = os.path.isdir(item)
        if space is not None:
            result = on_desktop(int(space), "com.apple.finder", [item if folder else os.path.dirname(item)])
            error = None if result.get("ok") else str(result.get("error") or "could not open on that desktop")
        else:
            error = _open(["-g", item] if folder else ["-g", "-R", item])
        if error:
            missing.append({"item": item, "reason": error})
        else:
            opened.append(item)

    return {"ok": not missing, "workspace": profile["name"], "opened": opened, "already_open": already,
            "missing": missing, "desktop": space,
            "note": "Opened in the background; apps that were already running were left as they are."}
