"""What takes room in a folder, and moving chosen items to the Trash (#72).

scan only reads: in the folder the person names (default ~/Downloads) it lists large files, files not
changed for a long time and files with exactly the same content, each with why it is listed. It stops
at a file and time budget and says so. Links, hidden files, app bundles and other packages, and iCloud
placeholders (files not on this Mac) are left out; macOS system folders and ~/Library are refused.

move_to_trash takes only ids from such a report, and only while each file is as the report saw it
(same size, time and file number). It uses macOS's own Trash (/usr/bin/trash), so Finder's Put Back
works, and keeps a receipt. undo moves the files back when Mac MCP may open the Trash (Full Disk
Access); without it the answer names what to Put Back in Finder: Mac MCP never asks Finder to do it,
since moving things out of the Trash makes Finder ask the owner for their password.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status

from . import paths
from .private_storage import ensure_private_dir

SCAN_MAX_FILES = 20_000
SCAN_TIME_S = 15.0
HASH_BUDGET_BYTES = 2 * 1024 ** 3
MIN_DUPLICATE_BYTES = 1024 * 1024
MIN_OLD_BYTES = 1024 * 1024
REPORT_TTL_S = 24 * 3600
MAX_ITEMS = 200
TRASH = "/usr/bin/trash"
_DATALESS = 0x40000000  # SF_DATALESS: an iCloud placeholder whose content is not on this Mac
_PACKAGES = (".app", ".bundle", ".framework", ".plugin", ".kext", ".photoslibrary", ".musiclibrary", ".tvlibrary",
             ".imovielibrary", ".fcpbundle", ".logicx", ".band", ".pages", ".numbers", ".key", ".xcodeproj",
             ".xcworkspace", ".playground", ".rtfd", ".dSYM", ".sparsebundle", ".photosLibrary", ".lrlibrary")


def _root_dir() -> Path:
    return ensure_private_dir(paths.state_dir() / "storage-review")


def _refused(folder: Path) -> Optional[str]:
    home = Path.home()
    if folder == Path("/") or not folder.is_absolute():
        return "Choose a folder, such as ~/Downloads."
    temporary = any(folder == Path(root) or Path(root) in folder.parents for root in ("/private/tmp", "/private/var/folders"))
    for root in ("/System", "/Library", "/usr", "/bin", "/sbin", "/private", "/Applications", "/opt", "/cores"):
        if (folder == Path(root) or Path(root) in folder.parents) and not temporary:
            return "macOS and app folders are not reviewed."
    library = home / "Library"
    if folder == library or library in folder.parents:
        return "~/Library holds app data, not files to clean up by hand, so it is not reviewed."
    return None


def _in_scope(path: Path) -> bool:
    from .policy import current_policy_context
    from .policy_scope import path_is_within

    scope = current_policy_context().scope
    roots = scope.path_roots if scope is not None else None
    return not roots or any(path_is_within(path, root) for root in roots)


def _item_id(path: str, info: os.stat_result) -> str:
    raw = f"{path}\0{info.st_size}\0{info.st_mtime_ns}\0{info.st_ino}"
    return "it_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _sha256(path: str, budget: List[int]) -> Optional[str]:
    size = os.path.getsize(path)
    if budget[0] < size:
        return None
    budget[0] -= size
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="minutes")


def scan(path: Optional[str] = None, *, min_size_mb: Optional[float] = None, older_than_days: Optional[int] = None,
         duplicates: bool = True, limit: int = 50, now: Optional[float] = None) -> Dict[str, Any]:
    folder = Path(path).expanduser() if path else Path.home() / "Downloads"
    try:
        folder = folder.resolve(strict=True)
    except OSError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That folder does not exist.") from None
    if not folder.is_dir():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "path must be a folder.")
    refused = _refused(folder)
    if refused:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, refused)
    if not _in_scope(folder):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "That folder is outside the paths this agent may use.")
    large = int(float(min_size_mb if min_size_mb is not None else 100) * 1024 * 1024)
    days = int(older_than_days if older_than_days is not None else 180)
    if large < 1024 * 1024 or days < 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "min_size_mb must be at least 1 and older_than_days at least 1.")
    limit = max(1, min(int(limit), MAX_ITEMS))
    now = time.time() if now is None else now
    cutoff = now - days * 86400
    started = time.monotonic()
    skipped = {"links": 0, "hidden": 0, "packages": 0, "icloud_placeholders": 0, "unreadable": 0}
    files: List[Dict[str, Any]] = []
    scanned = 0
    truncated = False
    pending = [str(folder)]
    while pending and not truncated:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if scanned >= SCAN_MAX_FILES or time.monotonic() - started > SCAN_TIME_S:
                        truncated = True
                        break
                    if entry.name.startswith("."):
                        skipped["hidden"] += 1
                        continue
                    try:
                        if entry.is_symlink():
                            skipped["links"] += 1
                            continue
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        skipped["unreadable"] += 1
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        if entry.name.lower().endswith(tuple(p.lower() for p in _PACKAGES)):
                            skipped["packages"] += 1
                        else:
                            pending.append(entry.path)
                        continue
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    scanned += 1
                    if getattr(info, "st_flags", 0) & _DATALESS:
                        skipped["icloud_placeholders"] += 1
                        continue
                    files.append({"path": entry.path, "info": info})
        except OSError:
            skipped["unreadable"] += 1
    reasons: Dict[str, List[str]] = {}
    for item in files:
        info = item["info"]
        why = []
        if info.st_size >= large:
            why.append("large")
        if info.st_mtime < cutoff and info.st_size >= MIN_OLD_BYTES:
            why.append("old")
        if why:
            reasons[item["path"]] = why
    groups: List[Dict[str, Any]] = []
    hash_limited = False
    if duplicates:
        by_size: Dict[int, List[Dict[str, Any]]] = {}
        for item in files:
            if item["info"].st_size >= MIN_DUPLICATE_BYTES:
                by_size.setdefault(item["info"].st_size, []).append(item)
        budget = [HASH_BUDGET_BYTES]
        for size in sorted(by_size, reverse=True):
            same = by_size[size]
            if len(same) < 2:
                continue
            by_hash: Dict[str, List[Dict[str, Any]]] = {}
            for item in same:
                digest = _sha256(item["path"], budget)
                if digest is None:
                    hash_limited = True
                    continue
                by_hash.setdefault(digest, []).append(item)
            for digest, twins in by_hash.items():
                if len(twins) < 2:
                    continue
                twins.sort(key=lambda item: item["info"].st_mtime)
                group = f"dup_{digest[:10]}"
                groups.append({"group": group, "copies": len(twins), "size": size,
                               "keep_suggested": twins[0]["path"], "room_freed": size * (len(twins) - 1)})
                for item in twins:
                    item["duplicate_group"] = group
                    item["keep_suggested"] = item is twins[0]
                    if item is not twins[0]:
                        reasons.setdefault(item["path"], []).append("duplicate")
    listed = [item for item in files if item["path"] in reasons]
    listed.sort(key=lambda item: item["info"].st_size, reverse=True)
    shown = listed[:limit]
    scan_id = "scan_" + secrets.token_hex(8)
    rows = []
    stored = []
    for item in shown:
        info = item["info"]
        item_id = _item_id(item["path"], info)
        row = {"id": item_id, "path": item["path"], "name": os.path.basename(item["path"]), "size": info.st_size,
               "modified": _iso(info.st_mtime), "reasons": reasons[item["path"]]}
        if item.get("duplicate_group"):
            row["duplicate_group"] = item["duplicate_group"]
        rows.append(row)
        stored.append({**row, "size_bytes": info.st_size, "mtime_ns": info.st_mtime_ns, "ino": info.st_ino})
    report = {"scan_id": scan_id, "root": str(folder), "created_at": now, "expires_at": now + REPORT_TTL_S, "items": stored}
    target = _root_dir() / f"{scan_id}.json"
    target.write_text(json.dumps(report), encoding="utf-8")
    os.chmod(target, 0o600)
    _prune(now)
    return {
        "ok": True, "scan_id": scan_id, "folder": str(folder), "read_only": True,
        "criteria": {"min_size_mb": round(large / 1024 / 1024, 1), "older_than_days": days, "duplicates": duplicates},
        "scanned_files": scanned, "truncated": truncated, "duplicates_hash_limited": hash_limited,
        "count": len(rows), "total_listed": len(listed), "items": rows,
        "bytes_listed": sum(item["info"].st_size for item in listed),
        "duplicate_groups": groups[:50], "skipped": skipped,
        "next": "Show the owner what is listed. Only ids they choose may go to move_to_trash with this scan_id "
                "(the report is kept for 24 hours).",
    }


def _prune(now: float) -> None:
    for path in _root_dir().glob("scan_*.json"):
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("expires_at", 0) < now:
                path.unlink()
        except (OSError, ValueError):
            continue


def _report(scan_id: str, now: float) -> Dict[str, Any]:
    if not re.fullmatch(r"scan_[0-9a-f]{16}", str(scan_id or "")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "scan_id is the id a scan returned.")
    path = _root_dir() / f"{scan_id}.json"
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That scan is gone; scan again.") from None
    if report.get("expires_at", 0) < now:
        raise HTTPException(status.HTTP_410_GONE, "That scan is older than 24 hours; scan again.")
    return report


def move_to_trash(scan_id: str, item_ids: List[str], *, now: Optional[float] = None) -> Dict[str, Any]:
    now = time.time() if now is None else now
    report = _report(scan_id, now)
    wanted = list(dict.fromkeys(str(item).strip() for item in item_ids or [] if str(item).strip()))
    if not wanted:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "item_ids are ids from the scan's items.")
    known = {item["id"]: item for item in report["items"]}
    moved: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for item_id in wanted:
        item = known.get(item_id)
        if item is None:
            skipped.append({"id": item_id, "reason": "not_in_this_scan"})
            continue
        path = item["path"]
        try:
            info = os.lstat(path)
        except OSError:
            skipped.append({"id": item_id, "path": path, "reason": "gone"})
            continue
        if (not stat.S_ISREG(info.st_mode) or info.st_size != item["size_bytes"] or info.st_mtime_ns != item["mtime_ns"]
                or info.st_ino != item["ino"]):
            skipped.append({"id": item_id, "path": path, "reason": "changed_since_scan"})
            continue
        if not _in_scope(Path(path)):
            skipped.append({"id": item_id, "path": path, "reason": "outside_scope"})
            continue
        try:
            done = subprocess.run([TRASH, "-v", path], capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            skipped.append({"id": item_id, "path": path, "reason": "trash_failed", "detail": type(exc).__name__})
            continue
        found = re.search(r'Moved ".*" to "(.*)"', done.stdout or "")
        if done.returncode != 0 or os.path.lexists(path):
            skipped.append({"id": item_id, "path": path, "reason": "trash_failed",
                            "detail": (done.stderr or done.stdout or "").strip()[:200]})
            continue
        moved.append({"id": item_id, "path": path, "size": item["size_bytes"],
                      "in_trash": found.group(1) if found else None})
    receipt_id = None
    if moved:
        receipt_id = "trash_" + secrets.token_hex(8)
        receipt = {"receipt_id": receipt_id, "scan_id": scan_id, "created_at": now, "items": moved}
        target = _root_dir() / f"{receipt_id}.json"
        target.write_text(json.dumps(receipt), encoding="utf-8")
        os.chmod(target, 0o600)
    return {
        "ok": bool(moved), "moved": moved, "skipped": skipped, "bytes_moved": sum(item["size"] for item in moved),
        "receipt_id": receipt_id, "verified": all(not os.path.lexists(item["path"]) for item in moved),
        "note": "In the Trash, not erased: Finder › Put Back restores each one, and storage_review action=undo with "
                "this receipt_id moves them back when Mac MCP may open the Trash.",
    }


def undo(receipt_id: str) -> Dict[str, Any]:
    if not re.fullmatch(r"trash_[0-9a-f]{16}", str(receipt_id or "")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "receipt_id is the id move_to_trash returned.")
    path = _root_dir() / f"{receipt_id}.json"
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That receipt is gone.") from None
    restored: List[str] = []
    put_back: List[Dict[str, str]] = []
    conflicts: List[str] = []
    for item in receipt["items"]:
        original, trashed = item["path"], item.get("in_trash")
        if os.path.lexists(original):
            conflicts.append(original)  # something is there again; nothing is overwritten
            continue
        try:
            if not trashed:
                raise PermissionError
            os.makedirs(os.path.dirname(original), exist_ok=True)
            os.rename(trashed, original)
            restored.append(original)
        except OSError:
            # Mac MCP may not open the Trash without Full Disk Access; Finder's Put Back always can.
            put_back.append({"name": os.path.basename(trashed or original), "back_to": os.path.dirname(original)})
    if restored and not put_back and not conflicts:
        path.unlink(missing_ok=True)
    out: Dict[str, Any] = {"ok": not put_back and not conflicts, "restored": restored, "conflicts": conflicts}
    if put_back:
        out["put_back_in_finder"] = put_back
        out["message"] = ("Mac MCP may not open the Trash (that needs Full Disk Access), so these are still there: "
                          "open the Trash in Finder, select them and choose Put Back.")
    return out
