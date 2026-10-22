"""`mac-mcp promote`: put the source repo's committed HEAD into the runtime, the way an update would.

For a maintainer whose runtime runs from their own development checkout. Between signed
checkpoints, changes reached the runtime by hand, file by file, and the runtime kept its last
checkpoint as its deployed commit, so the menu kept offering an update. `mac-mcp update` could not
help: it fast-forwards the checkout to the newest checkpoint, and a checkout already past it
stays where it is.

promote uses the updater's own steps: the three-way runtime merge that keeps local runtime
customizations (and conflicts instead of overwriting them), the backup, the sync, the menu app and
server helper refresh, the restart and the health check. On a failure the runtime is restored from
the backup. Only on success does the runtime's deployed commit become HEAD.

It does not replace the stable channel. It takes no signature, and it only accepts HEAD of `main`
when HEAD is committed, clean and already on `origin/main`: the same history the updater
fast-forwards along, so a later signed checkpoint updates from here as usual. A change to the
Python dependencies is left to `mac-mcp update`, which builds the new environment transactionally.
It never moves the repository.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import update_helper as uh
from .update_state import INCOMPLETE_UPDATE_STATES, UpdateInProgress, acquire_update_lock

BRANCH = uh.DEFAULT_BRANCH
REMOTE = uh.DEFAULT_REMOTE
MANAGED = ("mcp_server", "menu_app")


class PromoteError(RuntimeError):
    pass


@dataclass
class PromotePlan:
    repo: str
    runtime: str
    deployed_commit: str
    head_commit: str
    changed_files: list[str] = field(default_factory=list)
    server_changed: bool = False
    menu_app_changed: bool = False
    deps_changed: bool = False
    up_to_date: bool = False


def _git(repo: Path, *args: str, check: bool = True) -> str:
    return uh._git(repo, *args, check=check)


def plan_promote(repo: Path, runtime: Path) -> PromotePlan:
    """What promote would do, or why it refuses. Changes nothing."""
    if not (repo / ".git").exists():
        raise PromoteError(f"Not a Git checkout: {repo}")
    if not (runtime / "mcp_server").is_dir():
        raise PromoteError(f"Runtime not found: {runtime}")
    branch = _git(repo, "branch", "--show-current")
    if branch != BRANCH:
        raise PromoteError(f"The repository must be on '{BRANCH}' (it is on '{branch or 'a detached HEAD'}').")
    if _git(repo, "status", "--porcelain"):
        raise PromoteError("The repository has uncommitted changes. promote only takes committed work: commit first.")
    head = _git(repo, "rev-parse", "HEAD")
    tracking = _git(repo, "rev-parse", "--verify", "--quiet", f"{REMOTE}/{BRANCH}", check=False)
    if not tracking or uh._run(["git", "-C", str(repo), "merge-base", "--is-ancestor", head, tracking],
                               check=False).returncode != 0:
        raise PromoteError(f"HEAD {uh._short(head)} is not on {REMOTE}/{BRANCH}. Push it first, so a later "
                           "checkpoint can update from it.")
    deployed = uh._read_state_commit(runtime) or ""
    if not deployed or _git(repo, "cat-file", "-t", deployed, check=False) != "commit":
        raise PromoteError("The runtime has no known deployed commit. Install or run `mac-mcp update` once first.")
    if uh._run(["git", "-C", str(repo), "merge-base", "--is-ancestor", deployed, head], check=False).returncode != 0:
        raise PromoteError(f"The deployed commit {uh._short(deployed)} is not behind HEAD {uh._short(head)}; "
                           "the histories diverged.")
    plan = PromotePlan(repo=str(repo), runtime=str(runtime), deployed_commit=deployed, head_commit=head)
    if deployed == head:
        plan.up_to_date = True
        return plan
    changed = _git(repo, "diff", "--name-only", deployed, head, "--", *MANAGED)
    plan.changed_files = [line for line in changed.splitlines() if line]
    plan.server_changed = any(path.startswith("mcp_server/") for path in plan.changed_files)
    plan.menu_app_changed = any(path.startswith("menu_app/") for path in plan.changed_files)
    plan.deps_changed = uh._deps_changed(repo, deployed, head)
    return plan


def format_plan(plan: PromotePlan) -> str:
    if plan.up_to_date:
        return f"The runtime already runs {uh._short(plan.head_commit)} (HEAD). Nothing to promote."
    lines = [
        f"Promote {uh._short(plan.deployed_commit)} -> {uh._short(plan.head_commit)} "
        f"({len(plan.changed_files)} managed file(s) changed)",
        f"  server: {'changes, restart' if plan.server_changed else 'unchanged'}",
        f"  menu app: {'changes, rebuilt and relaunched' if plan.menu_app_changed else 'unchanged'}",
    ]
    if plan.deps_changed:
        lines.append("  dependencies: CHANGED - promote stops here; publish a checkpoint and run `mac-mcp update`.")
    return "\n".join(lines)


def _compile(runtime: Path, files: list[str]) -> None:
    sources = [str(runtime / rel) for rel in files if rel.endswith(".py") and (runtime / rel).is_file()]
    if not sources:
        return
    result = subprocess.run([sys.executable, "-m", "py_compile", *sources], capture_output=True, text=True,
                            timeout=120)
    if result.returncode != 0:
        raise PromoteError(f"A promoted file does not compile: {(result.stderr or result.stdout).strip()[:400]}")


def promote(
    repo: Optional[str | Path] = None,
    runtime: Optional[str | Path] = None,
    *,
    restart: bool = True,
    refresh_menu_app: bool = True,
    launchd_label: str = uh.DEFAULT_LAUNCHD_LABEL,
    say: Callable[[str], None] = lambda line: print(f"[mac-mcp promote] {line}", flush=True),
) -> dict:
    repo_path, runtime_path = uh.resolve_paths(str(repo) if repo else None, str(runtime) if runtime else None)
    try:
        lock_fd = acquire_update_lock()
    except UpdateInProgress as exc:
        raise PromoteError(str(exc)) from exc
    try:
        state = uh.validate_update_state()
        if isinstance(state, dict) and state.get("status") in INCOMPLETE_UPDATE_STATES:
            raise PromoteError("An update did not finish. Run `mac-mcp update` to recover it before promoting.")
        plan = plan_promote(repo_path, runtime_path)
        say(format_plan(plan))
        if plan.up_to_date:
            return {"ok": True, "promoted": False, **asdict(plan)}
        if plan.deps_changed:
            raise PromoteError("Python dependencies changed since the deployed commit; promote does not install "
                               "them. Publish a checkpoint and run `mac-mcp update`.")
        return _apply(plan, repo_path, runtime_path, restart=restart, refresh_menu_app=refresh_menu_app,
                      launchd_label=launchd_label, say=say)
    finally:
        os.close(lock_fd)


def _apply(plan: PromotePlan, repo: Path, runtime: Path, *, restart: bool, refresh_menu_app: bool,
           launchd_label: str, say: Callable[[str], None]) -> dict:
    temp_root = Path(tempfile.mkdtemp(prefix="mac-mcp-promote-", dir=str(uh.update_staging_root())))
    stage: Optional[Path] = None
    backup: Optional[Path] = None
    synced = False
    restarted = False
    try:
        stage, overlays = uh._prepare_runtime_merge(repo, runtime, plan.deployed_commit, plan.head_commit, temp_root)
        if overlays:
            say(f"Kept {overlays} runtime customization(s).")
        backup, old_files, new_files = uh._backup_runtime(repo, runtime, plan.deployed_commit, plan.head_commit)
        say(f"Backup: {backup}")
        synced = True
        count = uh._sync_runtime(stage, runtime, old_files, new_files)
        say(f"Synced {count} managed runtime file(s).")
        _compile(runtime, plan.changed_files)
        if plan.menu_app_changed and refresh_menu_app:
            if uh._refresh_installed_menu_app(runtime):
                say("Rebuilt and relaunched the Mac MCP menu bar app.")
            uh._ensure_server_helper(runtime)
        if plan.server_changed and restart:
            say("Restarting Mac MCP...")
            restarted = True
            health_url = uh._restart_service(runtime, launchd_label)
            if not uh._health_ok(health_url):
                raise PromoteError(f"Health check failed after restart: {health_url}")
            say(f"Health check passed: {health_url}")
        uh._write_state_commit(runtime, plan.head_commit)
        say(f"The runtime now runs {uh._short(plan.head_commit)}.")
        return {"ok": True, "promoted": True, "backup": str(backup), **asdict(plan)}
    except Exception as exc:
        if synced and backup is not None:
            uh._restore_runtime(runtime, backup)
            say(f"Restored the runtime from {backup}.")
            if restarted:
                health_url = uh._restart_service(runtime, launchd_label)
                say(f"Restarted the previous runtime: {'healthy' if uh._health_ok(health_url) else 'NOT healthy'}.")
        if isinstance(exc, (PromoteError, uh.UpdateError)):
            raise PromoteError(str(exc)) from exc
        raise
    finally:
        if stage is not None:
            _git(repo, "worktree", "remove", "--force", str(stage), check=False)
        shutil.rmtree(temp_root, ignore_errors=True)
