from __future__ import annotations

import ctypes
import json
import os
import select
import sys
import time
import warnings
from contextlib import redirect_stdout
from pathlib import Path
from typing import Optional

MODEL = os.getenv(
    "MAC_MCP_EMBEDDING_MODEL",
    os.getenv("MAC_MCP_MEMORY_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"),
)
DIMS = int(os.getenv("MAC_MCP_EMBEDDING_MODEL_DIMS", os.getenv("MAC_MCP_MEMORY_MODEL_DIMS", "384")))
CACHE = Path(
    os.getenv(
        "MAC_MCP_EMBEDDING_MODEL_CACHE",
        os.getenv("MAC_MCP_MEMORY_MODEL_CACHE", str(Path.home() / ".mac-mcp" / "cache" / "fastembed")),
    )
).expanduser().resolve()
try:
    IDLE_SECONDS = max(
        0.0,
        min(
            float(os.getenv("MAC_MCP_EMBEDDING_IDLE_SECONDS", os.getenv("MAC_MCP_MEMORY_MODEL_IDLE_SECONDS", "60"))),
            3600.0,
        ),
    )
except ValueError:
    IDLE_SECONDS = 60.0
try:
    BUSY_IDLE_SECONDS = max(
        IDLE_SECONDS, min(float(os.getenv("MAC_MCP_EMBEDDING_BUSY_IDLE_SECONDS", "180")), 3600.0)
    )
except ValueError:
    BUSY_IDLE_SECONDS = max(IDLE_SECONDS, 180.0)

# The loaded model costs about 670 MB of physical memory on an M3 and reloads in
# about half a second, so the worker stays only while searches keep coming: a
# burst (BURST_REQUESTS within BURST_WINDOW_S) keeps it for BUSY_IDLE_SECONDS,
# otherwise IDLE_SECONDS. While idle it checks memory pressure at least every
# PRESSURE_CHECK_S and leaves as soon as macOS reports warn or critical.
BURST_REQUESTS = 3
BURST_WINDOW_S = 120.0
PRESSURE_CHECK_S = 5.0
PRESSURE_WARN = 2  # kern.memorystatus_vm_pressure_level: 1 normal, 2 warn, 4 critical
EXIT_IDLE = 0
EXIT_MEMORY_PRESSURE = 3


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def memory_pressure_level() -> int:
    try:
        libc = ctypes.CDLL(None)
        value = ctypes.c_int(0)
        size = ctypes.c_size_t(ctypes.sizeof(value))
        if libc.sysctlbyname(b"kern.memorystatus_vm_pressure_level", ctypes.byref(value), ctypes.byref(size), None, 0) == 0:
            return int(value.value)
    except Exception:
        pass
    return 1


def idle_limit(served: list, now: float) -> float:
    recent = [at for at in served if now - at <= BURST_WINDOW_S]
    return BUSY_IDLE_SECONDS if len(recent) >= BURST_REQUESTS else IDLE_SECONDS


def next_line(stream, served: list, *, pressure=None, clock=time.monotonic):
    """The next request line, or the exit code once the worker should leave."""
    pressure = pressure or memory_pressure_level
    idle_since = clock()
    while True:
        now = clock()
        remaining = idle_limit(served, now) - (now - idle_since)
        if remaining <= 0:
            return EXIT_IDLE
        if pressure() >= PRESSURE_WARN:
            return EXIT_MEMORY_PRESSURE
        ready, _, _ = select.select([stream], [], [], min(remaining, PRESSURE_CHECK_S))
        if ready:
            line = stream.readline()
            return line if line else EXIT_IDLE


def process_line(model, line: str, extra: Optional[dict] = None) -> None:
    try:
        payload = json.loads(line)
        texts = payload.get("texts") if isinstance(payload, dict) else None
        if not isinstance(texts, list) or not texts:
            emit({"ok": False, "error": "texts must be a non-empty list"})
            return
        started = time.perf_counter()
        with redirect_stdout(sys.stderr):
            vectors = list(model.embed([str(text) for text in texts]))
        embed_ms = round((time.perf_counter() - started) * 1000, 1)
        values = [vector.tolist() if hasattr(vector, "tolist") else list(vector) for vector in vectors]
        if len(values) != len(texts) or any(len(vector) != DIMS for vector in values):
            emit({"ok": False, "error": "unexpected embedding dimensions"})
            return
        emit({"ok": True, "dimensions": DIMS, "vectors": values, "embed_ms": embed_ms, **(extra or {})})
    except Exception as exc:
        emit({"ok": False, "error": str(exc)[:300]})


def main() -> int:
    CACHE.mkdir(parents=True, exist_ok=True)
    load_started = time.perf_counter()
    try:
        with redirect_stdout(sys.stderr), warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=r"The model .* now uses mean pooling instead of CLS embedding.*")
            from fastembed import TextEmbedding
            model = TextEmbedding(
                model_name=MODEL,
                cache_dir=str(CACHE),
                threads=max(1, min(4, os.cpu_count() or 1)),
                local_files_only=False,
            )
    except Exception as exc:
        emit({"ok": False, "error": f"model load failed: {str(exc)[:300]}"})
        return 2

    load_ms = round((time.perf_counter() - load_started) * 1000, 1)
    first = sys.stdin.readline()
    if not first:
        return 0
    process_line(model, first, {"load_ms": load_ms})
    if IDLE_SECONDS <= 0:
        return 0

    served = [time.monotonic()]
    while True:
        line = next_line(sys.stdin, served)
        if isinstance(line, int):
            return line
        process_line(model, line)
        served = [at for at in served if time.monotonic() - at <= BURST_WINDOW_S] + [time.monotonic()]


if __name__ == "__main__":
    raise SystemExit(main())
