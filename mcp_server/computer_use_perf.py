from __future__ import annotations

import math
import threading
from collections import defaultdict, deque
from typing import Any, Deque, Dict

_MAX_SAMPLES = 64
_LOCK = threading.RLock()
_SAMPLES: Dict[str, Deque[Dict[str, int]]] = defaultdict(lambda: deque(maxlen=_MAX_SAMPLES))


def record_computer_use_sample(
    pipeline: str,
    *,
    duration_ms: int,
    payload_bytes: int = 0,
    remote_js_calls: int = 0,
    ax_traversals: int = 0,
) -> Dict[str, Any]:
    key = str(pipeline or "unknown").strip() or "unknown"
    sample = {
        "duration_ms": max(0, int(duration_ms or 0)),
        "payload_bytes": max(0, int(payload_bytes or 0)),
        "remote_js_calls": max(0, int(remote_js_calls or 0)),
        "ax_traversals": max(0, int(ax_traversals or 0)),
    }
    with _LOCK:
        rows = _SAMPLES[key]
        rows.append(sample)
        values = list(rows)
    durations = sorted(row["duration_ms"] for row in values)
    p95_index = max(0, min(len(durations) - 1, math.ceil(len(durations) * 0.95) - 1))
    count = len(values)
    return {
        "pipeline": key,
        "sample_count": count,
        "window_size": _MAX_SAMPLES,
        "p95_latency_ms": durations[p95_index],
        "avg_payload_bytes": round(sum(row["payload_bytes"] for row in values) / count, 1),
        "avg_remote_js_calls": round(sum(row["remote_js_calls"] for row in values) / count, 2),
        "avg_ax_traversals": round(sum(row["ax_traversals"] for row in values) / count, 2),
    }


def reset_computer_use_samples() -> None:
    with _LOCK:
        _SAMPLES.clear()
