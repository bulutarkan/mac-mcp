"""Check and repair the runtime virtual environment when its Python went away.

A Homebrew or macOS upgrade can remove the interpreter the runtime venv was
built from; every `mac-mcp` command (and the server's restart) then fails with
"bad interpreter". This script uses only the standard library and imports
nothing from mcp_server, so any working Python 3.10+ can run it:

    /opt/homebrew/bin/python3 ~/mac-mcp/mcp_server/venv_repair.py check
    /opt/homebrew/bin/python3 ~/mac-mcp/mcp_server/venv_repair.py repair

repair never rebuilds the active venv in place. It builds a new one beside it,
installs Mac MCP the way install.sh does and verifies it, then swaps it in,
moves the old one to .venv.previous-<timestamp> and verifies again; if that
fails, the old venv is put back.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

MIN_VERSION = (3, 10)
_PROBE = (
    "import json, platform, sys; "
    "print(json.dumps({'version': list(sys.version_info[:3]), 'machine': platform.machine(), "
    "'executable': sys.executable}))"
)
_IMPORT_CHECK = "import fastapi, uvicorn, mcp, httpx; import mcp_server.cli"


def _pyvenv_cfg(venv: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for line in (venv / "pyvenv.cfg").read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep:
                values[key.strip()] = value.strip()
    except OSError:
        pass
    return values


def _probe(python: Path, timeout: float = 30.0) -> dict | None:
    try:
        proc = subprocess.run([str(python), "-c", _PROBE], capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None


def inspect_venv(runtime: Path) -> dict:
    """Describe the runtime venv: ok, base_missing, broken, arch_mismatch, unsupported or missing."""
    venv = runtime / ".venv"
    cfg = _pyvenv_cfg(venv)
    python = venv / "bin" / "python"
    home = cfg.get("home", "")
    report = {
        "venv": str(venv),
        "home": home or None,
        "recorded_version": cfg.get("version") or cfg.get("version_info"),
        "pinned_to_versioned_path": "/Cellar/" in home,
        "host_machine": platform.machine(),
    }
    if not venv.is_dir():
        return {**report, "status": "missing"}
    if not os.path.exists(python):  # follows symlinks: false when the base interpreter is gone
        return {**report, "status": "base_missing"}
    info = _probe(python)
    if info is None:
        return {**report, "status": "broken"}
    report.update(version=".".join(str(part) for part in info["version"]), machine=info["machine"])
    if tuple(info["version"][:2]) < MIN_VERSION:
        return {**report, "status": "unsupported"}
    if info["machine"] != report["host_machine"]:
        return {**report, "status": "arch_mismatch"}
    return {**report, "status": "ok"}


def _candidates(preferred: str | None) -> list[Path]:
    found: list[Path] = []
    if preferred:
        found.append(Path(preferred))
    # Prefer Homebrew's stable opt/ links (they survive patch upgrades), newest first.
    found += sorted((Path(p) for p in glob.glob("/opt/homebrew/opt/python@3.*/bin/python3.*")
                     if not p.endswith("-config")), reverse=True, key=lambda p: _minor(p))
    found += [Path(p) for p in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3",
                                "/Library/Frameworks/Python.framework/Versions/Current/bin/python3")]
    if shutil.which("python3"):
        found.append(Path(shutil.which("python3")))
    unique: list[Path] = []
    for path in found:
        if path not in unique:
            unique.append(path)
    return unique


def _minor(path: Path) -> int:
    try:
        return int(path.name.rsplit(".", 1)[-1])
    except ValueError:
        return 0


def choose_interpreter(preferred: str | None = None) -> tuple[Path, dict]:
    host = platform.machine()
    for path in _candidates(preferred):
        if not os.path.exists(path):
            continue
        info = _probe(path)
        if info and tuple(info["version"][:2]) >= MIN_VERSION and info["machine"] == host:
            return path, info
    raise SystemExit(
        f"No working Python {MIN_VERSION[0]}.{MIN_VERSION[1]}+ for {host} was found. "
        "Install one (for example `brew install python`) and run this again."
    )


def _run(cmd: list[str], *, timeout: float = 900.0, cwd: Path | None = None) -> None:
    print("+ " + " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, timeout=timeout, stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {cmd[0]} {' '.join(cmd[1:3])}")


def _verify(venv: Path, runtime: Path) -> None:
    python = venv / "bin" / "python"
    _run([str(python), "-m", "pip", "check"], timeout=120)
    _run([str(python), "-c", _IMPORT_CHECK], timeout=120, cwd=runtime)


def _retarget_scripts(venv: Path, old: Path) -> int:
    """Point console scripts and activate files built under ``old`` at ``venv``."""
    old_text, new_text = str(old), str(venv)
    changed = 0
    for path in (venv / "bin").iterdir():
        if path.is_symlink() or not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:1024] or old_text.encode() not in data:
            continue
        mode = path.stat().st_mode
        path.write_bytes(data.replace(old_text.encode(), new_text.encode()))
        os.chmod(path, mode)
        changed += 1
    return changed


def repair(runtime: Path, *, python: str | None = None, force: bool = False) -> int:
    state = inspect_venv(runtime)
    if state["status"] == "ok" and not force:
        print(f"The runtime venv is healthy (Python {state.get('version')}); nothing to repair.")
        return 0
    interpreter, info = choose_interpreter(python)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    venv = runtime / ".venv"
    staged = runtime / f".venv.repair-{stamp}"
    previous = runtime / f".venv.previous-{stamp}"
    print(f"Runtime venv: {state['status']}. Rebuilding with {interpreter} "
          f"(Python {'.'.join(map(str, info['version']))}).", flush=True)
    try:
        _run([str(interpreter), "-m", "venv", str(staged)], timeout=300)
        _run([str(staged / "bin" / "python"), "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
              "--upgrade", "pip", "setuptools", "wheel"])
        _run([str(staged / "bin" / "python"), "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
              "-e", str(runtime)])
        _verify(staged, runtime)
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        shutil.rmtree(staged, ignore_errors=True)
        print(f"Repair stopped before touching the active venv: {exc}", file=sys.stderr)
        return 1

    moved = False
    try:
        if venv.exists() or venv.is_symlink():
            os.replace(venv, previous)
            moved = True
        os.replace(staged, venv)
        _retarget_scripts(venv, staged)
        _verify(venv, runtime)
        _run([str(venv / "bin" / "mac-mcp"), "--version"], timeout=60)
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        failed = runtime / f".venv.failed-{stamp}"
        if venv.exists():
            os.replace(venv, failed)
        if moved:
            os.replace(previous, venv)
        print(f"The rebuilt venv failed verification and the previous one was restored: {exc}", file=sys.stderr)
        print(f"The failed build is kept at {failed} for inspection.", file=sys.stderr)
        return 1
    print(f"Runtime venv rebuilt and verified. The previous venv is kept at {previous}.")
    print("Start Mac MCP again with: mac-mcp start")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["check", "repair"])
    parser.add_argument("--runtime", default=os.getenv("MAC_MCP_RUNTIME_DIR", str(Path.home() / "mac-mcp")))
    parser.add_argument("--python", help="Interpreter to rebuild with (default: newest Homebrew Python).")
    parser.add_argument("--force", action="store_true", help="Rebuild even when the venv looks healthy.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    runtime = Path(args.runtime).expanduser()
    if args.command == "check":
        report = inspect_venv(runtime)
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(f"runtime venv: {report['status']} ({report['venv']})")
        return 0 if report["status"] == "ok" else 1
    return repair(runtime, python=args.python, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
