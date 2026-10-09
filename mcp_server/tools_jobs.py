from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from fastapi import HTTPException, status

from .security import BASE_DIR, Settings, require_shell_enabled, truncate
from .workspace_sandbox import shell_execution_plan
from .file_transactions import abort_capture_transaction
from .shell_transactions import begin_shell_capture, finalize_shell_capture, shell_capture_http_error
from .tool_cancellation import ToolCancelledError, cancellable_sleep, cancellation_checkpoint

JOBS_DIR = BASE_DIR / "jobs"
DEFAULT_JOB_ENV = {
    "CI": "1",
    "NO_COLOR": "1",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "HOMEBREW_NO_AUTO_UPDATE": "1",
}

_PROCS: Dict[str, subprocess.Popen[str]] = {}
# Exact last-output time per running job; meta.json only gets a throttled copy.
_LAST_OUTPUT: Dict[str, float] = {}
_LOCK = threading.RLock()
_DEFAULT_WAIT_TIMEOUT_S = 60
_MAX_WAIT_TIMEOUT_S = 600
_ACTIVE_STATUSES = {"starting", "running", "stopping"}
_META_OUTPUT_UPDATE_S = 0.5
_PRUNE_INTERVAL_S = 60.0
_last_prune_at = 0.0


def _env_limit(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return min(maximum, max(minimum, int(os.getenv(name, "") or default)))
    except ValueError:
        return default


def stream_max_bytes() -> int:
    """Bytes kept per stdout/stderr file; later output is drained but not stored."""
    return _env_limit("MAC_MCP_JOB_STREAM_MAX_BYTES", 16 * 1024 * 1024, 64 * 1024, 1024 * 1024 * 1024)


def job_retention() -> Dict[str, int]:
    """Finished jobs older than days, beyond count, or over total bytes are removed."""
    return {
        "days": _env_limit("MAC_MCP_JOB_RETENTION_DAYS", 7, 1, 365),
        "max_jobs": _env_limit("MAC_MCP_JOB_RETENTION_COUNT", 200, 10, 10_000),
        "max_total_bytes": _env_limit("MAC_MCP_JOB_RETENTION_BYTES", 1024 * 1024 * 1024, 16 * 1024 * 1024, 64 * 1024 * 1024 * 1024),
    }


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _now() -> float:
    return time.time()


def _job_dir(job_id: str) -> Path:
    if not job_id or "/" in job_id or ".." in job_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid job_id.")
    return JOBS_DIR / job_id


def _meta_path(job_id: str) -> Path:
    return _job_dir(job_id) / "meta.json"


def _read_meta(job_id: str) -> Dict[str, Any]:
    path = _meta_path(job_id)
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Job not found: {job_id}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Corrupt job metadata: {job_id}") from exc


def _write_meta(job_id: str, meta: Dict[str, Any]) -> None:
    path = _meta_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True))
    tmp.replace(path)


def _base_env(extra_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = os.environ.copy()
    env.update({
        "HOME": str(Path.home()),
        "USER": os.getenv("USER", Path.home().name),
        "LOGNAME": os.getenv("LOGNAME", os.getenv("USER", Path.home().name)),
        "PATH": f"{os.environ.get('PATH', '')}:/usr/local/bin:/opt/homebrew/bin:/opt/homebrew/sbin",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
    })
    env.update(DEFAULT_JOB_ENV)
    if extra_env:
        env.update({str(k): str(v) for k, v in extra_env.items()})
    return env


def _is_pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _surviving_group(pgid: Optional[int]) -> bool:
    """Whether a job's process group still has members after its leader is gone.

    Jobs start in a new session, so the group id equals the leader's pid. Once the
    leader is reaped, a live group with that id can only hold the job's leftover
    descendants: a new group with the same id needs a live process with that pid,
    and in that case the id belongs to someone else and is never claimed.
    """
    if not pgid or _is_pid_alive(pgid):
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _signal_group_until_gone(pgid: int, alive: Callable[[], bool], signal_name: int, grace_s: float) -> None:
    if not alive():
        return
    try:
        os.killpg(pgid, signal_name)
    except ProcessLookupError:
        return
    if signal_name == signal.SIGKILL:
        return

    deadline = time.monotonic() + max(0.0, grace_s)
    while alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    if alive():
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _terminate_process(proc: subprocess.Popen[str], signal_name: int = signal.SIGTERM,
                       grace_s: float = 1.0) -> None:
    """Terminate a job's process group and never leave its descendants behind."""
    _signal_group_until_gone(
        proc.pid, lambda: proc.poll() is None or _surviving_group(proc.pid), signal_name, grace_s,
    )


def _terminate_pid_group(pid: int, signal_name: int = signal.SIGTERM,
                         grace_s: float = 1.0) -> None:
    """Best-effort cleanup for jobs created before this server process started."""
    _signal_group_until_gone(
        pid, lambda: _is_pid_alive(pid) or _surviving_group(pid), signal_name, grace_s,
    )


def _normalize_timeout(value: Optional[int], field_name: str) -> Optional[int]:
    if value is None:
        return None
    try:
        return min(_MAX_WAIT_TIMEOUT_S, max(1, int(value)))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"{field_name} must be a positive integer") from exc


def _wait_timeout(settings: Settings, timeout_s: Optional[int]) -> int:
    if timeout_s is None:
        return _DEFAULT_WAIT_TIMEOUT_S
    try:
        return min(_MAX_WAIT_TIMEOUT_S, max(1, int(timeout_s)))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "timeout_s must be a positive integer") from exc


def _append_stream(job_id: str, stream, filename: str) -> None:
    path = _job_dir(job_id) / filename
    limit = stream_max_bytes()
    written = path.stat().st_size if path.exists() else 0
    capped = False
    cap_recorded = False
    last_meta_at = 0.0
    with path.open("a", encoding="utf-8", errors="replace") as log_file:
        for chunk in iter(stream.readline, ""):
            if not chunk:
                break
            # Keep draining after the cap so the process never blocks on a full pipe.
            if not capped:
                size = len(chunk.encode("utf-8", errors="replace"))
                if written + size <= limit:
                    log_file.write(chunk)
                    log_file.flush()
                    written += size
                else:
                    capped = True
                    log_file.write(f"\n[mac-mcp: {filename} truncated after {written} bytes; later output was not stored]\n")
                    log_file.flush()
            now = _now()
            _LAST_OUTPUT[job_id] = now
            # Output still counts as activity for stall detection, but meta.json is
            # rewritten at most twice a second instead of once per line.
            if (capped and not cap_recorded) or now - last_meta_at >= _META_OUTPUT_UPDATE_S:
                with _LOCK:
                    meta = _read_meta(job_id)
                    meta["last_output_at"] = now
                    if capped:
                        meta[f"{filename.split('.')[0]}_truncated"] = True
                    _write_meta(job_id, meta)
                last_meta_at = now
                cap_recorded = capped
    stream.close()



def _finalize_job_capture(meta: Dict[str, Any]) -> None:
    capture_txid = str(meta.get("capture_transaction_id") or "").strip()
    if not capture_txid or meta.get("transaction"):
        return
    try:
        transaction = finalize_shell_capture(
            capture_txid,
            join_transaction_ids=list(meta.get("join_transaction_ids") or []),
        )
    except BaseException as exc:
        meta["filesystem_outcome"] = "unknown"
        meta["reversibility_error"] = {
            "error": "shell_capture_finalize_failed",
            "message": str(exc)[:500],
            "transaction_id": capture_txid,
        }
        return
    meta["transaction"] = transaction
    meta["transaction_id"] = transaction.get("transaction_id")
    meta["undoable"] = bool(transaction.get("undoable"))
    meta["reversibility"] = transaction.get("reversibility")
    meta["filesystem_outcome"] = "captured"
    meta["changed_count"] = transaction.get("changed_count")
    meta["changed_paths"] = transaction.get("changed_paths")


def _watch_process(job_id: str, timeout_s: Optional[int], no_output_timeout_s: Optional[int]) -> None:
    proc = _PROCS.get(job_id)
    if proc is None:
        return

    deadline = _now() + timeout_s if timeout_s is not None else None
    termination_reason: Optional[str] = None
    while proc.poll() is None:
        with _LOCK:
            meta = _read_meta(job_id)
            last_output_at = max(
                float(meta.get("last_output_at") or meta.get("started_at") or _now()),
                _LAST_OUTPUT.get(job_id, 0.0),
            )
            now = _now()
            if no_output_timeout_s is not None and now - last_output_at >= no_output_timeout_s:
                meta["status"] = "stopping"
                meta["stop_reason"] = "stalled"
                meta["updated_at"] = now
                _write_meta(job_id, meta)
                termination_reason = "stalled"
            elif deadline is not None and now >= deadline:
                meta["status"] = "stopping"
                meta["stop_reason"] = "timeout"
                meta["updated_at"] = now
                _write_meta(job_id, meta)
                termination_reason = "timeout"
        if termination_reason:
            _terminate_process(proc)
            break
        time.sleep(0.5)

    exit_code = proc.wait()
    # The shell exiting does not end the job while descendants in its group still
    # run (for example `server &`); keep the same timeout and stall limits on them.
    while termination_reason is None and _surviving_group(proc.pid):
        with _LOCK:
            meta = _read_meta(job_id)
            last_output_at = max(
                float(meta.get("last_output_at") or meta.get("started_at") or _now()),
                _LAST_OUTPUT.get(job_id, 0.0),
            )
            now = _now()
            if no_output_timeout_s is not None and now - last_output_at >= no_output_timeout_s:
                termination_reason = "stalled"
            elif deadline is not None and now >= deadline:
                termination_reason = "timeout"
            if termination_reason:
                meta.update({"status": "stopping", "stop_reason": termination_reason, "updated_at": now})
                _write_meta(job_id, meta)
        if termination_reason:
            _terminate_process(proc)
            break
        time.sleep(0.5)
    with _LOCK:
        try:
            meta = _read_meta(job_id)
        except HTTPException as exc:
            if exc.status_code == status.HTTP_404_NOT_FOUND:
                _PROCS.pop(job_id, None)
                _LAST_OUTPUT.pop(job_id, None)
                return
            raise
        last_output = _LAST_OUTPUT.pop(job_id, None)
        if last_output is not None:
            meta["last_output_at"] = max(float(meta.get("last_output_at") or 0.0), last_output)
        if meta.get("status") == "killed":
            final_status = "killed"
        elif termination_reason == "stalled" or meta.get("stop_reason") == "stalled" or meta.get("status") == "stalled":
            final_status = "stalled"
        elif termination_reason == "timeout" or meta.get("stop_reason") == "timeout" or meta.get("status") == "timeout":
            final_status = "timeout"
        else:
            final_status = "completed" if exit_code == 0 else "failed"
        meta.update({
            "status": final_status,
            "exit_code": exit_code,
            "ended_at": _now(),
            "updated_at": _now(),
            "duration_ms": int((_now() - float(meta.get("started_at", _now()))) * 1000),
        })
        _finalize_job_capture(meta)
        _write_meta(job_id, meta)
        _PROCS.pop(job_id, None)


_STALE_START_S = 60.0


def _normalize_status(job_id: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    status_value = meta.get("status")
    # Metadata is written before Popen; a crash in between leaves no process at all.
    if (
        status_value == "starting" and not meta.get("pid") and job_id not in _PROCS
        and _now() - float(meta.get("started_at") or 0.0) >= _STALE_START_S
    ):
        meta.update({
            "status": "failed", "exit_code": None, "ended_at": _now(), "updated_at": _now(),
            "note": "The job never started; the server stopped before launching it.",
        })
        _write_meta(job_id, meta)
        return meta
    # A stopped job is "killed" before its processes are gone; finalize it once they are.
    if status_value in {"running", "stalled", "stopping"} or (status_value == "killed" and not meta.get("ended_at")):
        proc = _PROCS.get(job_id)
        if proc is not None and proc.poll() is not None and _surviving_group(proc.pid):
            # The leader exited but its descendants still run; the watcher finalizes.
            return meta
        if proc is not None and proc.poll() is not None:
            exit_code = proc.returncode
            if status_value == "stopping":
                stop_reason = meta.get("stop_reason")
                meta["status"] = stop_reason if stop_reason in {"stalled", "timeout"} else "failed"
            elif status_value not in {"killed", "stalled", "timeout"}:
                meta["status"] = "completed" if exit_code == 0 else "failed"
            meta["exit_code"] = exit_code
            meta["ended_at"] = _now()
            meta["updated_at"] = _now()
            meta["duration_ms"] = int((_now() - float(meta.get("started_at", _now()))) * 1000)
            _finalize_job_capture(meta)
            _write_meta(job_id, meta)
            _PROCS.pop(job_id, None)
        elif proc is None and not _is_pid_alive(meta.get("pid")) and not _surviving_group(meta.get("pid")):
            if meta.get("ended_at"):
                return meta
            if status_value == "stopping":
                stop_reason = meta.get("stop_reason")
                meta["status"] = stop_reason if stop_reason in {"stalled", "timeout"} else "failed"
            elif status_value not in {"killed", "stalled", "timeout"}:
                meta["status"] = "failed"
            meta["exit_code"] = None
            meta["ended_at"] = _now()
            meta["updated_at"] = _now()
            meta["duration_ms"] = int((_now() - float(meta.get("started_at", _now()))) * 1000)
            if status_value not in {"killed", "stalled", "timeout"}:
                meta["note"] = "Process ended while bridge was not tracking it; exit code is unavailable."
            _finalize_job_capture(meta)
            _write_meta(job_id, meta)
    return meta


def _public_meta(job_id: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(meta)
    result["job_id"] = job_id
    result["duration_ms"] = int((float(meta.get("ended_at") or _now()) - float(meta.get("started_at", _now()))) * 1000)
    return result


def start_background_job(
    settings: Settings,
    command: str,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    timeout_s: Optional[int] = None,
    no_output_timeout_s: Optional[int] = None,
    *,
    reversible: bool = False,
    reversible_root: Optional[str] = None,
    join_transaction_ids: Optional[List[str]] = None,
    require_full_reversibility: bool = False,
) -> Dict[str, Any]:
    require_shell_enabled(settings)
    if not command or not command.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "command is required.")

    timeout_s = _normalize_timeout(timeout_s, "timeout_s")
    if timeout_s is None:
        timeout_s = _DEFAULT_WAIT_TIMEOUT_S
    no_output_timeout_s = _normalize_timeout(no_output_timeout_s, "no_output_timeout_s")

    prune_jobs()
    job_id = uuid.uuid4().hex[:12]
    job_path = _job_dir(job_id)
    _private_dir(JOBS_DIR)
    _private_dir(job_path)
    for name in ("stdout.log", "stderr.log"):
        (job_path / name).touch(mode=0o600)

    if join_transaction_ids and not reversible:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "join_transaction_ids requires reversible=true.")
    plan = shell_execution_plan(settings.workdir, requested_cwd=cwd, extra_env=env)
    workdir = plan.cwd
    argv = plan.argv(command)
    capture: Optional[Dict[str, Any]] = None
    capture_txid: Optional[str] = None
    if reversible:
        capture_root = Path(reversible_root).expanduser() if reversible_root else Path(workdir)
        try:
            capture = begin_shell_capture(capture_root, require_full=bool(require_full_reversibility))
            capture_txid = str(capture["transaction_id"])
        except BaseException as exc:
            if isinstance(exc, HTTPException):
                raise
            raise shell_capture_http_error(exc) from exc
    started_at = _now()
    meta = {
        "job_id": job_id,
        "command": command,
        "cwd": str(workdir),
        "pid": None,
        "status": "starting",
        "exit_code": None,
        "started_at": started_at,
        "updated_at": started_at,
        "last_output_at": started_at,
        "ended_at": None,
        "timeout_s": timeout_s,
        "no_output_timeout_s": no_output_timeout_s,
        "sandboxed": plan.sandboxed,
        "reversible_capture": bool(reversible),
        "capture_transaction_id": capture_txid,
        "join_transaction_ids": list(join_transaction_ids or []),
        "require_full_reversibility": bool(require_full_reversibility),
        "transaction": None,
        "transaction_id": None,
        "undoable": None,
        "reversibility": None,
        "filesystem_outcome": "capturing" if capture_txid else None,
        "reversibility_error": None,
    }
    _write_meta(job_id, meta)

    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(workdir),
            env=plan.env if plan.sandboxed else _base_env(env),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            bufsize=1,
            start_new_session=True,
        )
    except OSError as exc:
        if capture_txid:
            try:
                abort_capture_transaction(capture_txid, reason="process_start_failed", outcome_unknown=False)
            except Exception:
                pass
        meta.update({"status": "failed", "ended_at": _now(), "updated_at": _now(), "error": str(exc)})
        _write_meta(job_id, meta)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Could not start job: {exc}") from exc

    with _LOCK:
        _PROCS[job_id] = proc
        meta.update({"pid": proc.pid, "status": "running", "updated_at": _now()})
        _write_meta(job_id, meta)

    threading.Thread(target=_append_stream, args=(job_id, proc.stdout, "stdout.log"), daemon=True).start()
    threading.Thread(target=_append_stream, args=(job_id, proc.stderr, "stderr.log"), daemon=True).start()
    threading.Thread(target=_watch_process, args=(job_id, timeout_s, no_output_timeout_s), daemon=True).start()

    result = {
        "ok": True, "job_id": job_id, "pid": proc.pid, "status": "running",
        "command": command, "cwd": str(workdir), "sandboxed": plan.sandboxed,
        "reversible_capture": bool(reversible),
    }
    if capture_txid:
        result["capture_transaction_id"] = capture_txid
        result["capture_mode"] = capture.get("mode") if capture else None
    return result


def get_job_status(settings: Settings, job_id: str) -> Dict[str, Any]:
    with _LOCK:
        meta = _normalize_status(job_id, _read_meta(job_id))
        return {"ok": True, **_public_meta(job_id, meta)}


def _job_bytes(path: Path) -> int:
    total = 0
    for child in path.iterdir():
        try:
            total += child.stat().st_size
        except OSError:
            pass
    return total


def _is_active(job_id: str, meta: Dict[str, Any]) -> bool:
    return job_id in _PROCS or str(meta.get("status") or "") in _ACTIVE_STATUSES or not meta.get("ended_at")


def _remove_job_dir(path: Path) -> None:
    for child in path.iterdir():
        try:
            child.unlink()
        except OSError:
            pass
    try:
        path.rmdir()
    except OSError:
        pass


def prune_jobs(*, force: bool = False) -> Dict[str, Any]:
    """Drop finished jobs past the age, count or total-size limit; active jobs stay."""
    global _last_prune_at
    now = _now()
    if not force and now - _last_prune_at < _PRUNE_INTERVAL_S:
        return {"pruned": 0, "skipped": True}
    _last_prune_at = now
    limits = job_retention()
    if not JOBS_DIR.exists():
        return {"pruned": 0, **limits}
    finished: List[tuple[float, Path, int]] = []
    kept_bytes = 0
    pruned = 0
    with _LOCK:
        for path in JOBS_DIR.iterdir():
            if not path.is_dir():
                continue
            try:
                meta = _normalize_status(path.name, _read_meta(path.name))
            except HTTPException:
                continue
            size = _job_bytes(path)
            if _is_active(path.name, meta):
                kept_bytes += size
                continue
            finished.append((float(meta.get("ended_at") or meta.get("updated_at") or 0.0), path, size))
        finished.sort(key=lambda item: item[0], reverse=True)  # newest first
        cutoff = now - limits["days"] * 86400
        for index, (ended_at, path, size) in enumerate(finished):
            over_count = index >= limits["max_jobs"]
            over_bytes = kept_bytes + size > limits["max_total_bytes"]
            if ended_at < cutoff or over_count or over_bytes:
                _remove_job_dir(path)
                pruned += 1
            else:
                kept_bytes += size
    return {"pruned": pruned, **limits}


def list_jobs(settings: Settings, status_filter: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
    _private_dir(JOBS_DIR)
    prune_jobs()
    bounded = max(1, min(int(limit or 50), 500))
    jobs: List[Dict[str, Any]] = []
    total = 0
    with _LOCK:
        for path in sorted(JOBS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not path.is_dir() or not (path / "meta.json").exists():
                continue
            meta = _normalize_status(path.name, _read_meta(path.name))
            public = _public_meta(path.name, meta)
            if status_filter and public.get("status") != status_filter:
                continue
            total += 1
            if len(jobs) < bounded:
                jobs.append(public)
    return {
        "ok": True, "jobs": jobs, "count": len(jobs), "total": total,
        "truncated": total > len(jobs), "retention": job_retention(),
    }


def delete_job(settings: Settings, job_id: str) -> Dict[str, Any]:
    """Remove a finished job's metadata and both output streams."""
    path = _job_dir(job_id)
    with _LOCK:
        meta = _read_meta(job_id)
        meta = _normalize_status(job_id, meta)
        if _is_active(job_id, meta):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {"error": "job_active", "message": "Stop the job before deleting it.", "status": meta.get("status")},
            )
        _remove_job_dir(path)
    return {"ok": True, "job_id": job_id, "deleted": not path.exists()}


def get_job_output(
    settings: Settings,
    job_id: str,
    tail_lines: Optional[int] = None,
    since_offset: Optional[int] = None,
    stream: str = "both",
) -> Dict[str, Any]:
    streams = ["stdout", "stderr"] if stream == "both" else [stream]
    if any(s not in {"stdout", "stderr"} for s in streams):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "stream must be stdout, stderr, or both.")

    meta = _read_meta(job_id)
    # Read only the requested slice: memory stays bounded by the response budget,
    # not by how much the job wrote.
    budget = max(4096, int(settings.max_output_chars) * 4)
    result: Dict[str, Any] = {"ok": True, "job_id": job_id, "offsets": {}, "sizes": {}}
    for name in streams:
        path = _job_dir(job_id) / f"{name}.log"
        size = path.stat().st_size if path.exists() else 0
        start = min(max(0, int(since_offset or 0)), size)
        if since_offset is None and tail_lines is not None:
            start = max(0, size - budget)
        length = min(size - start, budget)
        data = b""
        if length > 0:
            with path.open("rb") as handle:
                handle.seek(start)
                data = handle.read(length)
        end = start + len(data)
        text = data.decode("utf-8", errors="replace")
        if tail_lines is not None:
            text = "\n".join(text.splitlines()[-max(0, int(tail_lines)):])
        text, truncated = truncate(text, settings.max_output_chars)
        result[name] = text
        result[f"{name}_truncated"] = truncated or end < size
        # A follow-up since_offset continues where this slice ended.
        result["offsets"][name] = size if tail_lines is not None and since_offset is None else end
        result["sizes"][name] = size
        if meta.get(f"{name}_truncated"):
            result[f"{name}_capped_at_bytes"] = stream_max_bytes()
    return result


def stop_job(settings: Settings, job_id: str, signal_name: str = "TERM") -> Dict[str, Any]:
    sig_name = signal_name.upper()
    allowed = {"TERM": signal.SIGTERM, "KILL": signal.SIGKILL, "INT": signal.SIGINT, "HUP": signal.SIGHUP}
    if sig_name not in allowed:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"signal must be one of: {', '.join(allowed)}")

    with _LOCK:
        meta = _normalize_status(job_id, _read_meta(job_id))
        if meta.get("status") not in {"running", "stalled", "stopping", "starting"}:
            return {"ok": True, "job_id": job_id, "status": meta.get("status"), "message": "Job is not running."}
        meta["status"] = "killed"
        meta["updated_at"] = _now()
        _write_meta(job_id, meta)

    pid = meta.get("pid")
    if pid:
        proc = _PROCS.get(job_id)
        if proc is not None:
            _terminate_process(proc, allowed[sig_name], grace_s=1.5)
        else:
            _terminate_pid_group(int(pid), allowed[sig_name], grace_s=1.5)
    return {"ok": True, "job_id": job_id, "status": "killed", "signal": sig_name}


def wait_jobs(
    settings: Settings,
    job_ids: List[str],
    timeout_s: Optional[int] = None,
    return_output: bool = False,
) -> Dict[str, Any]:
    if not job_ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "job_ids is required.")
    wait_timeout = _wait_timeout(settings, timeout_s)
    deadline = time.monotonic() + wait_timeout
    terminal = {"completed", "failed", "timeout", "killed", "stalled"}

    def is_finished(item: Dict[str, Any]) -> bool:
        return item.get("status") in terminal and bool(item.get("ended_at"))

    while True:
        statuses = [get_job_status(settings, job_id) for job_id in job_ids]
        if all(is_finished(s) for s in statuses):
            break
        if time.monotonic() >= deadline:
            break
        cancellable_sleep(0.25)

    if return_output:
        for item in statuses:
            output = get_job_output(settings, item["job_id"])
            item["stdout"] = output.get("stdout", "")
            item["stderr"] = output.get("stderr", "")

    return {"ok": True, "jobs": statuses, "completed": all(is_finished(s) for s in statuses)}


def run_commands_parallel(
    settings: Settings,
    commands: List[str],
    cwd: Optional[str] = None,
    timeout_s: Optional[int] = None,
    return_output: bool = True,
) -> Dict[str, Any]:
    require_shell_enabled(settings)
    if not commands:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "commands is required.")
    effective_timeout = _wait_timeout(settings, timeout_s)
    starts = [
        start_background_job(settings, command=command, cwd=cwd, timeout_s=effective_timeout)
        for command in commands
    ]
    try:
        cancellation_checkpoint()
        waited = wait_jobs(
            settings, [j["job_id"] for j in starts],
            timeout_s=effective_timeout, return_output=return_output,
        )
    except ToolCancelledError:
        for started in starts:
            try:
                stop_job(settings, str(started.get("job_id") or ""), signal_name="TERM")
            except Exception:
                pass
        raise
    waited["started"] = starts
    return waited
