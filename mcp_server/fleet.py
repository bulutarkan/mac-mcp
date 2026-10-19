"""Your Macs, side by side (#147): pair Macs once, see their state, run recipes on them.

Every Mac keeps its own address (Cloudflare, ngrok, a custom HTTPS URL; no central server). Mac B
lets another Mac in by showing a one-time invitation (a link and an 8-character code, 2 minutes,
single use, attempts limited like phone pairing). Mac A sends it to B; B's owner sees who asks and a
6-digit check that A shows too, and allows or refuses. Only then does B issue A a credential of its
own: B keeps only its hash, A keeps it in an owner-only file. B's Mac MCP API key is never shared.

What A may do on B is chosen on B: see its status, or also list and run its recipes. A recipe run
from A goes through B's own permission profile and approvals. Either side can end it at any time.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import platform
import secrets
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from . import paths
from .private_storage import PRIVATE_FILE_MODE, ensure_private_dir
from .sqlite_wal import enable_wal

OFFER_PREFIX = "mcfleetpair_"
CREDENTIAL_PREFIX = "mcfleet_"
POLL_PREFIX = "mcfleetpoll_"
OFFER_TTL_S = 120
REQUEST_TTL_S = 600
CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
CODE_LENGTH = 8
MAX_ATTEMPTS_PER_SOURCE = 5
MAX_ATTEMPTS_TOTAL = 200
PERMISSIONS = ("status", "recipes")
STARTED_AT = time.time()


class FleetError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _clean_name(value: object, fallback: str = "Mac") -> str:
    text = " ".join(str(value or "").split())
    return text[:80] or fallback


def _now() -> float:
    return time.time()


# ── This Mac ─────────────────────────────────────────────────────────────────

def _root() -> Path:
    return paths.state_dir() / "fleet"


def _computer_name() -> str:
    try:
        name = subprocess.run(["/usr/sbin/scutil", "--get", "ComputerName"], capture_output=True, text=True,
                              timeout=3).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        name = ""
    return _clean_name(name or platform.node().split(".")[0], "This Mac")


def identity() -> Dict[str, str]:
    """This Mac's stable id and name, made once and kept owner-only."""
    path = ensure_private_dir(_root()) / "device.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if str(data.get("device_id", "")).startswith("mac_"):
            return {"device_id": data["device_id"], "name": _clean_name(data.get("name"), "This Mac")}
    except (OSError, ValueError):
        pass
    data = {"device_id": "mac_" + uuid.uuid4().hex[:16], "name": _computer_name()}
    _write_private(path, json.dumps(data))
    return data


def rename(name: str) -> Dict[str, str]:
    data = {**identity(), "name": _clean_name(name, "This Mac")}
    _write_private(_root() / "device.json", json.dumps(data))
    return data


def _write_private(path: Path, text: str) -> None:
    ensure_private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)


def own_url() -> str:
    """The address other Macs reach this one at: its public endpoint's origin.

    MAC_MCP_FLEET_URL overrides it (a second Mac on this machine for testing, a LAN name).
    """
    override = os.getenv("MAC_MCP_FLEET_URL", "").strip()
    if override:
        return normalize_base_url(override)
    from .public_endpoint import resolve_public_endpoint

    try:
        endpoint = resolve_public_endpoint().endpoint_url or ""
    except Exception:
        endpoint = ""
    return normalize_base_url(endpoint) if endpoint else ""


def normalize_base_url(value: str) -> str:
    """scheme://host[:port] of an address. HTTPS, or plain HTTP only to this machine."""
    text = str(value or "").strip()
    if "://" not in text:
        text = "https://" + text
    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    if parts.scheme not in {"https", "http"} or not host:
        raise FleetError("FLEET_ADDRESS_INVALID", "Use the Mac's HTTPS address (for example https://mac.example.com).")
    if parts.scheme == "http" and not _is_loopback(host):
        raise FleetError("FLEET_HTTPS_REQUIRED", "Another Mac is only reached over HTTPS, so its key never travels in "
                                                  "the clear. Plain http is only for this machine (127.0.0.1).")
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}"


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def invitation_parts(text: str) -> Tuple[str, str]:
    """(base_url, code) from an invitation link `https://host/fleet#pair=<token>`, or `address code`."""
    raw = " ".join(str(text or "").split())
    if "#pair=" in raw:
        address, _, token = raw.partition("#pair=")
        return normalize_base_url(address.replace("/fleet", "", 1) if address.endswith("/fleet") else address), token.strip()
    pieces = raw.split(" ")
    if len(pieces) == 2:
        return normalize_base_url(pieces[0]), pieces[1]
    raise FleetError("FLEET_INVITATION_INVALID", "Paste the invitation link from the other Mac, or its address and "
                                                 "8-character code separated by a space.")


def check_code(server_device_id: str, client_device_id: str, secret: str) -> str:
    """The 6 digits both owners compare before B lets A in."""
    digest = hashlib.sha256(f"{server_device_id}|{client_device_id}|{secret}".encode("utf-8")).hexdigest()
    number = int(digest[:12], 16) % 1_000_000
    return f"{number // 1000:03d} {number % 1000:03d}"


# ── The store ────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS offers (
    code_hash TEXT PRIMARY KEY, manual_hash TEXT NOT NULL, permission TEXT NOT NULL, created_at REAL NOT NULL,
    expires_at REAL NOT NULL, consumed_at REAL, failed_attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS requests (
    request_id TEXT PRIMARY KEY, poll_hash TEXT NOT NULL, client_device_id TEXT NOT NULL, client_name TEXT NOT NULL,
    permission TEXT NOT NULL, check_code TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL,
    expires_at REAL NOT NULL, decided_at REAL, credential TEXT, delivered_at REAL, client_id TEXT
);
CREATE TABLE IF NOT EXISTS clients (
    client_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, name TEXT NOT NULL, permission TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE, created_at REAL NOT NULL, last_seen_at REAL, revoked_at REAL
);
CREATE TABLE IF NOT EXISTS peers (
    peer_id TEXT PRIMARY KEY, device_id TEXT, name TEXT NOT NULL, url TEXT NOT NULL, permission TEXT,
    state TEXT NOT NULL, request_id TEXT, poll_secret TEXT, check_code TEXT, created_at REAL NOT NULL,
    last_checked_at REAL, last_ok_at REAL, last_status TEXT, last_error TEXT
);
"""


class FleetStore:
    def __init__(self, root: Optional[Path] = None) -> None:
        self._root = root
        self._lock = threading.RLock()
        self._failures: Dict[str, int] = {}
        self._ready_for: Optional[Path] = None

    @property
    def root(self) -> Path:
        return self._root or _root()

    def _connect(self) -> sqlite3.Connection:
        root = ensure_private_dir(self.root)
        path = root / "fleet.sqlite3"
        conn = sqlite3.connect(path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        enable_wal(conn)
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            path.chmod(PRIVATE_FILE_MODE)
        except OSError:
            pass
        if self._ready_for != path:
            conn.executescript(_SCHEMA)
            self._ready_for = path
        return conn

    # B: inviting another Mac ---------------------------------------------------
    def issue_offer(self, permission: str, ttl_s: int = OFFER_TTL_S) -> Dict[str, Any]:
        if permission not in PERMISSIONS:
            raise FleetError("FLEET_PERMISSION_INVALID", f"permission is one of {', '.join(PERMISSIONS)}.")
        now = _now()
        token = OFFER_PREFIX + secrets.token_urlsafe(24)
        manual = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        with self._lock, closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            # One invitation at a time: a new one ends the last, so attempt counting stays clear.
            conn.execute("DELETE FROM offers")
            conn.execute("INSERT INTO offers VALUES (?, ?, ?, ?, ?, NULL, 0)",
                         (_hash(token), _hash(manual), permission, now, now + max(15, min(int(ttl_s), OFFER_TTL_S))))
            conn.execute("COMMIT")
            self._failures.clear()
        return {"token": token, "code": manual[:4] + "-" + manual[4:], "permission": permission,
                "created_at": now, "expires_at": now + max(15, min(int(ttl_s), OFFER_TTL_S))}

    def cancel_offer(self) -> None:
        with self._lock, closing(self._connect()) as conn:
            conn.execute("DELETE FROM offers")

    def active_offer(self) -> Optional[Dict[str, Any]]:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT permission, created_at, expires_at FROM offers WHERE consumed_at IS NULL "
                               "AND expires_at > ?", (_now(),)).fetchone()
        return dict(row) if row else None

    def request_access(self, code: str, *, client_device_id: str, client_name: str, server_device_id: str,
                       source: str) -> Dict[str, Any]:
        """A Mac presents an invitation. Returns the pending request it can poll; the offer is used up."""
        text = str(code or "").strip()
        is_token = text.startswith(OFFER_PREFIX)
        manual = "".join(ch for ch in text.upper() if ch.isalnum())
        if not is_token and len(manual) != CODE_LENGTH:
            raise FleetError("FLEET_INVITATION_INVALID", "That invitation is not valid or has expired.", 401)
        if not str(client_device_id or "").startswith("mac_") or len(client_device_id) > 40:
            raise FleetError("FLEET_DEVICE_INVALID", "The asking Mac did not say who it is.", 400)
        now = _now()
        with self._lock, closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                column = "code_hash" if is_token else "manual_hash"
                row = conn.execute(f"SELECT * FROM offers WHERE {column} = ?",
                                   (_hash(text if is_token else manual),)).fetchone()
                if row is None:
                    if not is_token:  # a wrong typed code counts against its sender and the window
                        self._failures[source] = self._failures.get(source, 0) + 1
                        conn.execute("UPDATE offers SET failed_attempts = failed_attempts + 1")
                        conn.execute("UPDATE offers SET consumed_at = ? WHERE failed_attempts >= ? AND consumed_at IS NULL",
                                     (now, MAX_ATTEMPTS_TOTAL))
                    conn.execute("COMMIT")
                    raise FleetError("FLEET_INVITATION_INVALID", "That invitation is not valid or has expired.", 401)
                if (row["consumed_at"] is not None or float(row["expires_at"]) <= now
                        or int(row["failed_attempts"]) >= MAX_ATTEMPTS_TOTAL
                        or self._failures.get(source, 0) >= MAX_ATTEMPTS_PER_SOURCE):
                    conn.execute("COMMIT")
                    raise FleetError("FLEET_INVITATION_INVALID", "That invitation is not valid or has expired.", 401)
                used = conn.execute("UPDATE offers SET consumed_at = ? WHERE code_hash = ? AND consumed_at IS NULL",
                                    (now, row["code_hash"]))
                if used.rowcount != 1:
                    conn.execute("COMMIT")
                    raise FleetError("FLEET_INVITATION_INVALID", "That invitation is not valid or has expired.", 401)
                request_id = "fpr_" + uuid.uuid4().hex[:16]
                poll = POLL_PREFIX + secrets.token_urlsafe(32)
                digits = check_code(server_device_id, client_device_id, row["code_hash"])
                conn.execute("INSERT INTO requests (request_id, poll_hash, client_device_id, client_name, permission, "
                             "check_code, status, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                             (request_id, _hash(poll), client_device_id, _clean_name(client_name), row["permission"],
                              digits, now, now + REQUEST_TTL_S))
                conn.execute("COMMIT")
            except FleetError:
                raise
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return {"request_id": request_id, "poll_secret": poll, "check_code": digits, "permission": row["permission"],
                "expires_at": now + REQUEST_TTL_S}

    def pending_requests(self) -> List[Dict[str, Any]]:
        now = _now()
        with closing(self._connect()) as conn:
            conn.execute("UPDATE requests SET status = 'expired' WHERE status = 'pending' AND expires_at <= ?", (now,))
            rows = conn.execute("SELECT request_id, client_name, client_device_id, permission, check_code, created_at, "
                                "expires_at FROM requests WHERE status = 'pending' ORDER BY created_at").fetchall()
        return [dict(row) for row in rows]

    def decide(self, request_id: str, *, allow: bool) -> Dict[str, Any]:
        now = _now()
        with self._lock, closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT * FROM requests WHERE request_id = ?", (str(request_id),)).fetchone()
                if row is None or row["status"] != "pending" or float(row["expires_at"]) <= now:
                    conn.execute("COMMIT")
                    raise FleetError("FLEET_REQUEST_GONE", "That request is no longer waiting.", 404)
                if not allow:
                    conn.execute("UPDATE requests SET status = 'denied', decided_at = ? WHERE request_id = ?",
                                 (now, row["request_id"]))
                    conn.execute("COMMIT")
                    return {"request_id": row["request_id"], "status": "denied"}
                token = CREDENTIAL_PREFIX + secrets.token_urlsafe(48)
                client_id = "fcl_" + uuid.uuid4().hex[:16]
                # A Mac paired again replaces its earlier access rather than holding two.
                conn.execute("UPDATE clients SET revoked_at = ? WHERE device_id = ? AND revoked_at IS NULL",
                             (now, row["client_device_id"]))
                conn.execute("INSERT INTO clients (client_id, device_id, name, permission, token_hash, created_at) "
                             "VALUES (?, ?, ?, ?, ?, ?)",
                             (client_id, row["client_device_id"], row["client_name"], row["permission"], _hash(token), now))
                # The credential waits, once, for the asking Mac's next poll.
                conn.execute("UPDATE requests SET status = 'allowed', decided_at = ?, credential = ?, client_id = ? "
                             "WHERE request_id = ?", (now, token, client_id, row["request_id"]))
                conn.execute("COMMIT")
            except FleetError:
                raise
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return {"request_id": row["request_id"], "status": "allowed", "client_id": client_id}

    def poll_request(self, request_id: str, poll_secret: str) -> Dict[str, Any]:
        now = _now()
        with self._lock, closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM requests WHERE request_id = ?", (str(request_id),)).fetchone()
            if row is None or not secrets.compare_digest(row["poll_hash"], _hash(str(poll_secret or ""))):
                conn.execute("COMMIT")
                raise FleetError("FLEET_REQUEST_GONE", "No such request.", 404)
            status = row["status"]
            if status == "pending" and float(row["expires_at"]) <= now:
                status = "expired"
                conn.execute("UPDATE requests SET status = 'expired' WHERE request_id = ?", (row["request_id"],))
            out: Dict[str, Any] = {"status": status, "permission": row["permission"]}
            if status == "allowed" and row["credential"]:
                out["credential"] = row["credential"]
                conn.execute("UPDATE requests SET credential = NULL, delivered_at = ? WHERE request_id = ?",
                             (now, row["request_id"]))
            conn.execute("COMMIT")
        return out

    def client_for(self, token: Optional[str]) -> Optional[Dict[str, Any]]:
        text = str(token or "")
        if not text.startswith(CREDENTIAL_PREFIX):
            return None
        now = _now()
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM clients WHERE token_hash = ? AND revoked_at IS NULL", (_hash(text),)).fetchone()
            if row is None:
                return None
            if not row["last_seen_at"] or now - float(row["last_seen_at"]) > 5:
                conn.execute("UPDATE clients SET last_seen_at = ? WHERE client_id = ?", (now, row["client_id"]))
        return dict(row)

    def clients(self) -> List[Dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT client_id, device_id, name, permission, created_at, last_seen_at FROM clients "
                                "WHERE revoked_at IS NULL ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def revoke_client(self, client_id: str) -> bool:
        with self._lock, closing(self._connect()) as conn:
            done = conn.execute("UPDATE clients SET revoked_at = ? WHERE client_id = ? AND revoked_at IS NULL",
                                (_now(), str(client_id)))
        return done.rowcount == 1

    # A: the Macs this one sees -------------------------------------------------
    def _credential_path(self, peer_id: str) -> Path:
        if not peer_id.startswith("fpe_") or not peer_id[4:].isalnum():
            raise FleetError("FLEET_PEER_UNKNOWN", "No such Mac.", 404)
        return self.root / "peers" / f"{peer_id}.key"

    def add_pending_peer(self, *, url: str, name: str, device_id: str, request_id: str, poll_secret: str,
                         check_code_value: str, permission: str) -> Dict[str, Any]:
        peer_id = "fpe_" + uuid.uuid4().hex[:16]
        with self._lock, closing(self._connect()) as conn:
            conn.execute("INSERT INTO peers (peer_id, device_id, name, url, permission, state, request_id, poll_secret, "
                         "check_code, created_at) VALUES (?, ?, ?, ?, ?, 'waiting', ?, ?, ?, ?)",
                         (peer_id, device_id, _clean_name(name), url, permission, request_id, poll_secret,
                          check_code_value, _now()))
        return self.peer(peer_id)

    def peer(self, peer_id: str) -> Dict[str, Any]:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM peers WHERE peer_id = ?", (str(peer_id),)).fetchone()
        if row is None:
            raise FleetError("FLEET_PEER_UNKNOWN", "No such Mac.", 404)
        return dict(row)

    def peers(self) -> List[Dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM peers ORDER BY created_at").fetchall()
        return [dict(row) for row in rows]

    def update_peer(self, peer_id: str, **fields: Any) -> None:
        allowed = {"device_id", "name", "permission", "state", "request_id", "poll_secret", "check_code",
                   "last_checked_at", "last_ok_at", "last_status", "last_error", "url"}
        sets = {key: value for key, value in fields.items() if key in allowed}
        if not sets:
            return
        with self._lock, closing(self._connect()) as conn:
            conn.execute(f"UPDATE peers SET {', '.join(f'{key} = ?' for key in sets)} WHERE peer_id = ?",
                         (*sets.values(), peer_id))

    def save_credential(self, peer_id: str, credential: str) -> None:
        _write_private(self._credential_path(peer_id), credential)

    def credential(self, peer_id: str) -> str:
        try:
            return self._credential_path(peer_id).read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""

    def remove_peer(self, peer_id: str) -> None:
        self._credential_path(peer_id).unlink(missing_ok=True)
        with self._lock, closing(self._connect()) as conn:
            conn.execute("DELETE FROM peers WHERE peer_id = ?", (peer_id,))


_STORE = FleetStore()


def store() -> FleetStore:
    return _STORE


def public_peer(row: Dict[str, Any]) -> Dict[str, Any]:
    """A peer as the owner and agents see it: never its credential or poll secret."""
    status: Dict[str, Any] = {}
    if row.get("last_status"):
        try:
            status = json.loads(row["last_status"])
        except ValueError:
            status = {}
    return {
        "peer_id": row["peer_id"], "name": row["name"], "url": row["url"], "device_id": row.get("device_id"),
        "state": row["state"], "permission": row.get("permission"),
        "check_code": row.get("check_code") if row["state"] == "waiting" else None,
        "reachable": row["state"] == "paired" and bool(row.get("last_ok_at"))
        and float(row.get("last_ok_at") or 0) >= float(row.get("last_checked_at") or 0),
        "last_checked_at": row.get("last_checked_at"), "last_ok_at": row.get("last_ok_at"),
        "last_error": row.get("last_error"), "status": status,
    }


def own_status(permission: str) -> Dict[str, Any]:
    """What a paired Mac sees about this one: cheap facts, nothing private."""
    from .install_kind import install_kind, installed_release_id
    from .version import __version__

    me = identity()
    return {"device_id": me["device_id"], "name": me["name"], "version": __version__,
            "release_id": installed_release_id() if install_kind() == "pkg" else None,
            "macos": platform.mac_ver()[0] or None, "uptime_s": int(_now() - STARTED_AT),
            "your_permission": permission}
