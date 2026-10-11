"""Saved logins that agents can use without ever seeing the password (#163).

Apple does not let third-party apps read iCloud Passwords, so Mac MCP keeps its
own small vault: each password is a generic-password item in the owner's login
keychain, written and read only through /usr/bin/security. Items created by that
tool can be read back by it without a macOS prompt, and the password is passed
on stdin, never in a process argument. Sites and usernames (no secrets) live in
an owner-only index next to the other Mac MCP state.

The owner adding a login for a site is the authorization: a later sign-in on
that site asks nothing. Passwords are only ever handed to the browser fill path;
nothing here returns one to a caller.
"""
from __future__ import annotations

import csv
import json
import os
import re
import secrets
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import urlsplit

from .private_storage import ensure_private_dir

SERVICE = "com.bulutarkan.mac-mcp.login"
SECURITY = "/usr/bin/security"
MAX_LOGINS = 500
_ID_RE = re.compile(r"^lv_[0-9a-f]{12}$")
_HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$")
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


class VaultError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _index_path() -> Path:
    state = Path(os.getenv("MAC_MCP_STATE_DIR", str(Path.home() / ".mac-mcp"))).expanduser()
    return ensure_private_dir(state / "vault") / "logins.json"


def _service() -> str:
    # Tests point this at their own service so they never touch real entries.
    return os.getenv("MAC_MCP_LOGIN_VAULT_SERVICE", SERVICE).strip() or SERVICE


def normalize_site(value: str) -> str:
    """Host a login belongs to: from a URL or a bare host, lower-case, without www."""
    raw = str(value or "").strip()
    if not raw:
        raise VaultError("SITE_REQUIRED", "A site (URL or host name) is required.")
    host = urlsplit(raw if "://" in raw else f"https://{raw}").hostname or ""
    host = host.strip(".").lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in _LOOPBACK and not _HOST_RE.fullmatch(host):
        raise VaultError("SITE_INVALID", f"Not a valid site: {raw[:80]}")
    return host


def site_matches(saved: str, host: str) -> bool:
    """A login saved for example.com is used on example.com and its subdomains, never on look-alikes."""
    tab = str(host or "").strip(".").lower()
    if tab.startswith("www."):
        tab = tab[4:]
    return bool(saved) and (tab == saved or tab.endswith("." + saved))


def _load() -> List[Dict[str, Any]]:
    path = _index_path()
    if not path.exists():
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VaultError("VAULT_INDEX_UNREADABLE", "The saved-logins index is unreadable.") from exc
    return [row for row in rows if isinstance(row, dict) and _ID_RE.fullmatch(str(row.get("id") or ""))] if isinstance(rows, list) else []


def _save(rows: Sequence[Dict[str, Any]]) -> None:
    path = _index_path()
    fd, tmp = tempfile.mkstemp(prefix=".logins-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(list(rows), handle, ensure_ascii=False, indent=2, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _public(row: Dict[str, Any]) -> Dict[str, Any]:
    return {key: row.get(key) for key in ("id", "site", "username", "label", "source", "created_at", "updated_at")}


def _keychain_write(login_id: str, site: str, password: str) -> None:
    proc = subprocess.run(
        [SECURITY, "add-generic-password", "-s", _service(), "-a", login_id,
         "-l", f"Mac MCP login: {site}", "-j", "mac-mcp-vault", "-U", "-w"],
        input=f"{password}\n{password}\n", capture_output=True, text=True, timeout=15, check=False,
    )
    if proc.returncode != 0:
        raise VaultError("KEYCHAIN_WRITE_FAILED", "The password could not be saved in the Keychain.")


def _keychain_delete(login_id: str) -> None:
    subprocess.run([SECURITY, "delete-generic-password", "-s", _service(), "-a", login_id],
                   capture_output=True, text=True, timeout=15, check=False)


def secret_for(login_id: str) -> str:
    """The password for one saved login. Only the browser fill path may call this."""
    if not _ID_RE.fullmatch(str(login_id or "")):
        raise VaultError("LOGIN_ID_INVALID", "Unknown saved login.")
    proc = subprocess.run([SECURITY, "find-generic-password", "-s", _service(), "-a", login_id, "-w"],
                          capture_output=True, text=True, timeout=15, check=False)
    value = proc.stdout[:-1] if proc.stdout.endswith("\n") else proc.stdout
    if proc.returncode != 0 or not value:
        raise VaultError("KEYCHAIN_READ_FAILED", "The saved password could not be read from the Keychain.")
    return value


def _check_password(password: str) -> str:
    value = str(password or "")
    if not value:
        raise VaultError("PASSWORD_REQUIRED", "A password is required.")
    if "\n" in value or "\r" in value or "\x00" in value:
        raise VaultError("PASSWORD_INVALID", "Passwords with line breaks are not supported.")
    return value


def add_login(site: str, username: str, password: str, *, label: Optional[str] = None,
              source: str = "manual") -> Dict[str, Any]:
    host = normalize_site(site)
    user = str(username or "").strip()
    if not user or len(user) > 320:
        raise VaultError("USERNAME_INVALID", "A username or email (up to 320 characters) is required.")
    value = _check_password(password)
    rows = _load()
    now = time.time()
    existing = next((row for row in rows if row.get("site") == host and row.get("username") == user), None)
    if existing is None and len(rows) >= MAX_LOGINS:
        raise VaultError("VAULT_FULL", f"At most {MAX_LOGINS} saved logins are supported.")
    login_id = existing["id"] if existing else "lv_" + secrets.token_hex(6)
    _keychain_write(login_id, host, value)
    if existing:
        existing.update({"updated_at": now, "label": (label or existing.get("label") or user)[:120], "source": source})
        row = existing
    else:
        row = {"id": login_id, "site": host, "username": user, "label": (label or user)[:120],
               "source": source, "created_at": now, "updated_at": now}
        rows.append(row)
    _save(rows)
    return _public(row)


def remove_login(login_id: str) -> bool:
    rows = _load()
    keep = [row for row in rows if row.get("id") != login_id]
    if len(keep) == len(rows):
        return False
    _keychain_delete(login_id)
    _save(keep)
    return True


def list_logins() -> List[Dict[str, Any]]:
    return [_public(row) for row in sorted(_load(), key=lambda row: (row.get("site") or "", row.get("username") or ""))]


def logins_for_host(host: str) -> List[Dict[str, Any]]:
    """Saved logins usable on this page host, most specific site first."""
    rows = [row for row in _load() if site_matches(str(row.get("site") or ""), host)]
    return [_public(row) for row in sorted(rows, key=lambda row: -len(str(row.get("site") or "")))]


# ── Passwords app export (File > Export Passwords) ─────────────────────────────

def _export_rows(path: str) -> List[Dict[str, str]]:
    source = Path(path).expanduser()
    try:
        with source.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            rows = [{str(k or "").strip().lower(): str(v or "") for k, v in row.items()} for row in reader]
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise VaultError("IMPORT_UNREADABLE", "The export file could not be read as CSV.") from exc
    if rows and not {"url", "username", "password"} <= set(rows[0]):
        raise VaultError("IMPORT_FORMAT", "Expected a Passwords export with URL, Username and Password columns.")
    return rows


def preview_import(path: str) -> List[Dict[str, Any]]:
    """What an export contains, without passwords: index, site, username, title."""
    items: List[Dict[str, Any]] = []
    for index, row in enumerate(_export_rows(path)):
        try:
            site = normalize_site(row.get("url", ""))
        except VaultError:
            continue
        if not row.get("username") or not row.get("password"):
            continue
        items.append({"index": index, "site": site, "username": row["username"].strip(),
                      "title": row.get("title", "").strip()[:120]})
    return items


def import_logins(path: str, indexes: Optional[Iterable[int]] = None, *, delete_file: bool = False) -> Dict[str, Any]:
    """Save the chosen rows of a Passwords export (all usable rows when indexes is None)."""
    rows = _export_rows(path)
    wanted = None if indexes is None else {int(i) for i in indexes}
    imported, skipped = 0, 0
    for index, row in enumerate(rows):
        if wanted is not None and index not in wanted:
            continue
        try:
            add_login(row.get("url", ""), row.get("username", ""), row.get("password", ""),
                      label=(row.get("title") or "").strip() or None, source="passwords_import")
            imported += 1
        except VaultError:
            skipped += 1
    deleted = False
    if delete_file and imported:
        try:
            Path(path).expanduser().unlink()
            deleted = True
        except OSError:
            deleted = False
    return {"imported": imported, "skipped": skipped, "file_deleted": deleted}
