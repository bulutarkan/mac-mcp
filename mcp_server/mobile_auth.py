from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Optional

PAIR_PREFIX = "mcpair_"
SESSION_PREFIX = "mcpmob_"
DEFAULT_PAIR_TTL_S = 120
DEFAULT_SESSION_TTL_S = 60 * 60 * 24 * 30
MANUAL_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
MANUAL_CODE_LENGTH = 8
MAX_MANUAL_ATTEMPTS = 5


def mobile_auth_db_path() -> Path:
    state_dir = Path(os.getenv("MAC_MCP_STATE_DIR", str(Path.home() / ".mac-mcp"))).expanduser()
    return state_dir / "state" / "mobile_auth.sqlite3"


class MobileAuthStore:
    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = Path(db_path or mobile_auth_db_path()).expanduser()
        self._lock = threading.RLock()

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.db_path.parent.chmod(0o700)
        except OSError:
            pass
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mobile_pairings (
                code_hash TEXT PRIMARY KEY,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                consumed_at REAL
            )
            """
        )
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(mobile_pairings)").fetchall()
        }
        if "manual_code_hash" not in columns:
            conn.execute("ALTER TABLE mobile_pairings ADD COLUMN manual_code_hash TEXT")
        if "failed_attempts" not in columns:
            conn.execute(
                "ALTER TABLE mobile_pairings ADD COLUMN failed_attempts INTEGER NOT NULL DEFAULT 0"
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mobile_sessions (
                token_hash TEXT PRIMARY KEY,
                device_id TEXT NOT NULL UNIQUE,
                device_name TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                last_seen_at REAL NOT NULL,
                revoked_at REAL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mobile_session_expiry ON mobile_sessions(expires_at)")
        try:
            self.db_path.chmod(0o600)
        except OSError:
            pass
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _clean_device_name(value: object) -> str:
        text = " ".join(str(value or "").strip().split())
        if not text:
            return "iPhone or iPad"
        return text[:80]

    @staticmethod
    def _normalize_manual_code(value: object) -> str:
        return "".join(ch for ch in str(value or "").upper() if ch.isalnum())

    @staticmethod
    def _format_manual_code(value: str) -> str:
        return value[:4] + "-" + value[4:]

    def issue_pairing(self, ttl_s: int = DEFAULT_PAIR_TTL_S) -> Dict[str, Any]:
        ttl = max(15, min(int(ttl_s), DEFAULT_PAIR_TTL_S))
        now = time.time()
        code = PAIR_PREFIX + secrets.token_urlsafe(32)
        manual_raw = "".join(
            secrets.choice(MANUAL_CODE_ALPHABET) for _ in range(MANUAL_CODE_LENGTH)
        )
        expires_at = now + ttl
        with self._lock, self._connection() as conn:
            with conn:
                # Pairing is an explicit short-lived window. Issuing a new one
                # invalidates any previous unused window so manual-code attempt
                # accounting stays unambiguous.
                conn.execute("DELETE FROM mobile_pairings")
                conn.execute(
                    """
                    INSERT INTO mobile_pairings(
                        code_hash, manual_code_hash, failed_attempts,
                        created_at, expires_at, consumed_at
                    ) VALUES (?, ?, 0, ?, ?, NULL)
                    """,
                    (
                        self._hash(code),
                        self._hash(manual_raw),
                        now,
                        expires_at,
                    ),
                )
        return {
            "code": code,
            "manual_code": self._format_manual_code(manual_raw),
            "created_at": now,
            "expires_at": expires_at,
        }

    def consume_pairing(
        self,
        code: str,
        *,
        device_name: object = None,
        session_ttl_s: int = DEFAULT_SESSION_TTL_S,
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(code, str):
            return None

        is_qr_code = code.startswith(PAIR_PREFIX)
        manual_code = self._normalize_manual_code(code)
        if not is_qr_code and len(manual_code) != MANUAL_CODE_LENGTH:
            return None

        now = time.time()
        token = SESSION_PREFIX + secrets.token_urlsafe(48)
        device_id = "mob_" + uuid.uuid4().hex[:16]
        ttl = max(300, int(session_ttl_s))
        expires_at = now + ttl
        lookup_hash = self._hash(code if is_qr_code else manual_code)

        with self._lock, self._connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                if is_qr_code:
                    row = conn.execute(
                        """
                        SELECT code_hash, expires_at, consumed_at, failed_attempts
                        FROM mobile_pairings
                        WHERE code_hash=?
                        """,
                        (lookup_hash,),
                    ).fetchone()
                else:
                    row = conn.execute(
                        """
                        SELECT code_hash, expires_at, consumed_at, failed_attempts
                        FROM mobile_pairings
                        WHERE manual_code_hash=?
                        """,
                        (lookup_hash,),
                    ).fetchone()

                    if row is None:
                        active = conn.execute(
                            """
                            SELECT code_hash, failed_attempts
                            FROM mobile_pairings
                            WHERE consumed_at IS NULL AND expires_at > ?
                            ORDER BY created_at DESC
                            LIMIT 1
                            """,
                            (now,),
                        ).fetchone()
                        if active is not None:
                            attempts = int(active["failed_attempts"] or 0) + 1
                            if attempts >= MAX_MANUAL_ATTEMPTS:
                                conn.execute(
                                    """
                                    UPDATE mobile_pairings
                                    SET failed_attempts=?, consumed_at=?
                                    WHERE code_hash=? AND consumed_at IS NULL
                                    """,
                                    (attempts, now, active["code_hash"]),
                                )
                            else:
                                conn.execute(
                                    """
                                    UPDATE mobile_pairings
                                    SET failed_attempts=?
                                    WHERE code_hash=? AND consumed_at IS NULL
                                    """,
                                    (attempts, active["code_hash"]),
                                )
                        conn.commit()
                        return None

                if (
                    row is None
                    or row["consumed_at"] is not None
                    or float(row["expires_at"]) <= now
                    or int(row["failed_attempts"] or 0) >= MAX_MANUAL_ATTEMPTS
                ):
                    conn.rollback()
                    return None

                updated = conn.execute(
                    """
                    UPDATE mobile_pairings
                    SET consumed_at=?
                    WHERE code_hash=? AND consumed_at IS NULL
                    """,
                    (now, row["code_hash"]),
                )
                if updated.rowcount != 1:
                    conn.rollback()
                    return None

                name = self._clean_device_name(device_name)
                conn.execute(
                    """
                    INSERT INTO mobile_sessions(
                        token_hash, device_id, device_name, created_at, expires_at, last_seen_at, revoked_at
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (self._hash(token), device_id, name, now, expires_at, now),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        return {
            "token": token,
            "device_id": device_id,
            "device_name": self._clean_device_name(device_name),
            "created_at": now,
            "expires_at": expires_at,
            "last_seen_at": now,
        }

    def resolve_session(self, token: Optional[str], *, touch: bool = True) -> Optional[Dict[str, Any]]:
        if not token or not isinstance(token, str) or not token.startswith(SESSION_PREFIX):
            return None
        now = time.time()
        token_hash = self._hash(token)
        with self._lock, self._connection() as conn:
            row = conn.execute(
                """
                SELECT device_id, device_name, created_at, expires_at, last_seen_at, revoked_at
                FROM mobile_sessions WHERE token_hash=?
                """,
                (token_hash,),
            ).fetchone()
            if row is None or row["revoked_at"] is not None or float(row["expires_at"]) <= now:
                return None
            last_seen_at = float(row["last_seen_at"])
            if touch and now - last_seen_at >= 5:
                with conn:
                    conn.execute(
                        "UPDATE mobile_sessions SET last_seen_at=? WHERE token_hash=? AND revoked_at IS NULL",
                        (now, token_hash),
                    )
                last_seen_at = now
        return {
            "device_id": str(row["device_id"]),
            "device_name": str(row["device_name"]),
            "created_at": float(row["created_at"]),
            "expires_at": float(row["expires_at"]),
            "last_seen_at": last_seen_at,
        }

    def list_devices(self) -> list[Dict[str, Any]]:
        now = time.time()
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT device_id, device_name, created_at, expires_at, last_seen_at
                FROM mobile_sessions
                WHERE revoked_at IS NULL AND expires_at > ?
                ORDER BY last_seen_at DESC
                """,
                (now,),
            ).fetchall()
        return [
            {
                "device_id": str(row["device_id"]),
                "device_name": str(row["device_name"]),
                "created_at": float(row["created_at"]),
                "expires_at": float(row["expires_at"]),
                "last_seen_at": float(row["last_seen_at"]),
            }
            for row in rows
        ]

    def revoke(self, device_id: str) -> bool:
        clean = str(device_id or "").strip()
        if not clean:
            return False
        now = time.time()
        with self._lock, self._connection() as conn:
            with conn:
                result = conn.execute(
                    "UPDATE mobile_sessions SET revoked_at=? WHERE device_id=? AND revoked_at IS NULL",
                    (now, clean),
                )
        return result.rowcount == 1
