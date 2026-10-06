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

from .update_helper import (
    UpdateError,
    check_update,
    resolve_paths,
    secure_bootstrap_update_blocker,
    validate_update_state,
)
from .update_state import INCOMPLETE_UPDATE_STATES, update_state_path


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
        "release_verified": info.release_verified,
        "release_id": info.release_id,
        "release_version": info.release_version,
        "release_payload_sha256": info.release_payload_sha256,
        "release_signer_fingerprint": info.release_signer_fingerprint,
        "release_file_count": info.release_file_count,
        "release_artifact_count": info.release_artifact_count,
        "branch_tip_commit": info.branch_tip_commit,
        "unverified_ahead": info.unverified_ahead,
    }


def launch_detached_update(
    info,
    repo: Path,
    runtime: Path,
    *,
    branch: str,
    remote: str,
    launchd_label: str = "",
    skip_restart: bool = False,
    skip_deps: bool = False,
) -> tuple[Dict[str, Any], subprocess.Popen]:
    """Launch the staged updater in a process session that survives its caller."""
    payload = _public_info(info)
    update_id = f"upd_{uuid.uuid4().hex[:10]}"
    status_path = update_state_path()
    logs_dir = status_path.parent / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"{update_id}.log"

    helper_src = Path(__file__).with_name("update_helper.py")
    state_src = helper_src.with_name("update_state.py")
    release_trust_src = helper_src.with_name("release_trust.py")
    managed_process_src = helper_src.with_name("managed_process.py")
    trusted_signers_src = helper_src.with_name("release_trusted_signers.txt")
    helper_tmp_dir = Path(tempfile.mkdtemp(prefix=f"mac-mcp-update-{update_id}-"))
    helper_tmp = helper_tmp_dir / "update_helper.py"
    shutil.copy2(helper_src, helper_tmp)
    shutil.copy2(state_src, helper_tmp_dir / "update_state.py")
    shutil.copy2(release_trust_src, helper_tmp_dir / "release_trust.py")
    shutil.copy2(managed_process_src, helper_tmp_dir / "managed_process.py")
    shutil.copy2(trusted_signers_src, helper_tmp_dir / "release_trusted_signers.txt")

    existing_state = validate_update_state()
    preserve_recovery_journal = bool(
        isinstance(existing_state, dict)
        and int(existing_state.get("transaction_version") or 0) == 1
        and str(existing_state.get("status") or "") in INCOMPLETE_UPDATE_STATES
    )
    started_state = {
        "status": "starting",
        "update_id": update_id,
        "from_commit": info.deployed_commit,
        "to_commit": info.target_commit,
        "repo": str(repo),
        "runtime": str(runtime),
        "release_id": info.release_id,
        "release_version": info.release_version,
        "log_path": str(log_path),
        "status_path": str(status_path),
    }
    if not preserve_recovery_journal:
        status_path.write_text(json.dumps(started_state, indent=2) + "\n", encoding="utf-8")

    log = log_path.open("a", encoding="utf-8")
    cmd = [
        sys.executable,
        str(helper_tmp),
        "--repo", str(repo),
        "--runtime", str(runtime),
        "--branch", branch,
        "--remote", remote,
        "--deferred-seconds", "0.8",
    ]
    if launchd_label:
        cmd.extend(["--launchd-label", launchd_label])
    if skip_restart:
        cmd.append("--skip-restart")
    if skip_deps:
        cmd.append("--skip-deps")
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
    except Exception:
        log.close()
        shutil.rmtree(helper_tmp_dir, ignore_errors=True)
        raise

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
    return payload, proc


def mac_mcp_update(check_only: bool = True, branch: str = "main") -> Dict[str, Any]:
    """Check for a commit-based Mac MCP update or start a safe detached update."""
    branch = str(branch or "main").strip()
    if not branch or len(branch) > 120:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "branch must be a non-empty Git branch name.")
    try:
        validate_update_state()
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
    bootstrap_blocker = secure_bootstrap_update_blocker(runtime) if info.update_available else None
    if bootstrap_blocker is not None:
        payload.update({
            "ok": False,
            "blocked": True,
            "reason": "secure_bootstrap_migration_required",
            "migration": bootstrap_blocker,
            "message": str(bootstrap_blocker.get("summary") or "Secure bootstrap migration is required before updating."),
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

    try:
        payload, _proc = launch_detached_update(
            info,
            repo,
            runtime,
            branch=branch,
            remote="origin",
            launchd_label=os.getenv("MAC_MCP_LAUNCHD_LABEL", "").strip(),
        )
    except Exception as exc:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"Could not start updater: {exc}",
        ) from exc
    return payload
