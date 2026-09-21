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
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import HTTPException, status
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect


@dataclass
class _Pending:
    event: threading.Event
    response: Optional[Dict[str, Any]] = None


def chrome_companion_token_path() -> Path:
    configured = os.getenv("MAC_MCP_CHROME_BRIDGE_TOKEN_FILE", "").strip()
    if configured:
        return Path(configured).expanduser()
    state_dir = Path(os.getenv("MAC_MCP_STATE_DIR", str(Path.home() / ".mac-mcp"))).expanduser()
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


class ChromeBackgroundBridge:
    """Thread-safe RPC broker for Chrome-only, non-focus-stealing tab creation."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._websocket: Optional[WebSocket] = None
        self._connection_id: Optional[str] = None
        self._pending: Dict[str, _Pending] = {}

    def configured_token(self) -> str:
        configured = str(os.getenv("MAC_MCP_CHROME_BRIDGE_TOKEN", "") or "").strip()
        return configured or ensure_chrome_companion_token()

    def is_connected(self) -> bool:
        with self._lock:
            return self._loop is not None and self._websocket is not None

    def _attach(self, websocket: WebSocket, loop: asyncio.AbstractEventLoop) -> str:
        connection_id = uuid.uuid4().hex
        with self._lock:
            self._loop = loop
            self._websocket = websocket
            self._connection_id = connection_id
        return connection_id

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
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail={
                        "ok": False,
                        "error": str(response.get("error") or "chrome_background_transport_failed"),
                        "retryable": True,
                        "message": str(response.get("message") or "Chrome background browser request failed."),
                    },
                )
            return response
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def request_open_tab(self, url: str, *, timeout_s: float = 6.0) -> Dict[str, Any]:
        return self._request("open_tab", {"url": str(url)}, timeout_s=timeout_s)

    def request_execute_js(self, chrome_tab_id: str | int, js: str, *, timeout_s: float = 20.0) -> str:
        response = self._request(
            "execute_js",
            {"chrome_tab_id": int(chrome_tab_id), "js": str(js)},
            timeout_s=timeout_s,
        )
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


chrome_background_bridge = ChromeBackgroundBridge()


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
    connection_id = chrome_background_bridge._attach(websocket, loop)
    await websocket.send_json({"type": "hello_ack"})
    try:
        while True:
            message = await websocket.receive_json()
            if isinstance(message, dict):
                if message.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
                else:
                    chrome_background_bridge.handle_message(message)
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
