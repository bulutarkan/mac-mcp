"""Questions to the owner that the menu app shows in its own window (ask_user).

The menu app keeps /dashboard/events open. A question is registered here, the app is
nudged with a live event, reads the question from /dashboard/api/owner-prompts and
claims it once it is on screen: the notch island when that is the chosen live-activity
style, otherwise a card at the top right. The owner answers there, with text and any
files they add; the app copies those files into the question's upload folder first.

If no menu app is listening, or none claims the question within a few seconds, the
caller falls back to the plain macOS dialog, so a question is never lost.
"""
from __future__ import annotations

import base64
import io
import mimetypes
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .observability import publish_live_event
from .paths import state_dir
from .tool_cancellation import cancellation_checkpoint

CLAIM_TIMEOUT_S = 4.0
MAX_SUGGESTIONS = 4
MAX_SUGGESTION_LENGTH = 60
MAX_ANSWER_LENGTH = 20_000
MAX_ATTACHMENTS = 6
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
# Pictures go back to the agent as base64; a long edge of 1600 px keeps a screenshot
# readable without filling the client's context.
INLINE_IMAGE_EDGE = 1600
INLINE_IMAGE_MAX_BYTES = 3 * 1024 * 1024
MAX_INLINE_IMAGES = 4
UPLOAD_RETENTION_S = 7 * 24 * 3600

_LOCK = threading.Lock()
_MENU_APP_STREAMS = 0


@dataclass
class _Prompt:
    id: str
    payload: Dict[str, Any]
    created_at: float
    expires_at: float
    upload_dir: Optional[Path]
    claimed: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    answer: Optional[Dict[str, Any]] = None


_PROMPTS: Dict[str, _Prompt] = {}


def uploads_root() -> Path:
    return state_dir() / "uploads" / "ask-user"


def menu_app_stream_opened() -> None:
    global _MENU_APP_STREAMS
    with _LOCK:
        _MENU_APP_STREAMS += 1


def menu_app_stream_closed() -> None:
    global _MENU_APP_STREAMS
    with _LOCK:
        _MENU_APP_STREAMS = max(0, _MENU_APP_STREAMS - 1)


def menu_app_listening() -> bool:
    with _LOCK:
        return _MENU_APP_STREAMS > 0


def clean_suggestions(suggestions: Any) -> List[str]:
    """Up to four short, distinct one-line answers the owner can pick with one click."""
    if not isinstance(suggestions, list):
        return []
    cleaned: List[str] = []
    for item in suggestions:
        if not isinstance(item, str):
            continue
        text = " ".join(item.split())[:MAX_SUGGESTION_LENGTH]
        if text and text.casefold() not in {existing.casefold() for existing in cleaned}:
            cleaned.append(text)
        if len(cleaned) == MAX_SUGGESTIONS:
            break
    return cleaned


def pending() -> List[Dict[str, Any]]:
    """Open questions, oldest first, as the menu app shows them."""
    now = time.time()
    with _LOCK:
        rows = [prompt for prompt in _PROMPTS.values() if prompt.expires_at > now and not prompt.done.is_set()]
    rows.sort(key=lambda prompt: prompt.created_at)
    return [{
        **prompt.payload,
        "prompt_id": prompt.id,
        "created_at": prompt.created_at,
        "expires_at": prompt.expires_at,
        "upload_dir": str(prompt.upload_dir) if prompt.upload_dir else None,
        "claimed": prompt.claimed.is_set(),
    } for prompt in rows]


def claim(prompt_id: str) -> bool:
    with _LOCK:
        prompt = _PROMPTS.get(str(prompt_id or ""))
    if prompt is None or prompt.done.is_set():
        return False
    prompt.claimed.set()
    return True


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.relative_to(folder)
        return True
    except ValueError:
        return False


def submit(prompt_id: str, action: str, text: Any = None, attachments: Any = None) -> Dict[str, Any]:
    """The owner's answer from the menu app: send (text and/or files) or skip."""
    with _LOCK:
        prompt = _PROMPTS.get(str(prompt_id or ""))
    if prompt is None or prompt.done.is_set():
        return {"ok": False, "error": "prompt_not_found"}
    if action not in {"send", "skip"}:
        return {"ok": False, "error": "invalid_action"}
    if action == "skip":
        prompt.answer = {"action": "skip"}
        prompt.claimed.set()
        prompt.done.set()
        return {"ok": True}

    answer_text = text if isinstance(text, str) else ""
    if len(answer_text) > MAX_ANSWER_LENGTH:
        return {"ok": False, "error": "answer_too_long"}
    files: List[Path] = []
    raw_files = attachments if isinstance(attachments, list) else []
    if raw_files and prompt.upload_dir is None:
        return {"ok": False, "error": "attachments_not_allowed"}
    if len(raw_files) > MAX_ATTACHMENTS:
        return {"ok": False, "error": "too_many_attachments"}
    for item in raw_files:
        raw_path = item.get("path") if isinstance(item, dict) else item
        if not isinstance(raw_path, str) or not raw_path.strip():
            return {"ok": False, "error": "invalid_attachment"}
        # Only files the app copied into this question's own folder; a symlink out of it is refused.
        path = Path(raw_path).expanduser()
        resolved = path.resolve()
        assert prompt.upload_dir is not None
        if path.is_symlink() or not _inside(resolved, prompt.upload_dir.resolve()) or not resolved.is_file():
            return {"ok": False, "error": "invalid_attachment"}
        if resolved.stat().st_size > MAX_ATTACHMENT_BYTES:
            return {"ok": False, "error": "attachment_too_large"}
        files.append(resolved)
    if not answer_text.strip() and not files:
        return {"ok": False, "error": "empty_answer"}
    prompt.answer = {"action": "send", "text": answer_text, "files": files}
    prompt.claimed.set()
    prompt.done.set()
    return {"ok": True}


def _inline_image(path: Path) -> Optional[Dict[str, Any]]:
    """A picture the agent can see: downscaled, as base64 JPEG (PNG when it has transparency)."""
    try:
        from PIL import Image, ImageOps
    except ImportError:  # pragma: no cover - Pillow ships with the server
        return None
    try:
        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source)
            image.thumbnail((INLINE_IMAGE_EDGE, INLINE_IMAGE_EDGE))
            has_alpha = image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info)
            buffer = io.BytesIO()
            if has_alpha:
                image.save(buffer, format="PNG", optimize=True)
                mime = "image/png"
            else:
                image.convert("RGB").save(buffer, format="JPEG", quality=85, optimize=True)
                mime = "image/jpeg"
            width, height = image.size
    except Exception:
        return None
    data = buffer.getvalue()
    if len(data) > INLINE_IMAGE_MAX_BYTES:
        return None
    return {"data_base64": base64.b64encode(data).decode("ascii"), "encoding": "base64",
            "data_mime_type": mime, "width": width, "height": height}


def _describe(files: List[Path]) -> List[Dict[str, Any]]:
    described: List[Dict[str, Any]] = []
    inline = 0
    for path in files:
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        item: Dict[str, Any] = {"name": path.name, "path": str(path), "mime_type": mime,
                                "size_bytes": path.stat().st_size,
                                "kind": "image" if mime.startswith("image/") else "file"}
        if item["kind"] == "image" and inline < MAX_INLINE_IMAGES:
            picture = _inline_image(path)
            if picture is not None:
                item.update(picture)
                inline += 1
        described.append(item)
    return described


def _prune_uploads(now: float) -> None:
    root = uploads_root()
    if not root.is_dir():
        return
    with _LOCK:
        open_ids = set(_PROMPTS)
    for folder in root.iterdir():
        try:
            if folder.is_dir() and folder.name not in open_ids and now - folder.stat().st_mtime > UPLOAD_RETENTION_S:
                shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            continue


def _wait(event: threading.Event, deadline: float) -> bool:
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            return event.is_set()
        if event.wait(timeout=min(0.25, remaining)):
            return True
        cancellation_checkpoint()


def ask(payload: Dict[str, Any], timeout_s: int, *, allow_attachments: bool = True) -> Optional[Dict[str, Any]]:
    """Show one question in the menu app and wait for the owner.

    None means the app could not show it (not running, or did not claim it in time); the
    caller then uses the macOS dialog. Otherwise {"action": "send"|"skip"|"timeout", ...};
    a send carries "text" and "attachments" (each image with its base64).
    """
    if not menu_app_listening():
        return None
    now = time.time()
    prompt_id = "ask_" + uuid.uuid4().hex[:16]
    upload_dir: Optional[Path] = None
    if allow_attachments:
        _prune_uploads(now)
        upload_dir = uploads_root() / prompt_id
        upload_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    prompt = _Prompt(id=prompt_id, payload={**payload, "timeout_s": int(timeout_s)}, created_at=now,
                     expires_at=now + int(timeout_s), upload_dir=upload_dir)
    with _LOCK:
        _PROMPTS[prompt_id] = prompt
    publish_live_event("owner_prompt")
    try:
        if not _wait(prompt.claimed, min(prompt.expires_at, now + CLAIM_TIMEOUT_S)):
            if upload_dir is not None:
                shutil.rmtree(upload_dir, ignore_errors=True)
            return None
        if not _wait(prompt.done, prompt.expires_at) or prompt.answer is None:
            return {"action": "timeout"}
        answer = dict(prompt.answer)
        if answer.get("action") == "send":
            answer["attachments"] = _describe(answer.pop("files", []))
        return answer
    finally:
        with _LOCK:
            _PROMPTS.pop(prompt_id, None)
        prompt.done.set()
        # Files stay only when they were sent; the agent may read them by path.
        sent = prompt.answer is not None and prompt.answer.get("action") == "send"
        if upload_dir is not None and not sent:
            shutil.rmtree(upload_dir, ignore_errors=True)
        elif upload_dir is not None:
            try:
                upload_dir.rmdir()  # only when nothing was added
            except OSError:
                pass
        publish_live_event("owner_prompt_closed")
