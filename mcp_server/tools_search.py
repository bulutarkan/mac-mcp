from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status

from .security import Settings, resolve_path, truncate
from .policy import current_policy_context
from .policy_scope import path_is_within
from .scoped_fs import ScopedFilesystemError, scope_needs_path_guard, scoped_search_text


def _current_path_scope():
    scope = current_policy_context().scope
    return scope if scope_needs_path_guard(scope) else None


def _scoped_search_error(exc: ScopedFilesystemError) -> HTTPException:
    code = status.HTTP_400_BAD_REQUEST if exc.reason == "invalid_search_pattern" else status.HTTP_403_FORBIDDEN
    return HTTPException(code, {"error": exc.code, "reason": exc.reason, "message": str(exc)})


def search_files(settings: Settings, pattern: str, path: str = str(Path.home()),
                 include_extensions: Optional[List[str]] = None,
                 case_sensitive: bool = False) -> Dict[str, Any]:
    """Search file contents using grep (recursive)."""
    root = resolve_path(path)
    scope = _current_path_scope()
    if scope is not None:
        try:
            stdout, match_count, truncated = scoped_search_text(
                scope, root, pattern, include_extensions=include_extensions, case_sensitive=case_sensitive, max_chars=50_000,
            )
        except ScopedFilesystemError as exc:
            raise _scoped_search_error(exc) from exc
        return {"ok": True, "match_count": match_count, "results": stdout, "truncated": truncated}

    flags = ["-rn", "--include=*"]
    if not case_sensitive:
        flags.append("-i")

    cmd = ["grep"] + flags
    if include_extensions:
        cmd = ["grep", "-rn"] + ([] if case_sensitive else ["-i"])
        for ext in include_extensions:
            cmd.extend(["--include", f"*.{ext.lstrip('.')}"])

    cmd += [pattern, str(root)]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        stdout, truncated = truncate(proc.stdout, 50_000)
        lines = stdout.splitlines()
        return {
            "ok": True,
            "match_count": len(lines),
            "results": stdout,
            "truncated": truncated,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Search timed out after 30s"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def spotlight_search(settings: Settings, query: str, max_results: int = 50) -> Dict[str, Any]:
    """Search files using macOS Spotlight (mdfind) — much faster for filenames."""
    try:
        proc = subprocess.run(
            ["mdfind", "-name", query],
            capture_output=True, text=True, timeout=10
        )
        results = [r for r in proc.stdout.splitlines() if r.strip()]
        scope = _current_path_scope()
        if scope is not None and scope.path_roots is not None:
            results = [
                candidate for candidate in results
                if any(path_is_within(candidate, root) for root in scope.path_roots)
            ]
        results = results[:max_results]
        return {"ok": True, "count": len(results), "results": results}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Spotlight search timed out"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
