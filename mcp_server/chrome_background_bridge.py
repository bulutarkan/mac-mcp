from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import secrets
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from fastapi import HTTPException, status
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import paths


@dataclass
class _Pending:
    event: threading.Event
    response: Optional[Dict[str, Any]] = None


def chrome_companion_token_path() -> Path:
    configured = os.getenv("MAC_MCP_CHROME_BRIDGE_TOKEN_FILE", "").strip()
    if configured:
        return Path(configured).expanduser()
    state_dir = paths.state_dir()
    return state_dir / "chrome-companion-token"


def ensure_chrome_companion_token(path: Optional[Path] = None) -> str:
    token_file = (path or chrome_companion_token_path()).expanduser()
    token_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(token_file.parent, 0o700)
    except OSError:
        pass
    if token_file.is_symlink():
        raise RuntimeError("Chrome companion token path must not be a symlink")
    if token_file.exists():
        stat = token_file.stat()
        if hasattr(os, "getuid") and stat.st_uid != os.getuid():
            raise RuntimeError("Chrome companion token file is not owned by the current user")
        token = token_file.read_text(encoding="utf-8").strip()
        if len(token) >= 32:
            os.chmod(token_file, 0o600)
            return token

    token = secrets.token_urlsafe(48)
    fd, tmp_name = tempfile.mkstemp(prefix=".chrome-companion-token-", dir=str(token_file.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, token_file)
        os.chmod(token_file, 0o600)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
    return token


def _existing_chrome_companion_port(config_path: Optional[Path]) -> Optional[int]:
    if config_path is None or not config_path.is_file() or config_path.is_symlink():
        return None
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r'"port"\s*:\s*(\d+)', text)
    if not match:
        return None
    try:
        port = int(match.group(1))
    except ValueError:
        return None
    return port if 1 <= port <= 65535 else None


def _resolve_chrome_companion_port(existing_config: Optional[Path] = None) -> int:
    configured = str(os.getenv("MAC_MCP_PORT", "") or "").strip()
    if configured:
        try:
            port = int(configured)
            if 1 <= port <= 65535:
                return port
        except ValueError:
            pass

    args = list(sys.argv[1:])
    for index, arg in enumerate(args):
        value = ""
        if arg == "--port" and index + 1 < len(args):
            value = args[index + 1]
        elif arg.startswith("--port="):
            value = arg.split("=", 1)[1]
        if value:
            try:
                port = int(value)
                if 1 <= port <= 65535:
                    return port
            except ValueError:
                pass

    existing_port = _existing_chrome_companion_port(existing_config)
    if existing_port is not None:
        return existing_port
    return 8000


def ensure_chrome_companion_config(runtime_root: Optional[Path] = None) -> Optional[Path]:
    runtime = (runtime_root or Path(__file__).resolve().parent.parent).resolve()
    extension_dir = runtime / "menu_app" / "ChromeVisualCompanion"
    if not extension_dir.is_dir():
        return None
    path = extension_dir / "bridge_config.js"
    token = str(os.getenv("MAC_MCP_CHROME_BRIDGE_TOKEN", "") or "").strip() or ensure_chrome_companion_token()
    port = _resolve_chrome_companion_port(path)
    payload = (
        "globalThis.MAC_MCP_CHROME_BRIDGE = "
        + json.dumps({"port": port, "token": token, "reconnect_ms": 1000}, separators=(",", ":"))
        + ";\n"
    )
    fd, tmp_name = tempfile.mkstemp(prefix=".bridge-config-", dir=str(extension_dir), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
        os.chmod(path, 0o600)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
    return path


# What the companion in this source reports in its hello (background.js FEATURES). A loaded
# companion missing any of them predates this server and needs one reload, even when its
# version number matches (same-version checkpoints add features too).
EXPECTED_COMPANION_FEATURES = frozenset({
    "dialogs", "gestures", "alarm_reconnect", "tab_queue", "dialog_memory", "dialog_session_hold", "keys", "frames",
    "open_window", "open_tab_window", "print_pdf", "tab_inventory",
})


_WAKE_RETRY_S = 60.0


class ChromeBackgroundBridge:
    """Thread-safe RPC broker for Chrome-only, non-focus-stealing tab creation."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._websocket: Optional[WebSocket] = None
        self._connection_id: Optional[str] = None
        self._pending: Dict[str, _Pending] = {}
        self._features: frozenset = frozenset()
        self._version: Optional[str] = None
        self._connected_at: Optional[float] = None
        self._last_seen_at: Optional[float] = None
        # Asks an open Chrome tab to wake the companion's worker (set by tools_browser; AppleScript).
        self._waker: Optional[Callable[[], bool]] = None
        self._wake_failed_at = 0.0

    def configured_token(self) -> str:
        configured = str(os.getenv("MAC_MCP_CHROME_BRIDGE_TOKEN", "") or "").strip()
        return configured or ensure_chrome_companion_token()

    def is_connected(self) -> bool:
        with self._lock:
            return self._loop is not None and self._websocket is not None

    def set_waker(self, waker: Optional[Callable[[], bool]]) -> None:
        self._waker = waker

    def wake(self, timeout_s: float = 4.0) -> bool:
        """Chrome stops an idle companion worker and its alarm does not always bring it back: ask an
        open tab to wake it, then wait briefly for it to connect. True when it is connected."""
        if self.is_connected():
            return True
        waker = self._waker
        # A companion that is not installed never answers: try again only after a quiet minute.
        if waker is None or time.monotonic() - self._wake_failed_at < _WAKE_RETRY_S:
            return False
        try:
            asked = bool(waker())
        except Exception:
            asked = False
        deadline = time.monotonic() + (max(0.0, float(timeout_s)) if asked else 0.0)
        while time.monotonic() < deadline:
            if self.is_connected():
                return True
            time.sleep(0.1)
        if self.is_connected():
            return True
        self._wake_failed_at = time.monotonic()
        return False

    def _attach(self, websocket: WebSocket, loop: asyncio.AbstractEventLoop, features: Any = None,
                version: Any = None) -> str:
        connection_id = uuid.uuid4().hex
        names = features if isinstance(features, list) else []
        now = time.time()
        with self._lock:
            self._loop = loop
            self._websocket = websocket
            self._connection_id = connection_id
            self._features = frozenset(str(name)[:40] for name in names[:20])
            # The extension's own manifest version; shown to the owner, so plain and short.
            self._version = re.sub(r"[^0-9A-Za-z.\-]", "", version)[:20] or None if isinstance(version, str) else None
            self._connected_at = now
            self._last_seen_at = now
        return connection_id

    def touch(self, connection_id: str) -> None:
        with self._lock:
            if self._connection_id == connection_id:
                self._last_seen_at = time.time()

    def status(self) -> Dict[str, Any]:
        """What the Settings checklist shows: connected, which companion, and whether it needs a reload."""
        with self._lock:
            connected = self._loop is not None and self._websocket is not None
            missing = sorted(EXPECTED_COMPANION_FEATURES - self._features) if connected else []
            return {
                "connected": connected,
                "version": self._version if connected else None,
                "connected_at": self._connected_at if connected else None,
                "last_seen_at": self._last_seen_at if connected else None,
                "missing_features": missing,
                "needs_reload": bool(missing),
            }

    def supports(self, feature: str) -> bool:
        with self._lock:
            return feature in self._features

    def require(self, feature: str) -> None:
        """Refuse before sending when the loaded companion predates a capability."""
        if self.is_connected() and not self.supports(feature):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "ok": False, "error": "chrome_companion_update_required", "retryable": False,
                    "feature": feature,
                    "message": "The loaded Mac MCP Chrome Companion is older than this server. Reload it once at "
                               "chrome://extensions (Mac MCP Chrome Companion -> reload) and retry.",
                },
            )

    def _detach(self, connection_id: str) -> None:
        with self._lock:
            if self._connection_id != connection_id:
                return
            self._loop = None
            self._websocket = None
            self._connection_id = None
            pending = list(self._pending.values())
            self._pending.clear()
        for item in pending:
            item.response = {"ok": False, "error": "chrome_background_transport_disconnected"}
            item.event.set()

    async def _send(self, websocket: WebSocket, payload: Dict[str, Any]) -> None:
        await websocket.send_json(payload)

    def _request(self, message_type: str, payload: Dict[str, Any], *, timeout_s: float) -> Dict[str, Any]:
        request_id = uuid.uuid4().hex
        pending = _Pending(event=threading.Event())
        if not self.is_connected():
            self.wake()
        with self._lock:
            loop = self._loop
            websocket = self._websocket
            if loop is None or websocket is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "ok": False,
                        "error": "chrome_background_transport_unavailable",
                        "retryable": True,
                        "message": "Chrome Background Companion is not connected. Load or reload it in Chrome and retry.",
                    },
                )
            self._pending[request_id] = pending

        message = {"type": message_type, "request_id": request_id, **payload}
        try:
            future = asyncio.run_coroutine_threadsafe(self._send(websocket, message), loop)
            future.result(timeout=max(1.0, min(float(timeout_s), 3.0)))
            if not pending.event.wait(timeout=max(1.0, float(timeout_s))):
                raise HTTPException(
                    status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                    detail={
                        "ok": False,
                        "error": "chrome_background_transport_timeout",
                        "retryable": True,
                        "message": "Chrome did not acknowledge the background browser request in time.",
                    },
                )
            response = dict(pending.response or {})
            if not response.get("ok"):
                error = str(response.get("error") or "chrome_background_transport_failed")
                detail = {
                    "ok": False,
                    "error": error,
                    "retryable": error != "browser_dialog_open",
                    "message": str(response.get("message") or "Chrome background browser request failed."),
                }
                for key in ("dialog", "steps_done", "frames"):
                    if key in response:
                        detail[key] = response[key]
                if error == "browser_dialog_open":
                    detail["required_action"] = "browser_act with {type: 'dialog', decision: 'accept' or 'dismiss'}"
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT if error == "browser_dialog_open" else status.HTTP_502_BAD_GATEWAY,
                    detail=detail,
                )
            return response
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def request_open_tab(
        self, url: str, *, window_id: Optional[int] = None, timeout_s: float = 6.0,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"url": str(url)}
        if window_id is not None:
            payload["window_id"] = int(window_id)
        return self._request("open_tab", payload, timeout_s=timeout_s)

    def request_open_window(self, url: Optional[str] = None, *, timeout_s: float = 6.0) -> Dict[str, Any]:
        """A new Chrome window that does not take focus (a new tab page when url is empty)."""
        self.require("open_window")
        return self._request("open_window", {"url": str(url or "")}, timeout_s=timeout_s)

    def request_execute_js(
        self, chrome_tab_id: str | int, js: str, *, timeout_s: float = 20.0, frame: Optional[str] = None,
    ) -> str:
        payload: Dict[str, Any] = {"chrome_tab_id": int(chrome_tab_id), "js": str(js)}
        if frame:
            self.require("frames")
            payload["frame"] = str(frame)[:300]
        response = self._request("execute_js", payload, timeout_s=timeout_s)
        return str(response.get("result") or "")

    def request_dispatch_mouse(
        self,
        chrome_tab_id: str | int,
        x: float,
        y: float,
        *,
        click_count: int = 1,
        timeout_s: float = 8.0,
    ) -> Dict[str, Any]:
        return self._request(
            "dispatch_mouse",
            {
                "chrome_tab_id": int(chrome_tab_id),
                "x": float(x),
                "y": float(y),
                "click_count": 2 if int(click_count) == 2 else 1,
            },
            timeout_s=timeout_s,
        )

    def request_handle_dialog(
        self, chrome_tab_id: str | int, accept: bool, prompt_text: Optional[str] = None, *, timeout_s: float = 8.0,
    ) -> Dict[str, Any]:
        self.require("dialogs")
        payload: Dict[str, Any] = {"chrome_tab_id": int(chrome_tab_id), "accept": bool(accept)}
        if prompt_text is not None:
            payload["prompt_text"] = str(prompt_text)[:2000]
        return self._request("handle_dialog", payload, timeout_s=timeout_s)

    def request_gesture(
        self, chrome_tab_id: str | int, steps: list, *, timeout_s: float = 15.0, feature: str = "gestures",
    ) -> Dict[str, Any]:
        self.require(feature)
        return self._request("gesture", {"chrome_tab_id": int(chrome_tab_id), "steps": list(steps)}, timeout_s=timeout_s)

    def request_set_file_input(
        self, chrome_tab_id: str | int, css_selector: str, file_path: str, *, timeout_s: float = 20.0,
    ) -> Dict[str, Any]:
        return self._request(
            "set_file_input",
            {
                "chrome_tab_id": int(chrome_tab_id),
                "css_selector": str(css_selector),
                "file_path": str(file_path),
            },
            timeout_s=timeout_s,
        )

    def request_print_pdf(self, chrome_tab_id: str | int, *, timeout_s: float = 60.0) -> Dict[str, Any]:
        """The tab as Chrome prints it (Page.printToPDF), base64 in data; the tab is never activated."""
        self.require("print_pdf")
        return self._request("print_pdf", {"chrome_tab_id": int(chrome_tab_id)}, timeout_s=timeout_s)

    def handle_message(self, message: Dict[str, Any]) -> None:
        message_type = str(message.get("type") or "")
        if message_type == "pong":
            return
        if message_type != "result":
            return
        request_id = str(message.get("request_id") or "")
        if not request_id:
            return
        with self._lock:
            pending = self._pending.get(request_id)
        if pending is None:
            return
        pending.response = dict(message)
        pending.event.set()



@dataclass
class _ChromeProfileConnection:
    """One authenticated Chrome extension WebSocket; no profile contents are stored."""
    websocket: WebSocket
    loop: asyncio.AbstractEventLoop
    features: frozenset
    version: Optional[str]
    connected_at: float
    last_seen_at: float


class MultiProfileChromeBackgroundBridge(ChromeBackgroundBridge):
    """Keep Chrome profiles independent and route operations only to their owner.

    Chrome's extension API refuses a tab/window ID belonging to another profile.
    Unknown ownership is resolved by trying a request on each peer in turn;
    only an explicit 'not found in this profile' result permits another attempt.
    A successful route is cached and purged on disconnect. All other failures,
    including a timeout or an uncertain mutation, stop without replaying.
    """

    def __init__(self) -> None:
        super().__init__()
        self._clients: Dict[str, _ChromeProfileConnection] = {}
        self._tab_routes: Dict[int, str] = {}
        self._window_routes: Dict[int, str] = {}
        self._pending_routes: Dict[str, str] = {}
        self._inventory_verified = False
        self._retired_tabs: set[int] = set()
        self._retired_windows: set[int] = set()

    def is_connected(self) -> bool:
        with self._lock:
            return bool(self._clients)

    def _attach(self, websocket: WebSocket, loop: asyncio.AbstractEventLoop,
                features: Any = None, version: Any = None) -> str:
        connection_id = uuid.uuid4().hex
        names = features if isinstance(features, list) else []
        cleaned_version = (
            re.sub(r"[^0-9A-Za-z.\-]", "", version)[:20] or None
            if isinstance(version, str) else None
        )
        now = time.time()
        with self._lock:
            self._clients[connection_id] = _ChromeProfileConnection(
                websocket, loop, frozenset(str(name)[:40] for name in names[:20]),
                cleaned_version, now, now,
            )
            # Any new connection changes the verified multi-profile universe.
            self._retired_tabs.update(self._tab_routes)
            self._retired_windows.update(self._window_routes)
            self._inventory_verified = False
            self._tab_routes.clear()
            self._window_routes.clear()
        return connection_id

    def touch(self, connection_id: str) -> None:
        with self._lock:
            peer = self._clients.get(connection_id)
            if peer is not None:
                peer.last_seen_at = time.time()

    def status(self) -> Dict[str, Any]:
        with self._lock:
            peers = list(self._clients.values())
            if not peers:
                return {
                    "connected": False, "version": None, "connected_at": None,
                    "last_seen_at": None, "missing_features": [], "needs_reload": False,
                }
            latest = max(peers, key=lambda peer: peer.connected_at)
            missing = sorted(set().union(
                *(EXPECTED_COMPANION_FEATURES - peer.features for peer in peers)
            ))
            result: Dict[str, Any] = {
                "connected": True, "version": latest.version,
                "connected_at": latest.connected_at,
                "last_seen_at": max(peer.last_seen_at for peer in peers),
                "missing_features": missing, "needs_reload": bool(missing),
            }
            if len(peers) > 1:
                result["connection_count"] = len(peers)
                result["versions"] = sorted({peer.version for peer in peers if peer.version})
            return result

    def supports(self, feature: str) -> bool:
        with self._lock:
            return any(feature in peer.features for peer in self._clients.values())

    def inventory_supported(self) -> bool:
        """An inventory is trustworthy only if every connected profile supports it."""
        with self._lock:
            return bool(self._clients) and all(
                "tab_inventory" in peer.features for peer in self._clients.values()
            )

    def request_tab_inventory(self, *, timeout_s: float = 8.0) -> list[dict]:
        """Return an all-or-nothing, read-only inventory from authenticated profiles."""
        with self._lock:
            clients = dict(self._clients)
        if not clients:
            raise HTTPException(status.HTTP_409_CONFLICT, {
                "ok": False, "error": "chrome_background_transport_unavailable",
                "retryable": True,
            })
        if any("tab_inventory" not in peer.features for peer in clients.values()):
            raise HTTPException(status.HTTP_409_CONFLICT, {
                "ok": False, "error": "chrome_companion_update_required",
                "retryable": False, "feature": "tab_inventory",
            })
        result: list[dict] = []
        seen_tabs: set[int] = set()
        owners_of_windows: dict[int, str] = {}
        for connection_id in clients:
            response = self._send_to_profile(connection_id, "list_tabs", {}, timeout_s)
            if not response.get("ok") or not isinstance(response.get("tabs"), list):
                raise HTTPException(status.HTTP_502_BAD_GATEWAY, {
                    "ok": False, "error": "chrome_tab_inventory_incomplete",
                    "retryable": True,
                })
            if len(response["tabs"]) > 500:
                raise HTTPException(status.HTTP_502_BAD_GATEWAY, {
                    "ok": False, "error": "chrome_tab_inventory_limit",
                    "retryable": False,
                })
            for entry in response["tabs"]:
                if not isinstance(entry, dict):
                    raise HTTPException(status.HTTP_502_BAD_GATEWAY, {
                        "ok": False, "error": "chrome_tab_inventory_invalid",
                        "retryable": False,
                    })
                try:
                    tid = int(entry["chrome_tab_id"])
                    wid = int(entry["chrome_window_id"])
                    idx = int(entry["index"])
                except (ValueError, TypeError, KeyError) as exc:
                    raise HTTPException(status.HTTP_502_BAD_GATEWAY, {
                        "ok": False, "error": "chrome_tab_inventory_invalid",
                        "retryable": False,
                    }) from exc
                if (tid <= 0 or wid <= 0 or idx < 0 or tid in seen_tabs or (
                    wid in owners_of_windows and owners_of_windows[wid] != connection_id
                )):
                    raise HTTPException(status.HTTP_409_CONFLICT, {
                        "ok": False, "error": "chrome_tab_identity_collision",
                        "retryable": False,
                    })
                seen_tabs.add(tid)
                owners_of_windows[wid] = connection_id
                result.append({
                    "native_id": str(tid), "chrome_window_id": wid, "chrome_index": idx,
                    "owner_id": connection_id,
                    "active": entry.get("active") is True,
                    "title": str(entry.get("title") or "")[:512],
                    "url": str(entry.get("url") or "")[:2048],
                })
        with self._lock:
            if set(self._clients) != set(clients):
                raise HTTPException(status.HTTP_409_CONFLICT, {
                    "ok": False, "error": "chrome_profile_disconnected_during_inventory",
                    "retryable": True,
                })
            # Atomic authoritative replacement: do not preserve tabs removed after
            # a prior inventory, and never reuse a stale owner mapping.
            self._tab_routes = {int(row["native_id"]): row["owner_id"] for row in result}
            self._window_routes = dict(owners_of_windows)
            # Explicit read-only inventory refresh creates new, owner-scoped
            # tab handles; only then are previously retired IDs usable again.
            self._retired_tabs.clear()
            self._retired_windows.clear()
            self._inventory_verified = True
        return result

    def _detach(self, connection_id: str) -> None:
        with self._lock:
            if self._clients.pop(connection_id, None) is None:
                return
            # A disconnected profile may have held IDs that are now recycled.
            # Invalidate *all* previous inventory until a fresh all-peer scan.
            self._retired_tabs.update(self._tab_routes)
            self._retired_windows.update(self._window_routes)
            self._inventory_verified = False
            self._tab_routes.clear()
            self._window_routes.clear()
            pending_ids = [
                request_id for request_id, owner in self._pending_routes.items()
                if owner == connection_id
            ]
            pending = [self._pending.pop(request_id, None) for request_id in pending_ids]
            for request_id in pending_ids:
                self._pending_routes.pop(request_id, None)
        for item in pending:
            if item is not None:
                item.response = {
                    "ok": False, "error": "chrome_background_transport_disconnected",
                }
                item.event.set()

    def handle_message(self, message: Dict[str, Any],
                       connection_id: Optional[str] = None) -> None:
        if str(message.get("type") or "") != "result":
            return
        request_id = str(message.get("request_id") or "")
        with self._lock:
            owner = self._pending_routes.get(request_id)
            if not request_id or owner is None or owner != connection_id:
                return
            pending = self._pending.get(request_id)
            if pending is not None:
                pending.response = dict(message)
                pending.event.set()

    def _send_to_profile(self, connection_id: str, message_type: str,
                         payload: Dict[str, Any], timeout_s: float) -> Dict[str, Any]:
        request_id = uuid.uuid4().hex
        pending = _Pending(event=threading.Event())
        with self._lock:
            peer = self._clients.get(connection_id)
            if peer is None:
                raise HTTPException(status.HTTP_409_CONFLICT, {
                    "ok": False, "error": "chrome_background_transport_disconnected",
                    "retryable": True,
                })
            self._pending[request_id] = pending
            self._pending_routes[request_id] = connection_id
        try:
            message = {"type": message_type, "request_id": request_id, **payload}
            future = asyncio.run_coroutine_threadsafe(
                self._send(peer.websocket, message), peer.loop
            )
            future.result(timeout=max(1.0, min(float(timeout_s), 3.0)))
            if not pending.event.wait(timeout=max(1.0, float(timeout_s))):
                raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, {
                    "ok": False, "error": "chrome_background_transport_timeout",
                    "retryable": True,
                    "message": "Chrome did not acknowledge the request.",
                })
            return dict(pending.response or {})
        finally:
            with self._lock:
                self._pending.pop(request_id, None)
                self._pending_routes.pop(request_id, None)

    def _request(self, message_type: str, payload: Dict[str, Any],
                 *, timeout_s: float) -> Dict[str, Any]:
        tab_id = int(payload["chrome_tab_id"]) if "chrome_tab_id" in payload else None
        window_id = int(payload["window_id"]) if "window_id" in payload else None
        with self._lock:
            clients = dict(self._clients)
            cached = (self._tab_routes.get(tab_id) if tab_id is not None
                      else self._window_routes.get(window_id))
        if not clients:
            raise HTTPException(status.HTTP_409_CONFLICT, {
                "ok": False, "error": "chrome_background_transport_unavailable",
                "retryable": True, "message": "No Chrome companion is connected.",
            })
        with self._lock:
            identity_retired = ((tab_id is not None and tab_id in self._retired_tabs)
                                or (window_id is not None and window_id in self._retired_windows))
        if identity_retired:
            raise HTTPException(status.HTTP_409_CONFLICT, {
                "ok": False, "error": "stale_tab_handle", "retryable": False,
                "required_action": "browser_list_tabs",
                "message": "Chrome profile connection changed. Refresh tabs to establish a new owner-scoped handle.",
            })
        if message_type in {"open_tab", "open_window"} and len(clients) > 1 and window_id is None:
            raise HTTPException(status.HTTP_409_CONFLICT, {
                "ok": False, "error": "chrome_profile_selection_required",
                "retryable": False,
                "message": "Specify the target Chrome window when multiple profiles are connected.",
            })
        feature = (
            "open_tab_window" if message_type == "open_tab" and window_id is not None
            else "frames" if message_type == "execute_js" and payload.get("frame")
            else None
        )
        if tab_id is not None or window_id is not None:
            # Never address a tab/window without authenticated ownership,
            # even with just one connected Companion: the AppleScript scan
            # may be reporting a different Chrome profile entirely.
            # Establish complete, collision-free inventory of all clients.
            with self._lock:
                verified = self._inventory_verified and cached in self._clients
            if not verified:
                self.request_tab_inventory(timeout_s=min(timeout_s, 8.0))
                with self._lock:
                    clients = dict(self._clients)
                    cached = (self._tab_routes.get(tab_id) if tab_id is not None
                              else self._window_routes.get(window_id))
            if cached not in clients:
                raise HTTPException(status.HTTP_404_NOT_FOUND, {
                    "ok": False, "error": "chrome_profile_target_not_found",
                    "retryable": False,
                    "message": "Target was not present in a complete authenticated Chrome profile inventory.",
                })
            ordered = [cached]
        else:
            ordered = list(clients)
            if cached in clients:
                ordered.remove(cached)
                ordered.insert(0, cached)
        if feature:
            if cached in clients and feature not in clients[cached].features:
                ordered = []
            else:
                ordered = [cid for cid in ordered if feature in clients[cid].features]
            if not ordered:
                raise HTTPException(status.HTTP_409_CONFLICT, {
                    "ok": False, "error": "chrome_companion_update_required",
                    "retryable": False, "feature": feature,
                    "message": "Reload or update the companion in the target Chrome profile.",
                })

        for connection_id in ordered:
            response = self._send_to_profile(connection_id, message_type, payload, timeout_s)
            if response.get("ok"):
                with self._lock:
                    if connection_id in self._clients:
                        if tab_id is not None:
                            self._tab_routes[tab_id] = connection_id
                        returned_tab = response.get("chrome_tab_id")
                        returned_window = response.get("chrome_window_id")
                        if returned_tab is not None:
                            self._tab_routes[int(returned_tab)] = connection_id
                        if window_id is not None:
                            self._window_routes[window_id] = connection_id
                        if returned_window is not None:
                            self._window_routes[int(returned_window)] = connection_id
                return response
            error = str(response.get("error") or "chrome_background_transport_failed")
            detail_message = str(response.get("message") or "Chrome request failed.")
            # Another profile MUST report an absent tab/window before we try it.
            absent_tab = tab_id is not None and (
                "no tab with id" in detail_message.lower()
                or "no tab with given id" in detail_message.lower()
            )
            absent_window = (
                message_type == "open_tab" and window_id is not None
                and error == "chrome_window_not_found"
            )
            if absent_tab or absent_window:
                with self._lock:
                    if tab_id is not None and self._tab_routes.get(tab_id) == connection_id:
                        self._tab_routes.pop(tab_id, None)
                    if window_id is not None and self._window_routes.get(window_id) == connection_id:
                        self._window_routes.pop(window_id, None)
                if len(clients) > 1:
                    # A stale ID is not permission to retry an operation in a
                    # different account. The caller must refresh tab handles.
                    raise HTTPException(status.HTTP_404_NOT_FOUND, {
                        "ok": False, "error": "chrome_profile_target_not_found",
                        "retryable": False,
                        "message": "The previously verified tab/window is absent; refresh inventory and tab identity.",
                    })
                continue
            detail: Dict[str, Any] = {
                "ok": False, "error": error, "retryable": error != "browser_dialog_open",
                "message": detail_message,
            }
            for key in ("dialog", "steps_done", "frames"):
                if key in response:
                    detail[key] = response[key]
            if error == "browser_dialog_open":
                detail["required_action"] = "browser_act with a dialog accept or dismiss decision"
            raise HTTPException(
                status.HTTP_409_CONFLICT if error == "browser_dialog_open"
                else status.HTTP_502_BAD_GATEWAY, detail
            )

        # A connected profile running an older Companion may own the requested
        # window but cannot answer window-targeted requests. Do not report that
        # the window is absent merely because those peers were filtered out.
        if feature and any(feature not in peer.features for peer in clients.values()):
            raise HTTPException(status.HTTP_409_CONFLICT, {
                "ok": False, "error": "chrome_companion_update_required",
                "retryable": False,
                "feature": feature,
                "message": "The target window may belong to a Chrome profile whose Companion lacks this feature. "
                           "Update or reload Mac MCP Chrome Companion in the target profile; no tab was opened.",
            })
        raise HTTPException(status.HTTP_404_NOT_FOUND, {
            "ok": False, "error": "chrome_profile_target_not_found",
            "retryable": True,
            "message": "This tab or window is not owned by any connected Chrome profile.",
        })


chrome_background_bridge = MultiProfileChromeBackgroundBridge()



def _diag_line(diag: Any) -> str:
    """The companion's recent lifecycle notes as one short log line: when it woke, what it skipped, close codes."""
    parts = []
    for entry in (diag if isinstance(diag, list) else [])[-16:]:
        if not isinstance(entry, dict):
            continue
        stamp = time.strftime("%H:%M:%S", time.localtime(float(entry.get("t") or 0) / 1000.0))
        detail = ",".join(f"{key}={str(value)[:16]}" for key, value in entry.items()
                          if key in {"why", "r", "code", "clean", "accepted", "state"})
        parts.append(f"{stamp} {str(entry.get('e') or '?')[:16]}" + (f"({detail})" if detail else ""))
    return " | ".join(parts)


def _loopback_client(websocket: WebSocket) -> bool:
    host = str(getattr(websocket.client, "host", "") or "")
    return host in {"127.0.0.1", "::1"}


async def chrome_background_bridge_websocket(websocket: WebSocket) -> None:
    origin = str(websocket.headers.get("origin") or "")
    if not _loopback_client(websocket) or not origin.startswith("chrome-extension://"):
        await websocket.close(code=4403)
        return

    await websocket.accept()
    try:
        hello = await asyncio.wait_for(websocket.receive_json(), timeout=3.0)
    except (asyncio.TimeoutError, WebSocketDisconnect):
        await websocket.close(code=4401)
        return
    expected = chrome_background_bridge.configured_token()
    supplied = str(hello.get("token") or "") if isinstance(hello, dict) and hello.get("type") == "hello" else ""
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        await websocket.close(code=4401)
        return

    loop = asyncio.get_running_loop()
    connection_id = chrome_background_bridge._attach(websocket, loop, hello.get("features"), hello.get("version"))
    await websocket.send_json({"type": "hello_ack"})
    try:
        while True:
            message = await websocket.receive_json()
            if isinstance(message, dict):
                chrome_background_bridge.touch(connection_id)
                if message.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
                elif message.get("type") == "diag":
                    line = _diag_line(message.get("notes"))
                    if line:
                        print(f"[chrome companion] connected; recent: {line}", file=sys.stderr, flush=True)
                else:
                    chrome_background_bridge.handle_message(message, connection_id=connection_id)
    except WebSocketDisconnect:
        pass
    finally:
        chrome_background_bridge._detach(connection_id)


def create_chrome_background_bridge_routes() -> list[WebSocketRoute]:
    try:
        ensure_chrome_companion_config()
    except (OSError, RuntimeError):
        # The server and Safari path must remain usable even if this optional
        # Chrome companion cannot prepare its local config file.
        pass
    return [WebSocketRoute("/chrome-background-bridge", chrome_background_bridge_websocket)]
