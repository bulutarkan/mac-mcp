from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


RECORD_VERSION = 1


@dataclass(frozen=True)
class ProcessSnapshot:
    pid: int
    start_time: str
    executable: str
    command: str
    cwd: str | None

    @property
    def command_sha256(self) -> str:
        return hashlib.sha256(self.command.encode("utf-8", "surrogatepass")).hexdigest()


@dataclass(frozen=True)
class ProcessRecord:
    pid: int
    role: str | None
    start_time: str | None
    executable: str | None
    command_sha256: str | None
    cwd: str | None
    metadata: dict[str, Any]
    format: str


@dataclass(frozen=True)
class ProcessValidation:
    status: str
    pid: int | None
    role: str
    record_format: str | None
    reason: str
    snapshot: ProcessSnapshot | None = None

    @property
    def valid(self) -> bool:
        return self.status == "valid"

    @property
    def legacy_match(self) -> bool:
        return self.status == "legacy_match"

    @property
    def safe_to_remove_record(self) -> bool:
        return self.status in {"dead", "invalid_record", "identity_mismatch", "role_mismatch"}


def pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (TypeError, ValueError, OSError):
        return False


def _run_text(command: list[str], timeout: float = 3.0) -> str | None:
    try:
        proc = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip()


def process_snapshot(pid: int) -> ProcessSnapshot | None:
    if pid <= 0 or not pid_alive(pid):
        return None

    start_time = _run_text(["/bin/ps", "-p", str(pid), "-o", "lstart="])
    executable = _run_text(["/bin/ps", "-p", str(pid), "-o", "comm="])
    command = _run_text(["/bin/ps", "-p", str(pid), "-o", "command="])
    if not start_time or not executable or not command:
        return None

    cwd: str | None = None
    lsof = shutil.which("lsof") or "/usr/sbin/lsof"
    if Path(lsof).exists():
        raw = _run_text([lsof, "-a", "-p", str(pid), "-d", "cwd", "-Fn"])
        if raw:
            cwd = next(
                (line[1:] for line in raw.splitlines() if line.startswith("n") and len(line) > 1),
                None,
            )

    return ProcessSnapshot(
        pid=int(pid),
        start_time=start_time.strip(),
        executable=executable.strip(),
        command=command.strip(),
        cwd=cwd,
    )


def listener_pids(port: int) -> list[int]:
    lsof = shutil.which("lsof") or "/usr/sbin/lsof"
    if not Path(lsof).exists():
        return []
    try:
        probe = subprocess.run(
            [lsof, f"-tiTCP:{int(port)}", "-sTCP:LISTEN"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    if probe.returncode not in {0, 1}:
        return []
    out: list[int] = []
    for raw in (probe.stdout or "").splitlines():
        try:
            pid = int(raw.strip())
        except ValueError:
            continue
        if pid > 0 and pid_alive(pid) and pid not in out:
            out.append(pid)
    return out


def _tokens(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _flag_value(tokens: list[str], flag: str) -> str | None:
    for index, token in enumerate(tokens):
        if token == flag and index + 1 < len(tokens):
            return tokens[index + 1]
        prefix = flag + "="
        if token.startswith(prefix):
            return token[len(prefix):]
    return None


def _same_path(left: str | None, right: Path | str | None) -> bool:
    if not left or right is None:
        return False
    try:
        return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()
    except OSError:
        return os.path.abspath(os.path.expanduser(left)) == os.path.abspath(os.path.expanduser(str(right)))


def matches_role(
    snapshot: ProcessSnapshot,
    role: str,
    *,
    port: int | None = None,
    project_root: Path | str | None = None,
    binary: Path | str | None = None,
) -> bool:
    tokens = _tokens(snapshot.command)
    basename = Path(snapshot.executable).name.lower()
    role = str(role or "").strip().lower()

    if binary is not None and not _same_path(snapshot.executable, binary):
        return False

    if role == "server":
        if "uvicorn" not in tokens or "mcp_server.main:app" not in tokens:
            return False
        if port is not None and _flag_value(tokens, "--port") != str(int(port)):
            return False
        if project_root is not None and not _same_path(snapshot.cwd, project_root):
            return False
        return True

    if role == "ngrok":
        if basename != "ngrok":
            return False
        if len(tokens) < 2 or "http" not in tokens[1:3]:
            return False
        if port is not None and str(int(port)) not in tokens:
            return False
        return True

    if role == "cloudflared":
        if basename != "cloudflared":
            return False
        if "tunnel" not in tokens or "run" not in tokens:
            return False
        if port is not None:
            expected = f"http://127.0.0.1:{int(port)}"
            if _flag_value(tokens, "--url") != expected:
                return False
        return True

    return False


def read_process_record(path: Path) -> ProcessRecord | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None

    try:
        legacy_pid = int(raw)
    except ValueError:
        legacy_pid = None
    if legacy_pid is not None:
        if legacy_pid <= 0:
            return None
        return ProcessRecord(
            pid=legacy_pid,
            role=None,
            start_time=None,
            executable=None,
            command_sha256=None,
            cwd=None,
            metadata={},
            format="legacy",
        )

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return ProcessRecord(
            pid=0,
            role=None,
            start_time=None,
            executable=None,
            command_sha256=None,
            cwd=None,
            metadata={},
            format="invalid",
        )
    if not isinstance(payload, dict) or payload.get("version") != RECORD_VERSION:
        return ProcessRecord(
            pid=0,
            role=None,
            start_time=None,
            executable=None,
            command_sha256=None,
            cwd=None,
            metadata={},
            format="invalid",
        )

    try:
        pid = int(payload.get("pid"))
    except (TypeError, ValueError):
        pid = 0
    metadata = payload.get("metadata")
    return ProcessRecord(
        pid=pid,
        role=str(payload.get("role") or "") or None,
        start_time=str(payload.get("start_time") or "") or None,
        executable=str(payload.get("executable") or "") or None,
        command_sha256=str(payload.get("command_sha256") or "") or None,
        cwd=str(payload.get("cwd") or "") or None,
        metadata=metadata if isinstance(metadata, dict) else {},
        format="json",
    )


def read_pid(path: Path) -> int | None:
    record = read_process_record(path)
    if record is None or record.pid <= 0:
        return None
    return record.pid


def write_process_record(
    path: Path,
    role: str,
    pid: int,
    *,
    metadata: dict[str, Any] | None = None,
    snapshot: ProcessSnapshot | None = None,
) -> ProcessRecord:
    snap = snapshot or process_snapshot(pid)
    if snap is None:
        raise RuntimeError(f"Could not inspect process {pid} before recording ownership.")

    safe_metadata: dict[str, Any] = {}
    for key, value in (metadata or {}).items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe_metadata[str(key)] = value

    payload = {
        "version": RECORD_VERSION,
        "pid": int(pid),
        "role": str(role),
        "start_time": snap.start_time,
        "executable": snap.executable,
        "command_sha256": snap.command_sha256,
        "cwd": snap.cwd,
        "metadata": safe_metadata,
    }

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    return read_process_record(path)  # type: ignore[return-value]


def validate_process_record(
    path: Path,
    role: str,
    *,
    port: int | None = None,
    project_root: Path | str | None = None,
    binary: Path | str | None = None,
) -> ProcessValidation:
    record = read_process_record(path)
    if record is None:
        return ProcessValidation("missing", None, role, None, "record_missing")
    if record.format == "invalid" or record.pid <= 0:
        return ProcessValidation("invalid_record", None, role, record.format, "record_invalid")

    pid = record.pid
    if not pid_alive(pid):
        return ProcessValidation("dead", pid, role, record.format, "process_not_alive")

    snap = process_snapshot(pid)
    if snap is None:
        return ProcessValidation("unverifiable", pid, role, record.format, "process_metadata_unavailable")

    if not matches_role(snap, role, port=port, project_root=project_root, binary=binary):
        return ProcessValidation("role_mismatch", pid, role, record.format, "process_role_mismatch", snap)

    if record.format == "legacy":
        return ProcessValidation("legacy_match", pid, role, record.format, "legacy_role_match", snap)

    if record.role != role:
        return ProcessValidation("identity_mismatch", pid, role, record.format, "record_role_mismatch", snap)

    if (
        record.start_time != snap.start_time
        or record.executable != snap.executable
        or record.command_sha256 != snap.command_sha256
        or (record.cwd is not None and record.cwd != snap.cwd)
    ):
        return ProcessValidation("identity_mismatch", pid, role, record.format, "process_fingerprint_mismatch", snap)

    return ProcessValidation("valid", pid, role, record.format, "record_matches_process", snap)


def migrate_legacy_record(
    path: Path,
    role: str,
    *,
    port: int | None = None,
    project_root: Path | str | None = None,
    binary: Path | str | None = None,
    metadata: dict[str, Any] | None = None,
) -> ProcessValidation:
    validation = validate_process_record(
        path,
        role,
        port=port,
        project_root=project_root,
        binary=binary,
    )
    if not validation.legacy_match or validation.pid is None or validation.snapshot is None:
        return validation
    write_process_record(
        path,
        role,
        validation.pid,
        metadata=metadata,
        snapshot=validation.snapshot,
    )
    return validate_process_record(
        path,
        role,
        port=port,
        project_root=project_root,
        binary=binary,
    )


def record_mode(path: Path) -> int | None:
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None
