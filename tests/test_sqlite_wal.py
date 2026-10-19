"""WAL is switched on once and the switch survives a brand-new file opened by two processes at once."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import sqlite_wal


class FlakyConnection:
    """Answers like SQLite while another process is switching the same new file to WAL."""

    def __init__(self, locked_times: int, mode: str = "delete") -> None:
        self.locked_times = locked_times
        self.mode = mode
        self.statements: list = []

    def execute(self, sql: str):
        self.statements.append(sql)
        if sql == "PRAGMA journal_mode=WAL":
            if self.locked_times:
                self.locked_times -= 1
                raise sqlite3.OperationalError("database is locked")
            self.mode = "wal"
        mode = self.mode

        class Row:
            def fetchone(self):
                return (mode,)

        return Row()


class EnableWalTests(unittest.TestCase):
    def test_a_new_file_is_switched_to_wal(self) -> None:
        path = Path(tempfile.mkdtemp()) / "new.sqlite3"
        conn = sqlite3.connect(path)
        sqlite_wal.enable_wal(conn)
        self.assertEqual("wal", conn.execute("PRAGMA journal_mode").fetchone()[0])
        conn.close()

    def test_a_locked_switch_is_retried(self) -> None:
        conn = FlakyConnection(locked_times=3)
        with patch.object(sqlite_wal.time, "sleep") as sleep:
            sqlite_wal.enable_wal(conn)
        self.assertEqual("wal", conn.mode)
        self.assertEqual(3, sleep.call_count)

    def test_a_database_already_in_wal_is_left_alone(self) -> None:
        conn = FlakyConnection(locked_times=99, mode="wal")
        sqlite_wal.enable_wal(conn)
        self.assertNotIn("PRAGMA journal_mode=WAL", conn.statements)

    def test_other_errors_and_a_lock_that_never_clears_are_raised(self) -> None:
        with patch.object(sqlite_wal.time, "sleep"), self.assertRaises(sqlite3.OperationalError):
            sqlite_wal.enable_wal(FlakyConnection(locked_times=sqlite_wal.ATTEMPTS))

        class Broken(FlakyConnection):
            def execute(self, sql: str):
                raise sqlite3.OperationalError("disk I/O error")

        with self.assertRaises(sqlite3.OperationalError):
            sqlite_wal.enable_wal(Broken(0))


if __name__ == "__main__":
    unittest.main()
