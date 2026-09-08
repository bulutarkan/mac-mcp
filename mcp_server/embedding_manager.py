from __future__ import annotations

import hashlib
import json
import math
import os
import re
import select
import subprocess
import sys
import threading
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

FEATURE_VECTOR_DIMS = 256
FEATURE_VECTOR_BACKEND = "feature_hash_v1"
MULTILINGUAL_VECTOR_BACKEND = "fastembed_multilingual_minilm_v1"
MULTILINGUAL_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
MULTILINGUAL_DIMS = 384
APPLE_VECTOR_BACKEND = "apple_nl_en_v1"

_WORKER: Optional[subprocess.Popen[str]] = None
_WORKER_CACHE: Optional[str] = None
WORKER_LOCK = threading.RLock()


def embedding_mode() -> str:
    return os.getenv(
        "MAC_MCP_EMBEDDING",
        os.getenv("MAC_MCP_MEMORY_EMBEDDING", "auto"),
    ).strip().lower()


def model_cache() -> Path:
    raw = os.getenv(
        "MAC_MCP_EMBEDDING_MODEL_CACHE",
        os.getenv("MAC_MCP_MEMORY_MODEL_CACHE", ""),
    ).strip()
    cache = Path(raw).expanduser() if raw else (Path.home() / ".mac-mcp" / "cache" / "fastembed")
    cache = cache.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    return cache


def idle_seconds() -> float:
    raw = os.getenv(
        "MAC_MCP_EMBEDDING_IDLE_SECONDS",
        os.getenv("MAC_MCP_MEMORY_MODEL_IDLE_SECONDS", "60"),
    ).strip()
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = 60.0
    return max(0.0, min(value, 3600.0))


def normalize(text: Any) -> str:
    value = unicodedata.normalize("NFKD", str(text or "")).casefold()
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.replace("ı", "i")
    return re.sub(r"[^a-z0-9çğıöşü]+", " ", value).strip()


def tokens(text: str) -> List[str]:
    return [token for token in normalize(text).split() if len(token) >= 2]


def feature_vector(text: str) -> List[float]:
    words = tokens(text)
    features = [(f"w:{token}", 1.0) for token in words]
    features += [(f"b:{words[i]}_{words[i + 1]}", 0.75) for i in range(len(words) - 1)]
    compact = "_".join(words)
    if len(compact) >= 3:
        features += [(f"c:{compact[i:i + 3]}", 0.18) for i in range(len(compact) - 2)]
    vec = [0.0] * FEATURE_VECTOR_DIMS
    for feature, weight in features:
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        raw = int.from_bytes(digest, "big")
        index = raw % FEATURE_VECTOR_DIMS
        sign = -1.0 if ((raw >> 8) & 1) else 1.0
        vec[index] += sign * weight
    length = math.sqrt(sum(v * v for v in vec))
    return [v / length for v in vec] if length else vec


def normalize_vector(vector: Sequence[float]) -> List[float]:
    values = [float(v) for v in vector]
    length = math.sqrt(sum(v * v for v in values))
    return [v / length for v in values] if length else values


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return max(0.0, min(1.0, sum(x * y for x, y in zip(a, b))))


def _close_worker_pipes(proc: subprocess.Popen[str]) -> None:
    for stream in (proc.stdin, proc.stdout):
        try:
            if stream is not None:
                stream.close()
        except Exception:
            pass


def discard_worker_locked(*, terminate: bool = False) -> None:
    global _WORKER, _WORKER_CACHE
    proc = _WORKER
    _WORKER = None
    _WORKER_CACHE = None
    if proc is None:
        return
    if terminate and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    _close_worker_pipes(proc)


def _live_worker_locked(cache_key: str) -> Optional[subprocess.Popen[str]]:
    proc = _WORKER
    if proc is None:
        return None
    if proc.poll() is not None or _WORKER_CACHE != cache_key:
        discard_worker_locked(terminate=proc.poll() is None)
        return None
    return proc


def _start_worker_locked() -> Optional[subprocess.Popen[str]]:
    global _WORKER, _WORKER_CACHE
    cache = model_cache()
    cache_key = str(cache)
    live = _live_worker_locked(cache_key)
    if live is not None:
        return live
    worker = Path(__file__).with_name("embedding_worker.py")
    if not worker.exists():
        return None
    env = os.environ.copy()
    env["MAC_MCP_EMBEDDING_MODEL_CACHE"] = cache_key
    env["MAC_MCP_EMBEDDING_IDLE_SECONDS"] = str(idle_seconds())
    env["MAC_MCP_EMBEDDING_MODEL"] = MULTILINGUAL_MODEL
    env["MAC_MCP_EMBEDDING_MODEL_DIMS"] = str(MULTILINGUAL_DIMS)
    env.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    try:
        proc = subprocess.Popen(
            [sys.executable, str(worker)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=env,
        )
    except Exception:
        return None
    _WORKER = proc
    _WORKER_CACHE = cache_key
    return proc


def worker_vectors(texts: Sequence[str], *, allow_start: bool) -> Optional[List[List[float]]]:
    if not texts:
        return []
    acquired = WORKER_LOCK.acquire(blocking=allow_start)
    if not acquired:
        return None
    try:
        cache_key = str(model_cache())
        proc = _live_worker_locked(cache_key)
        if proc is None:
            if not allow_start:
                return None
            proc = _start_worker_locked()
        if proc is None or proc.stdin is None or proc.stdout is None:
            return None

        for attempt in range(2 if allow_start else 1):
            try:
                proc.stdin.write(json.dumps({"texts": list(texts)}, ensure_ascii=False) + "\n")
                proc.stdin.flush()
                ready, _, _ = select.select([proc.stdout], [], [], 120.0 if allow_start else 20.0)
                if not ready:
                    discard_worker_locked(terminate=True)
                    return None
                line = proc.stdout.readline()
                if not line:
                    raise BrokenPipeError("Embedding worker exited before returning vectors.")
                payload = json.loads(line)
                vectors = payload.get("vectors") if isinstance(payload, dict) and payload.get("ok") else None
                if not isinstance(vectors, list) or len(vectors) != len(texts):
                    return None
                normalized = [normalize_vector(vector) for vector in vectors if isinstance(vector, list)]
                if len(normalized) != len(texts) or any(len(vector) != MULTILINGUAL_DIMS for vector in normalized):
                    return None
                return normalized
            except Exception:
                discard_worker_locked(terminate=True)
                if not allow_start or attempt > 0:
                    return None
                proc = _start_worker_locked()
                if proc is None or proc.stdin is None or proc.stdout is None:
                    return None
        return None
    finally:
        WORKER_LOCK.release()


def multilingual_vectors(texts: Sequence[str], *, allow_start: bool) -> Optional[List[List[float]]]:
    return worker_vectors(texts, allow_start=allow_start)



def apple_helper() -> Optional[Path]:
    mode = embedding_mode()
    if mode in {"feature", "feature_hash", "off", "disabled"}:
        return None
    swiftc = Path("/usr/bin/swiftc")
    source = Path(__file__).with_name("memory_embedding.swift")
    if not swiftc.exists() or not source.exists():
        return None
    cache = (Path.home() / ".mac-mcp" / "cache" / "apple-nl").resolve()
    cache.mkdir(parents=True, exist_ok=True)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    binary = cache / f"memory-embed-{source_hash}"
    if binary.exists() and os.access(binary, os.X_OK):
        return binary
    try:
        proc = subprocess.run(
            [str(swiftc), "-O", str(source), "-o", str(binary)],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            return None
        binary.chmod(0o700)
        return binary
    except Exception:
        return None


def apple_vectors(texts: Sequence[str]) -> Optional[List[List[float]]]:
    if not texts:
        return []
    helper = apple_helper()
    if helper is None:
        return None
    try:
        proc = subprocess.run(
            [str(helper)], input=json.dumps(list(texts), ensure_ascii=False),
            capture_output=True, text=True, timeout=20,
        )
        if proc.returncode != 0 or not proc.stdout:
            return None
        payload = json.loads(proc.stdout)
        vectors = payload.get("vectors") if isinstance(payload, dict) else None
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            return None
        normalized=[]
        for vector in vectors:
            if not isinstance(vector, list) or not vector:
                return None
            normalized.append(normalize_vector(vector))
        return normalized
    except Exception:
        return None


def semantic_vectors(texts: Sequence[str], *, allow_start: bool = False) -> Tuple[str, int, List[List[float]]]:
    mode = embedding_mode()
    if mode in {"auto", "multilingual", "fastembed"}:
        multilingual = multilingual_vectors(texts, allow_start=allow_start)
        if multilingual:
            return MULTILINGUAL_VECTOR_BACKEND, len(multilingual[0]), multilingual
    if mode in {"auto", "apple", "multilingual", "fastembed"}:
        apple = apple_vectors(texts)
        if apple:
            return APPLE_VECTOR_BACKEND, len(apple[0]), apple
    vectors = [feature_vector(text) for text in texts]
    return FEATURE_VECTOR_BACKEND, FEATURE_VECTOR_DIMS, vectors


def worker_status() -> Dict[str, Any]:
    with WORKER_LOCK:
        proc = _WORKER
        alive = bool(proc is not None and proc.poll() is None)
        return {
            "alive": alive,
            "pid": proc.pid if alive and proc else None,
            "cache": str(model_cache()),
            "idle_seconds": idle_seconds(),
            "model": MULTILINGUAL_MODEL,
        }
