"""Turning on SQLite WAL when several processes may open a new database file at once.

SQLite does not wait on its busy timeout for the switch to WAL: two processes that open
the same brand-new file together can get "database is locked" at once (agents of a team
start together, each in its own worker process). WAL is stored in the file, so a database
that is already in WAL is left alone and the switch is retried briefly only while the
file is new.
"""
from __future__ import annotations

import sqlite3
import time

ATTEMPTS = 40
DELAY_S = 0.05


def enable_wal(conn: sqlite3.Connection) -> None:
    for attempt in range(ATTEMPTS):
        try:
            if str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal":
                return
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == ATTEMPTS - 1:
                raise
            time.sleep(DELAY_S)
