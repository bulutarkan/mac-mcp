from __future__ import annotations

import json
import os
import select
import sys
import warnings
from contextlib import redirect_stdout
from pathlib import Path

MODEL = os.getenv("MAC_MCP_MEMORY_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
DIMS = int(os.getenv("MAC_MCP_MEMORY_MODEL_DIMS", "384"))
CACHE = Path(os.getenv("MAC_MCP_MEMORY_MODEL_CACHE", str(Path.home() / ".mac-mcp" / "cache" / "fastembed"))).expanduser().resolve()
try:
    IDLE_SECONDS = max(0.0, min(float(os.getenv("MAC_MCP_MEMORY_MODEL_IDLE_SECONDS", "60")), 3600.0))
except ValueError:
    IDLE_SECONDS = 60.0


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def process_line(model, line: str) -> bool:
    try:
        payload = json.loads(line)
        texts = payload.get("texts") if isinstance(payload, dict) else None
        if not isinstance(texts, list) or not texts:
            emit({"ok": False, "error": "texts must be a non-empty list"})
            return True
        with redirect_stdout(sys.stderr):
            vectors = list(model.embed([str(text) for text in texts]))
        values = [vector.tolist() if hasattr(vector, "tolist") else list(vector) for vector in vectors]
        if len(values) != len(texts) or any(len(vector) != DIMS for vector in values):
            emit({"ok": False, "error": "unexpected embedding dimensions"})
            return True
        emit({"ok": True, "dimensions": DIMS, "vectors": values})
        return True
    except Exception as exc:
        emit({"ok": False, "error": str(exc)[:300]})
        return True


def main() -> int:
    CACHE.mkdir(parents=True, exist_ok=True)
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

    first = sys.stdin.readline()
    if not first:
        return 0
    process_line(model, first)
    if IDLE_SECONDS <= 0:
        return 0

    while True:
        ready, _, _ = select.select([sys.stdin], [], [], IDLE_SECONDS)
        if not ready:
            return 0
        line = sys.stdin.readline()
        if not line:
            return 0
        process_line(model, line)


if __name__ == "__main__":
    raise SystemExit(main())
