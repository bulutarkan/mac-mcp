from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status

from .security import Settings, resolve_path
from .policy import current_policy_context
from .policy_scope import path_is_within
from .scoped_fs import ScopedFilesystemError, scope_needs_path_guard, scoped_search_text


def _current_path_scope():
    scope = current_policy_context().scope
    return scope if scope_needs_path_guard(scope) else None


def _scoped_search_error(exc: ScopedFilesystemError) -> HTTPException:
    code = status.HTTP_400_BAD_REQUEST if exc.reason == "invalid_search_pattern" else status.HTTP_403_FORBIDDEN
    return HTTPException(code, {"error": exc.code, "reason": exc.reason, "message": str(exc)})


SEARCH_PAGE_CHARS = 50_000
SPOTLIGHT_MAX_RESULTS = 500


def _page(lines: List[str], offset: int) -> tuple[str, int, bool]:
    """Whole lines from offset on, up to SEARCH_PAGE_CHARS: (text, lines returned, more follow)."""
    out: List[str] = []
    chars = 0
    for line in lines[offset:]:
        rendered = line + "\n"
        if chars + len(rendered) > SEARCH_PAGE_CHARS:
            if out:
                break
            rendered = rendered[:SEARCH_PAGE_CHARS - 1] + "\n"  # one very long line still makes progress
        out.append(rendered)
        chars += len(rendered)
    return "".join(out), len(out), offset + len(out) < len(lines)


def _search_answer(text: str, returned: int, total: int, offset: int, more: bool) -> Dict[str, Any]:
    answer: Dict[str, Any] = {
        "ok": True,
        "match_count": returned,  # lines in this page; total_matches counts them all (#252)
        "total_matches": total,
        "offset": offset,
        "results": text,
        "truncated": more,
        "has_more": more,
    }
    if more:
        answer["next_offset"] = offset + returned
        answer["note"] = (f"Showing matches {offset + 1}-{offset + returned} of {total}. Call again with "
                          f"offset={offset + returned} for more, or narrow the pattern or path.")
    return answer


def search_files(settings: Settings, pattern: str, path: str = str(Path.home()),
                 include_extensions: Optional[List[str]] = None,
                 case_sensitive: bool = False, offset: int = 0) -> Dict[str, Any]:
    """Search file contents using grep (recursive), a page of matching lines at a time."""
    root = resolve_path(path)
    offset = max(0, int(offset or 0))
    scope = _current_path_scope()
    if scope is not None:
        try:
            stdout, total, more = scoped_search_text(
                scope, root, pattern, include_extensions=include_extensions, case_sensitive=case_sensitive,
                max_chars=SEARCH_PAGE_CHARS, offset=offset,
            )
        except ScopedFilesystemError as exc:
            raise _scoped_search_error(exc) from exc
        return _search_answer(stdout, stdout.count("\n"), total, offset, more)

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
        lines = proc.stdout.split("\n")
        if lines and not lines[-1]:
            lines.pop()
        text, returned, more = _page(lines, offset)
        return _search_answer(text, returned, len(lines), offset, more)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Search timed out after 30s"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def spotlight_search(settings: Settings, query: str, max_results: int = 50, offset: int = 0) -> Dict[str, Any]:
    """Search files using macOS Spotlight (mdfind) — much faster for filenames."""
    max_results = max(1, min(int(max_results or 50), SPOTLIGHT_MAX_RESULTS))
    offset = max(0, int(offset or 0))
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
        total = len(results)
        results = results[offset:offset + max_results]
        answer: Dict[str, Any] = {"ok": True, "count": len(results), "results": results, "total": total,
                                  "offset": offset, "has_more": offset + len(results) < total}
        if answer["has_more"]:
            answer["next_offset"] = offset + len(results)
        return answer
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Spotlight search timed out"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
