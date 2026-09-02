from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status

from .security import BASE_DIR, Settings, truncate

JOBS_DIR = BASE_DIR / "jobs"
DEFAULT_JOB_ENV = {
    "CI": "1",
    "NO_COLOR": "1",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "HOMEBREW_NO_AUTO_UPDATE": "1",
}

_PROCS: Dict[str, subprocess.Popen[str]] = {}
_LOCK = threading.RLock()
_DEFAULT_WAIT_TIMEOUT_S = 60
_MAX_WAIT_TIMEOUT_S = 600


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
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
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


def _terminate_process(proc: subprocess.Popen[str], signal_name: int = signal.SIGTERM,
                       grace_s: float = 1.0) -> None:
    """Terminate a job's process group and never leave its descendants behind."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal_name)
    except ProcessLookupError:
        return

    if signal_name == signal.SIGKILL:
        return

    deadline = time.monotonic() + max(0.0, grace_s)
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _terminate_pid_group(pid: int, signal_name: int = signal.SIGTERM,
                         grace_s: float = 1.0) -> None:
    """Best-effort cleanup for jobs created before this server process started."""
    try:
        os.killpg(pid, signal_name)
    except ProcessLookupError:
        return
    if signal_name == signal.SIGKILL:
        return

    deadline = time.monotonic() + max(0.0, grace_s)
    while _is_pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _is_pid_alive(pid):
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


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
    with path.open("a", encoding="utf-8", errors="replace") as log_file:
        for chunk in iter(stream.readline, ""):
            if not chunk:
                break
            log_file.write(chunk)
            log_file.flush()
            with _LOCK:
                meta = _read_meta(job_id)
                meta["last_output_at"] = _now()
                _write_meta(job_id, meta)
    stream.close()


def _watch_process(job_id: str, timeout_s: Optional[int], no_output_timeout_s: Optional[int]) -> None:
    proc = _PROCS.get(job_id)
    if proc is None:
        return

    deadline = _now() + timeout_s if timeout_s is not None else None
    termination_reason: Optional[str] = None
    while proc.poll() is None:
        with _LOCK:
            meta = _read_meta(job_id)
            last_output_at = float(meta.get("last_output_at") or meta.get("started_at") or _now())
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
    with _LOCK:
        meta = _read_meta(job_id)
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
        _write_meta(job_id, meta)
        _PROCS.pop(job_id, None)


def _normalize_status(job_id: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    status_value = meta.get("status")
    if status_value in {"running", "stalled", "stopping"}:
        proc = _PROCS.get(job_id)
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
            _write_meta(job_id, meta)
            _PROCS.pop(job_id, None)
        elif proc is None and not _is_pid_alive(meta.get("pid")):
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
) -> Dict[str, Any]:
    if not command or not command.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "command is required.")

    timeout_s = _normalize_timeout(timeout_s, "timeout_s")
    if timeout_s is None:
        timeout_s = _DEFAULT_WAIT_TIMEOUT_S
    no_output_timeout_s = _normalize_timeout(no_output_timeout_s, "no_output_timeout_s")

    job_id = uuid.uuid4().hex[:12]
    job_path = _job_dir(job_id)
    job_path.mkdir(parents=True, exist_ok=True)
    (job_path / "stdout.log").touch()
    (job_path / "stderr.log").touch()

    workdir = Path(cwd).expanduser().resolve() if cwd else settings.workdir
    if not workdir.exists() or not workdir.is_dir():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"cwd does not exist or is not a directory: {workdir}")

    argv = ["/bin/zsh", "-lc", command] if settings.allow_shell else command.split()
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
    }
    _write_meta(job_id, meta)

    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(workdir),
            env=_base_env(env),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            bufsize=1,
            start_new_session=True,
        )
    except OSError as exc:
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

    return {"ok": True, "job_id": job_id, "pid": proc.pid, "status": "running", "command": command, "cwd": str(workdir)}


def get_job_status(settings: Settings, job_id: str) -> Dict[str, Any]:
    with _LOCK:
        meta = _normalize_status(job_id, _read_meta(job_id))
        return {"ok": True, **_public_meta(job_id, meta)}


def list_jobs(settings: Settings, status_filter: Optional[str] = None) -> Dict[str, Any]:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    jobs: List[Dict[str, Any]] = []
    with _LOCK:
        for path in sorted(JOBS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not path.is_dir() or not (path / "meta.json").exists():
                continue
            meta = _normalize_status(path.name, _read_meta(path.name))
            public = _public_meta(path.name, meta)
            if status_filter and public.get("status") != status_filter:
                continue
            jobs.append(public)
    return {"ok": True, "jobs": jobs, "count": len(jobs)}


def get_job_output(
    settings: Settings,
    job_id: str,
    tail_lines: Optional[int] = None,
    since_offset: Optional[int] = None,
    stream: str = "both",
) -> Dict[str, Any]:
    _read_meta(job_id)
    streams = ["stdout", "stderr"] if stream == "both" else [stream]
    if any(s not in {"stdout", "stderr"} for s in streams):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "stream must be stdout, stderr, or both.")

    result: Dict[str, Any] = {"ok": True, "job_id": job_id, "offsets": {}}
    for name in streams:
        path = _job_dir(job_id) / f"{name}.log"
        data = path.read_bytes() if path.exists() else b""
        start = max(0, int(since_offset or 0))
        chunk = data[start:]
        text = chunk.decode("utf-8", errors="replace")
        if tail_lines is not None:
            text = "\n".join(text.splitlines()[-max(0, int(tail_lines)):])
        text, truncated = truncate(text, settings.max_output_chars)
        result[name] = text
        result[f"{name}_truncated"] = truncated
        result["offsets"][name] = len(data)
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
        time.sleep(0.25)

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
    if not commands:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "commands is required.")
    effective_timeout = _wait_timeout(settings, timeout_s)
    starts = [
        start_background_job(settings, command=command, cwd=cwd, timeout_s=effective_timeout)
        for command in commands
    ]
    waited = wait_jobs(settings, [j["job_id"] for j in starts], timeout_s=effective_timeout, return_output=return_output)
    waited["started"] = starts
    return waited
