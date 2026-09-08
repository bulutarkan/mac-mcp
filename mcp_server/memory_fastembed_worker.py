from __future__ import annotations

# Backward-compatible entrypoint. Memory and Agent Skills now share embedding_worker.py.
try:
    from .embedding_worker import main
except ImportError:  # direct script execution
    from embedding_worker import main

if __name__ == "__main__":
    raise SystemExit(main())
