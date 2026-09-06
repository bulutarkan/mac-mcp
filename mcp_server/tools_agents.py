from __future__ import annotations

import json
import os
import re
import shutil
import signal as signal_module
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, status

from .security import BASE_DIR, Settings, truncate

AGENTS_DIR = BASE_DIR / "agents"
TERMINAL_STATUSES = {"completed", "failed", "timeout", "cancelled"}
DEFAULT_AGENT_TIMEOUT_S = 1800
MAX_AGENT_TIMEOUT_S = 7200
DEFAULT_RESULT_LIMIT = 6000
DETAILED_RESULT_LIMIT = 20000

_PROVIDER_NAMES = {"opencode", "codex"}
_ACCESS_MODES = {"read_only", "workspace_write", "full"}
_RESULT_STYLES = {"concise", "detailed"}
_WORKERS: Dict[str, subprocess.Popen] = {}
_WORKERS_LOCK = threading.RLock()


def _now() -> float:
    return time.time()


def _agent_dir(agent_id: str) -> Path:
    if not agent_id or "/" in agent_id or ".." in agent_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid agent_id.")
    return AGENTS_DIR / agent_id


def _meta_path(agent_id: str) -> Path:
    return _agent_dir(agent_id) / "meta.json"


def _read_meta(agent_id: str) -> Dict[str, Any]:
    path = _meta_path(agent_id)
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Agent not found: {agent_id}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Corrupt agent metadata: {agent_id}") from exc


def _write_meta(agent_id: str, meta: Dict[str, Any]) -> None:
    path = _meta_path(agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _tail_text(path: Path, max_lines: int = 40, max_chars: int = 6000) -> str:
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-max(1, max_lines):])[-max_chars:]


def _is_pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _kill_group(pid: Optional[int], sig: int) -> None:
    if not pid:
        return
    try:
        os.killpg(int(pid), sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        pass


def _reap_worker(agent_id: str, proc: subprocess.Popen) -> None:
    try:
        proc.wait()
    finally:
        with _WORKERS_LOCK:
            _WORKERS.pop(agent_id, None)


def _base_env() -> Dict[str, str]:
    env = os.environ.copy()
    home = Path.home()
    user = os.getenv("USER") or home.name
    path_parts = [
        str(home / ".opencode" / "bin"),
        str(home / ".npm-global" / "bin"),
        str(home / ".local" / "bin"),
        "/opt/homebrew/bin",
        "/opt/homebrew/sbin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    inherited = env.get("PATH", "")
    if inherited:
        path_parts.insert(0, inherited)
    env.update({
        "HOME": str(home),
        "USER": user,
        "LOGNAME": os.getenv("LOGNAME") or user,
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "CI": "1",
        "NO_COLOR": "1",
        "HOMEBREW_NO_AUTO_UPDATE": "1",
        "PATH": ":".join(path_parts),
    })
    root = str(BASE_DIR.parent)
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = root + (":" + existing_pythonpath if existing_pythonpath else "")
    return env


def _find_binary(provider: str) -> Optional[str]:
    provider = provider.lower()
    home = Path.home()
    if provider == "opencode":
        candidates = [
            os.getenv("OPENCODE_BINARY"),
            shutil.which("opencode"),
            "/opt/homebrew/bin/opencode",
            str(home / ".opencode" / "bin" / "opencode"),
            "/usr/local/bin/opencode",
        ]
    elif provider == "codex":
        candidates = [
            os.getenv("CODEX_BINARY"),
            shutil.which("codex"),
            "/opt/homebrew/bin/codex",
            "/Applications/ChatGPT.app/Contents/Resources/codex",
            "/usr/local/bin/codex",
            str(home / ".npm-global" / "bin" / "codex"),
        ]
    else:
        return None
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(Path(candidate))
    return None


def _version(binary: Optional[str]) -> Optional[str]:
    if not binary:
        return None
    try:
        proc = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10, env=_base_env())
        text = (proc.stdout or proc.stderr or "").strip()
        return text.splitlines()[0] if text else None
    except Exception:
        return None


def _opencode_models(binary: str) -> List[str]:
    try:
        proc = subprocess.run([binary, "models"], capture_output=True, text=True, timeout=30, env=_base_env())
        if proc.returncode != 0:
            return []
        return [line.strip() for line in proc.stdout.splitlines() if line.strip() and "/" in line]
    except Exception:
        return []


def _codex_known_models() -> Tuple[List[str], Optional[str], Optional[str]]:
    config = Path.home() / ".codex" / "config.toml"
    if not config.exists():
        return [], None, None
    try:
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], None, None
    default_model_match = re.search(r'^model\s*=\s*"([^"]+)"', text, re.MULTILINE)
    default_reasoning_match = re.search(r'^model_reasoning_effort\s*=\s*"([^"]+)"', text, re.MULTILINE)
    section_match = re.search(r'\[tui\.model_availability_nux\](.*?)(?:\n\[|\Z)', text, re.DOTALL)
    models: List[str] = []
    if section_match:
        models.extend(re.findall(r'^"([^"]+)"\s*=', section_match.group(1), re.MULTILINE))
    default_model = default_model_match.group(1) if default_model_match else None
    if default_model and default_model not in models:
        models.insert(0, default_model)
    return models, default_model, default_reasoning_match.group(1) if default_reasoning_match else None


def agent_catalog(
    settings: Settings,
    provider: Optional[str] = None,
    model_filter: Optional[str] = None,
    free_only: bool = False,
    limit: int = 80,
) -> Dict[str, Any]:
    requested = provider.lower().strip() if provider else None
    limit = max(1, min(int(limit), 200))
    if requested and requested not in _PROVIDER_NAMES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"provider must be one of: {', '.join(sorted(_PROVIDER_NAMES))}")

    providers: Dict[str, Any] = {}
    if not requested or requested == "opencode":
        binary = _find_binary("opencode")
        all_models = _opencode_models(binary) if binary else []
        free_models = [m for m in all_models if "free" in m.lower()]
        matched = free_models if free_only else all_models
        if model_filter:
            q = model_filter.lower().strip()
            matched = [m for m in matched if q in m.lower()]
        providers["opencode"] = {
            "available": bool(binary),
            "version": _version(binary),
            "model_count": len(all_models),
            "matched_count": len(matched),
            "models": matched[:limit],
            "models_truncated": len(matched) > limit,
            "free_models": free_models[:50],
            "reasoning": "Pass a model-supported OpenCode --variant value such as minimal/low/medium/high/max.",
        }
    if not requested or requested == "codex":
        binary = _find_binary("codex")
        models, default_model, default_reasoning = _codex_known_models()
        providers["codex"] = {
            "available": bool(binary),
            "version": _version(binary),
            "models": models,
            "default_model": default_model,
            "default_reasoning": default_reasoning,
            "reasoning_values": ["minimal", "low", "medium", "high", "max"],
        }
    return {"ok": True, "providers": providers}


def _resolve_cwd(cwd: Optional[str]) -> Path:
    workdir = Path(cwd).expanduser().resolve() if cwd else Path.home().resolve()
    if not workdir.exists() or not workdir.is_dir():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"cwd does not exist or is not a directory: {workdir}")
    return workdir


def _handoff_instruction(result_style: str) -> str:
    if result_style == "detailed":
        return (
            "When the work is finished, give the parent AI a clean handoff. Do not narrate routine tool/file steps. "
            "Include verified findings/results, important evidence, blockers or caveats, and the next useful action. "
            "Keep it focused; do not dump raw logs unless they are necessary."
        )
    return (
        "When the work is finished, give the parent AI a concise handoff only. Do not narrate routine tool/file steps "
        "or your thinking process. Include only verified findings/results, material numbers or changes, important caveats, "
        "and the next useful action. Aim for roughly 250 words or less unless the task itself requires more."
    )


def _public_meta(agent_id: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    now = float(meta.get("ended_at") or _now())
    started = float(meta.get("started_at") or now)
    return {
        "agent_id": agent_id,
        "status": meta.get("status"),
        "title": meta.get("title"),
        "provider": meta.get("provider"),
        "model": meta.get("model"),
        "reasoning": meta.get("reasoning"),
        "cwd": meta.get("cwd"),
        "access_mode": meta.get("access_mode"),
        "started_at": meta.get("started_at"),
        "ended_at": meta.get("ended_at"),
        "duration_ms": int(max(0.0, now - started) * 1000),
        "session_id": meta.get("session_id"),
        "parent_agent_id": meta.get("parent_agent_id"),
        "attempt": meta.get("attempt", 1),
        "exit_code": meta.get("exit_code"),
        "note": meta.get("note"),
        "usage": meta.get("usage"),
    }


def _normalize(agent_id: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    if meta.get("status") in {"starting", "running"}:
        worker_pid = meta.get("worker_pid")
        if worker_pid and not _is_pid_alive(worker_pid):
            provider_pid = meta.get("provider_pid")
            if provider_pid and _is_pid_alive(provider_pid):
                _kill_group(provider_pid, signal_module.SIGTERM)
            meta.update({
                "status": "failed",
                "ended_at": _now(),
                "updated_at": _now(),
                "note": "Agent worker exited before recording a terminal result.",
            })
            _write_meta(agent_id, meta)
    return meta


def _spawn_internal(
    settings: Settings,
    provider: str,
    prompt: str,
    model: Optional[str],
    reasoning: Optional[str],
    cwd: Optional[str],
    timeout_s: Optional[int],
    title: Optional[str],
    result_style: str,
    access_mode: str,
    parent_agent_id: Optional[str] = None,
    resume_session_id: Optional[str] = None,
    attempt: int = 1,
) -> Dict[str, Any]:
    provider = provider.lower().strip()
    if provider not in _PROVIDER_NAMES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"provider must be one of: {', '.join(sorted(_PROVIDER_NAMES))}")
    binary = _find_binary(provider)
    if not binary:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"{provider} CLI is not installed or not executable.")
    if not prompt or not prompt.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "prompt is required.")
    if result_style not in _RESULT_STYLES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"result_style must be one of: {', '.join(sorted(_RESULT_STYLES))}")
    if access_mode not in _ACCESS_MODES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"access_mode must be one of: {', '.join(sorted(_ACCESS_MODES))}")

    workdir = _resolve_cwd(cwd)
    effective_timeout = min(max(10, int(timeout_s or DEFAULT_AGENT_TIMEOUT_S)), MAX_AGENT_TIMEOUT_S)
    agent_id = "agt_" + uuid.uuid4().hex[:10]
    path = _agent_dir(agent_id)
    path.mkdir(parents=True, exist_ok=False)
    user_prompt = prompt.strip()
    effective_prompt = user_prompt + "\n\n" + _handoff_instruction(result_style)
    (path / "prompt.txt").write_text(user_prompt, encoding="utf-8")
    (path / "effective_prompt.txt").write_text(effective_prompt, encoding="utf-8")
    (path / "stdout.log").touch()
    (path / "stderr.log").touch()
    (path / "worker.log").touch()
    (path / "result.txt").touch()

    started = _now()
    meta: Dict[str, Any] = {
        "agent_id": agent_id,
        "title": (title or user_prompt.splitlines()[0][:100]).strip(),
        "provider": provider,
        "binary": binary,
        "model": model,
        "reasoning": reasoning,
        "cwd": str(workdir),
        "access_mode": access_mode,
        "result_style": result_style,
        "timeout_s": effective_timeout,
        "status": "starting",
        "worker_pid": None,
        "provider_pid": None,
        "session_id": None,
        "resume_session_id": resume_session_id,
        "parent_agent_id": parent_agent_id,
        "attempt": attempt,
        "exit_code": None,
        "started_at": started,
        "updated_at": started,
        "ended_at": None,
    }
    _write_meta(agent_id, meta)

    worker_log = (path / "worker.log").open("a", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "mcp_server.tools_agents", "--worker", agent_id],
            cwd=str(BASE_DIR.parent),
            env=_base_env(),
            stdin=subprocess.DEVNULL,
            stdout=worker_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )
    except OSError as exc:
        worker_log.close()
        meta.update({"status": "failed", "ended_at": _now(), "updated_at": _now(), "note": str(exc)})
        _write_meta(agent_id, meta)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Could not start agent worker: {exc}") from exc
    worker_log.close()
    with _WORKERS_LOCK:
        _WORKERS[agent_id] = proc
    threading.Thread(target=_reap_worker, args=(agent_id, proc), daemon=True).start()
    meta.update({"worker_pid": proc.pid, "status": "running", "updated_at": _now()})
    _write_meta(agent_id, meta)
    return {"ok": True, **_public_meta(agent_id, meta)}


def spawn_agent(
    settings: Settings,
    provider: str,
    prompt: str,
    model: Optional[str] = None,
    reasoning: Optional[str] = None,
    cwd: Optional[str] = None,
    timeout_s: Optional[int] = None,
    title: Optional[str] = None,
    result_style: str = "concise",
    access_mode: str = "workspace_write",
) -> Dict[str, Any]:
    return _spawn_internal(
        settings, provider, prompt, model, reasoning, cwd, timeout_s, title,
        result_style, access_mode,
    )


def list_agents(settings: Settings, status_filter: Optional[str] = None, limit: int = 20) -> Dict[str, Any]:
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    items: List[Dict[str, Any]] = []
    for path in sorted(AGENTS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not path.is_dir() or not (path / "meta.json").exists():
            continue
        meta = _normalize(path.name, _read_meta(path.name))
        if status_filter and meta.get("status") != status_filter:
            continue
        public = _public_meta(path.name, meta)
        result_path = path / "result.txt"
        if meta.get("status") == "completed" and result_path.exists():
            result = result_path.read_text(encoding="utf-8", errors="replace").strip()
            public["result_preview"] = result[:300]
        items.append(public)
        if len(items) >= max(1, min(int(limit), 200)):
            break
    return {"ok": True, "count": len(items), "agents": items}


def get_agent(
    settings: Settings,
    agent_id: str,
    include_logs: bool = False,
    tail_lines: int = 40,
) -> Dict[str, Any]:
    meta = _normalize(agent_id, _read_meta(agent_id))
    result: Dict[str, Any] = {"ok": True, **_public_meta(agent_id, meta)}
    result_path = _agent_dir(agent_id) / "result.txt"
    if meta.get("status") in TERMINAL_STATUSES and result_path.exists():
        text = result_path.read_text(encoding="utf-8", errors="replace").strip()
        limit = DETAILED_RESULT_LIMIT if meta.get("result_style") == "detailed" else DEFAULT_RESULT_LIMIT
        result["result"] = truncate(text, limit)[0]
    if include_logs:
        result["logs"] = {
            "stdout": _tail_text(_agent_dir(agent_id) / "stdout.log", tail_lines),
            "stderr": _tail_text(_agent_dir(agent_id) / "stderr.log", tail_lines),
            "worker": _tail_text(_agent_dir(agent_id) / "worker.log", tail_lines),
        }
    return result


def agent_action(
    settings: Settings,
    agent_id: str,
    action: str,
    message: Optional[str] = None,
    signal: str = "TERM",
) -> Dict[str, Any]:
    action = action.lower().strip()
    if action not in {"cancel", "message", "retry", "despawn"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "action must be cancel, message, retry, or despawn.")
    meta = _normalize(agent_id, _read_meta(agent_id))

    if action == "cancel":
        if meta.get("status") in TERMINAL_STATUSES:
            return {"ok": True, **_public_meta(agent_id, meta), "message": "Agent is already finished."}
        sig_name = signal.upper()
        allowed = {"TERM": signal_module.SIGTERM, "KILL": signal_module.SIGKILL, "INT": signal_module.SIGINT}
        if sig_name not in allowed:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "signal must be TERM, KILL, or INT.")
        _kill_group(meta.get("provider_pid"), allowed[sig_name])
        _kill_group(meta.get("worker_pid"), allowed[sig_name])
        with _WORKERS_LOCK:
            worker_proc = _WORKERS.get(agent_id)
        if worker_proc is not None:
            try:
                worker_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                _kill_group(meta.get("worker_pid"), signal_module.SIGKILL)
                try:
                    worker_proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        meta.update({"status": "cancelled", "ended_at": _now(), "updated_at": _now(), "note": f"Cancelled with {sig_name}."})
        _write_meta(agent_id, meta)
        return {"ok": True, **_public_meta(agent_id, meta)}

    if action == "despawn":
        if meta.get("status") not in TERMINAL_STATUSES:
            raise HTTPException(status.HTTP_409_CONFLICT, "Cancel a running agent before despawn.")
        shutil.rmtree(_agent_dir(agent_id))
        return {"ok": True, "agent_id": agent_id, "status": "despawned"}

    original_prompt = (_agent_dir(agent_id) / "prompt.txt").read_text(encoding="utf-8", errors="replace")
    if action == "retry":
        return _spawn_internal(
            settings=settings,
            provider=meta["provider"],
            prompt=original_prompt,
            model=meta.get("model"),
            reasoning=meta.get("reasoning"),
            cwd=meta.get("cwd"),
            timeout_s=meta.get("timeout_s"),
            title=f"Retry: {meta.get('title') or agent_id}",
            result_style=meta.get("result_style", "concise"),
            access_mode=meta.get("access_mode", "workspace_write"),
            parent_agent_id=agent_id,
            attempt=int(meta.get("attempt", 1)) + 1,
        )

    if not message or not message.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "message is required for action=message.")
    if meta.get("status") not in TERMINAL_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, "Wait for or cancel the current agent before sending a follow-up.")
    session_id = meta.get("session_id")
    if not session_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "This agent has no resumable provider session_id.")
    return _spawn_internal(
        settings=settings,
        provider=meta["provider"],
        prompt=message.strip(),
        model=meta.get("model"),
        reasoning=meta.get("reasoning"),
        cwd=meta.get("cwd"),
        timeout_s=meta.get("timeout_s"),
        title=f"Follow-up: {meta.get('title') or agent_id}",
        result_style=meta.get("result_style", "concise"),
        access_mode=meta.get("access_mode", "workspace_write"),
        parent_agent_id=agent_id,
        resume_session_id=session_id,
        attempt=int(meta.get("attempt", 1)) + 1,
    )


def _extract_opencode(path: Path) -> Tuple[str, Optional[str], Optional[Dict[str, Any]]]:
    current: List[str] = []
    candidate = ""
    final = ""
    session_id: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None
    if not path.exists():
        return "", None, None
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        session_id = session_id or event.get("sessionID") or (event.get("part") or {}).get("sessionID")
        event_type = event.get("type")
        part = event.get("part") or {}
        if event_type == "step_start":
            current = []
        elif event_type == "text":
            text = part.get("text")
            if text:
                current.append(str(text))
        elif event_type == "step_finish":
            if current:
                candidate = "".join(current).strip()
            if part.get("reason") == "stop" and candidate:
                final = candidate
            if isinstance(part.get("tokens"), dict):
                usage = part["tokens"]
    return (final or candidate).strip(), session_id, usage


def _find_key_recursive(value: Any, keys: set) -> Optional[Any]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keys and item:
                return item
            found = _find_key_recursive(item, keys)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_key_recursive(item, keys)
            if found:
                return found
    return None


def _extract_codex_session(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        found = _find_key_recursive(event, {"thread_id", "session_id", "sessionID"})
        if found:
            return str(found)
    return None


def _build_provider_command(meta: Dict[str, Any], prompt: str, result_path: Path) -> List[str]:
    provider = meta["provider"]
    binary = meta["binary"]
    model = meta.get("model")
    reasoning = meta.get("reasoning")
    resume_session_id = meta.get("resume_session_id")

    if provider == "opencode":
        cmd = [binary, "run", "--format", "json", "--auto", "--dir", meta["cwd"]]
        if model:
            cmd += ["--model", model]
        if reasoning:
            cmd += ["--variant", reasoning]
        if resume_session_id:
            cmd += ["--session", resume_session_id]
        cmd.append(prompt)
        return cmd

    if resume_session_id:
        cmd = [binary, "exec", "resume", "--json", "--skip-git-repo-check", "-o", str(result_path)]
        if model:
            cmd += ["--model", model]
        if reasoning:
            cmd += ["--config", f'model_reasoning_effort="{reasoning}"']
        cmd += [resume_session_id, prompt]
        return cmd

    cmd = [
        binary, "exec", "--json", "--color", "never", "--skip-git-repo-check",
        "-C", meta["cwd"], "-o", str(result_path),
        "--config", 'approval_policy="never"',
    ]
    access_mode = meta.get("access_mode", "workspace_write")
    sandbox_map = {"read_only": "read-only", "workspace_write": "workspace-write", "full": "danger-full-access"}
    cmd += ["--sandbox", sandbox_map[access_mode]]
    if model:
        cmd += ["--model", model]
    if reasoning:
        cmd += ["--config", f'model_reasoning_effort="{reasoning}"']
    cmd.append(prompt)
    return cmd


def _worker(agent_id: str) -> int:
    meta = _read_meta(agent_id)
    path = _agent_dir(agent_id)
    prompt = (path / "effective_prompt.txt").read_text(encoding="utf-8", errors="replace")
    stdout_path = path / "stdout.log"
    stderr_path = path / "stderr.log"
    result_path = path / "result.txt"
    cmd = _build_provider_command(meta, prompt, result_path)
    meta.update({"status": "running", "updated_at": _now()})
    _write_meta(agent_id, meta)

    timed_out = False
    try:
        with stdout_path.open("w", encoding="utf-8") as stdout_file, stderr_path.open("w", encoding="utf-8") as stderr_file:
            proc = subprocess.Popen(
                cmd,
                cwd=meta["cwd"],
                env=_base_env(),
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                text=True,
                start_new_session=True,
            )
            meta = _read_meta(agent_id)
            meta.update({"provider_pid": proc.pid, "updated_at": _now()})
            _write_meta(agent_id, meta)
            try:
                exit_code = proc.wait(timeout=int(meta.get("timeout_s") or DEFAULT_AGENT_TIMEOUT_S))
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_group(proc.pid, signal_module.SIGTERM)
                try:
                    exit_code = proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    _kill_group(proc.pid, signal_module.SIGKILL)
                    exit_code = proc.wait()
    except Exception as exc:
        meta = _read_meta(agent_id)
        if meta.get("status") != "cancelled":
            meta.update({
                "status": "failed", "exit_code": None, "ended_at": _now(), "updated_at": _now(),
                "note": f"Agent worker error: {exc}",
            })
            result_path.write_text(f"Agent worker error: {exc}", encoding="utf-8")
            _write_meta(agent_id, meta)
        return 1

    meta = _read_meta(agent_id)
    if meta.get("status") == "cancelled":
        return 0

    session_id: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None
    result = ""
    if meta["provider"] == "opencode":
        result, session_id, usage = _extract_opencode(stdout_path)
    else:
        session_id = _extract_codex_session(stdout_path)
        if result_path.exists():
            result = result_path.read_text(encoding="utf-8", errors="replace").strip()

    if not result:
        error_tail = _tail_text(stderr_path, max_lines=30, max_chars=3000)
        if error_tail:
            result = error_tail
        else:
            result = "Agent finished without a final handoff. Check logs with get_agent(include_logs=true)."

    limit = DETAILED_RESULT_LIMIT if meta.get("result_style") == "detailed" else DEFAULT_RESULT_LIMIT
    result, was_truncated = truncate(result, limit)
    result_path.write_text(result, encoding="utf-8")
    final_status = "timeout" if timed_out else ("completed" if exit_code == 0 else "failed")
    meta.update({
        "status": final_status,
        "exit_code": exit_code,
        "ended_at": _now(),
        "updated_at": _now(),
        "session_id": session_id or meta.get("resume_session_id"),
        "usage": usage,
        "result_truncated": was_truncated,
        "result_chars": len(result),
    })
    if final_status != "completed":
        meta["note"] = f"Provider exited with code {exit_code}."
    _write_meta(agent_id, meta)
    return 0 if final_status == "completed" else 1


def _main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        return _worker(sys.argv[2])
    print("tools_agents is an internal module; use the MCP agent tools.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
