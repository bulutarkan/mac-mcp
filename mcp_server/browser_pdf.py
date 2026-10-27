"""Save a browser tab as a PDF artifact in the background (#137).

Nothing comes to the front and no print dialog opens:

* Google Chrome with a companion that has ``print_pdf``: Chrome's own print engine
  (DevTools ``Page.printToPDF``) renders the tab, paginated, as Chrome would print it.
* Safari, and Chrome without that companion: the tab's current page (its DOM, with the
  styles and images it shows and without its scripts) is handed to a small WebKit helper
  (page_pdf.swift) that lays it out off screen and writes one tall PDF page. The helper has
  no window and never becomes the active app; it fetches the page's images and styles again
  without cookies, so an image only a signed-in session can load may be missing.

The file goes under the state directory (never over an existing file) and is registered as a
SHA-256 artifact, so browser_upload_artifact or artifact_pipeline can use it next.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from . import paths, swift_build
from .artifact_pipeline import register_artifact
from .chrome_background_bridge import chrome_background_bridge

_SOURCE = Path(__file__).resolve().with_name("page_pdf.swift")
_HELPER_LOCK = threading.Lock()
_CHUNK_CHARS = 20_000
_MAX_SNAPSHOT_CHARS = 4_000_000
_HELPER_TIMEOUT_S = 45


def pdf_dir() -> Path:
    directory = paths.state_dir() / "artifacts" / "pdf"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    return directory


def _target_path(title: str) -> Path:
    stem = re.sub(r"[^\w.-]+", "-", str(title or "page"), flags=re.UNICODE).strip("-.")[:60] or "page"
    return pdf_dir() / f"{stem}-{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(3).hex()}.pdf"


def _helper() -> Path:
    directory = swift_build.cache_dir("MAC_MCP_PAGE_PDF_CACHE", str(paths.state_dir() / "cache" / "page-pdf"))
    with _HELPER_LOCK:
        return swift_build.compile_cached(_SOURCE, directory, "page-pdf")


def _finish(path: Path, method: str, page: Dict[str, Any], **extra: Any) -> Dict[str, Any]:
    artifact = register_artifact(path, source="browser_pdf")
    return {
        "ok": True, "type": "pdf", "method": method, "path": artifact["path"], "artifact_id": artifact["artifact_id"],
        "sha256": artifact["sha256"], "size": artifact["size"], "url": page.get("url"), "title": page.get("title"),
        "background": True, **extra,
    }


def chrome_print_pdf(native_id: str, page: Dict[str, Any]) -> Dict[str, Any]:
    response = chrome_background_bridge.request_print_pdf(native_id, timeout_s=60.0)
    data = base64.b64decode(str(response.get("data") or ""), validate=True)
    if not data.startswith(b"%PDF"):
        return {"ok": False, "type": "pdf", "error": "pdf_failed", "message": "Chrome did not return a PDF."}
    path = _target_path(page.get("title") or "")
    with open(path, "xb") as handle:
        handle.write(data)
    return _finish(path, "chrome_print", page)


def snapshot_pdf(run_js: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
    """PDF of the page from its DOM; run_js(options) runs browser_js/page_snapshot.js in the tab."""
    js_calls = 0
    start = run_js({"phase": "start", "max_chars": _MAX_SNAPSHOT_CHARS})
    js_calls += 1
    if not start.get("ok"):
        return {"ok": False, "type": "pdf", "error": "pdf_page_too_large" if start.get("error") == "page_too_large" else "pdf_failed",
                "message": "The page is too large to save this way." if start.get("error") == "page_too_large"
                else str(start.get("error") or "Could not read the page."), "_js_calls": js_calls}
    token, length = str(start.get("token") or ""), int(start.get("length") or 0)
    parts = []
    try:
        for offset in range(0, length, _CHUNK_CHARS):
            chunk = run_js({"phase": "chunk", "token": token, "offset": offset, "size": _CHUNK_CHARS})
            js_calls += 1
            if not chunk.get("ok"):
                return {"ok": False, "type": "pdf", "error": "pdf_failed",
                        "message": "The page changed or reloaded while it was being read.", "_js_calls": js_calls}
            parts.append(str(chunk.get("text") or ""))
    finally:
        try:
            run_js({"phase": "clear"})
            js_calls += 1
        except Exception:
            pass
    html = "".join(parts)
    path = _target_path(start.get("title") or "")
    request = {"html": html, "base_url": start.get("url"), "out": str(path),
               "width": float(start.get("width") or 1280), "timeout_s": _HELPER_TIMEOUT_S - 5}
    try:
        helper = _helper()
        proc = subprocess.run([str(helper)], input=json.dumps(request), capture_output=True, text=True,
                              timeout=_HELPER_TIMEOUT_S, check=False)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        return {"ok": False, "type": "pdf", "error": "pdf_unavailable",
                "message": f"The PDF helper could not run: {type(exc).__name__}", "_js_calls": js_calls}
    answer: Dict[str, Any] = {}
    for line in reversed((proc.stdout or "").splitlines()):
        try:
            answer = json.loads(line)
            break
        except ValueError:
            continue
    if not answer.get("ok") or not path.exists():
        return {"ok": False, "type": "pdf", "error": "pdf_failed",
                "message": str(answer.get("message") or answer.get("error") or "The PDF helper failed."), "_js_calls": js_calls}
    out = _finish(path, "webkit_snapshot", start,
                  note="One tall page from the page as shown, without its scripts; images that need the "
                       "site's sign-in may be missing.")
    out["_js_calls"] = js_calls
    return out
