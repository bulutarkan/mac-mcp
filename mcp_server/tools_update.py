from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict

from fastapi import HTTPException, status

from .update_helper import UpdateError, check_update, resolve_paths
from .update_state import update_state_path


def _public_info(info) -> Dict[str, Any]:
    return {
        "repo": info.repo,
        "runtime": info.runtime,
        "branch": f"{info.remote}/{info.branch}",
        "installed_commit": info.deployed_commit,
        "installed_short": info.deployed_commit[:8],
        "latest_commit": info.target_commit,
        "latest_short": info.target_commit[:8],
        "behind_by": info.behind_by,
        "update_available": info.update_available,
        "dirty": info.dirty,
    }


def mac_mcp_update(check_only: bool = True, branch: str = "main") -> Dict[str, Any]:
    """Check for a commit-based Mac MCP update or start a safe detached update."""
    branch = str(branch or "main").strip()
    if not branch or len(branch) > 120:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "branch must be a non-empty Git branch name.")
    try:
        repo, runtime = resolve_paths()
        info = check_update(repo, runtime, branch=branch, remote="origin", fetch=True)
    except UpdateError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    payload = _public_info(info)
    if info.dirty:
        payload.update({
            "ok": False,
            "blocked": True,
            "reason": "repository_dirty",
            "message": "Update blocked because the repository has local changes. Commit or stash them first.",
        })
        return payload
    if check_only:
        payload.update({
            "ok": True,
            "check_only": True,
            "message": "Update available." if info.update_available else "Mac MCP is up to date.",
        })
        return payload
    if not info.update_available:
        payload.update({"ok": True, "updated": False, "message": "Mac MCP is already up to date."})
        return payload

    update_id = f"upd_{uuid.uuid4().hex[:10]}"
    status_path = update_state_path()
    logs_dir = status_path.parent / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"{update_id}.log"
    helper_src = Path(__file__).with_name("update_helper.py")
    state_src = helper_src.with_name("update_state.py")
    helper_tmp_dir = Path(tempfile.mkdtemp(prefix=f"mac-mcp-update-{update_id}-"))
    helper_tmp = helper_tmp_dir / "update_helper.py"
    shutil.copy2(helper_src, helper_tmp)
    shutil.copy2(state_src, helper_tmp_dir / "update_state.py")

    started_state = {
        "status": "starting",
        "update_id": update_id,
        "from_commit": info.deployed_commit,
        "to_commit": info.target_commit,
        "log_path": str(log_path),
        "status_path": str(status_path),
    }
    status_path.write_text(json.dumps(started_state, indent=2) + "\n", encoding="utf-8")

    log = log_path.open("a", encoding="utf-8")
    cmd = [
        sys.executable,
        str(helper_tmp),
        "--repo", str(repo),
        "--runtime", str(runtime),
        "--branch", branch,
        "--remote", "origin",
        "--deferred-seconds", "0.8",
    ]
    label = os.getenv("MAC_MCP_LAUNCHD_LABEL", "").strip()
    if label:
        cmd.extend(["--launchd-label", label])
    cmd.extend(["--cleanup-staging-dir", str(helper_tmp_dir)])
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=str(repo),
            start_new_session=True,
            close_fds=True,
        )
        log.close()
    except Exception as exc:
        log.close()
        shutil.rmtree(helper_tmp_dir, ignore_errors=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Could not start updater: {exc}") from exc

    payload.update({
        "ok": True,
        "check_only": False,
        "update_started": True,
        "update_id": update_id,
        "updater_pid": proc.pid,
        "log_path": str(log_path),
        "status_path": str(status_path),
        "message": "Update started. Mac MCP will restart automatically; refresh the MCP tools after it reconnects.",
    })
    return payload
