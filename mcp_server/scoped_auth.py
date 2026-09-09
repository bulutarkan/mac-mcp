from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, status

from .policy import PolicyContext, environment_policy_context
from .policy_scope import ResourceScope
from .security import Settings, authenticate

DEFAULT_SCOPED_AUTH_DB = Path.home() / ".mac-mcp" / "state" / "scoped_credentials.sqlite3"
_TOKEN_PREFIX = "mcpagt_"


@dataclass(frozen=True)
class ScopedCredential:
    token_id: str
    agent_id: str
    team_id: Optional[str]
    profile: str
    scope: ResourceScope
    expires_at: float

    def policy_context(self) -> PolicyContext:
        return PolicyContext(
            profile=self.profile,
            actor=f"agent:{self.agent_id}",
            agent_id=self.agent_id,
            team_id=self.team_id,
            scope=self.scope,
        )


class ScopedCredentialStore:
    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = Path(db_path or os.getenv("MAC_MCP_SCOPED_AUTH_DB", str(DEFAULT_SCOPED_AUTH_DB))).expanduser()
        self._lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.db_path.parent.chmod(0o700)
        except OSError:
            pass
        conn = sqlite3.connect(self.db_path, timeout=10)
        try:
            self.db_path.chmod(0o600)
        except OSError:
            pass
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scoped_credentials (
                token_hash TEXT PRIMARY KEY,
                token_id TEXT NOT NULL UNIQUE,
                agent_id TEXT NOT NULL,
                team_id TEXT,
                profile TEXT NOT NULL,
                scope_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                revoked_at REAL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_scoped_agent ON scoped_credentials(agent_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_scoped_expiry ON scoped_credentials(expires_at)")
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def issue(
        self,
        *,
        agent_id: str,
        team_id: Optional[str],
        profile: str,
        scope: ResourceScope,
        ttl_s: int,
    ) -> tuple[str, str]:
        now = time.time()
        token_id = "cred_" + secrets.token_hex(8)
        token = _TOKEN_PREFIX + secrets.token_urlsafe(32)
        expires_at = now + max(60, int(ttl_s))
        with self._lock, self._connection() as conn:
            conn.execute("DELETE FROM scoped_credentials WHERE expires_at <= ? OR revoked_at IS NOT NULL", (now,))
            conn.execute(
                """
                INSERT INTO scoped_credentials (
                    token_hash, token_id, agent_id, team_id, profile, scope_json,
                    created_at, expires_at, revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    self._hash(token), token_id, agent_id, team_id, profile,
                    json.dumps(scope.to_dict(), ensure_ascii=False, sort_keys=True),
                    now, expires_at,
                ),
            )
        return token, token_id

    def resolve(self, token: str) -> Optional[ScopedCredential]:
        if not token.startswith(_TOKEN_PREFIX):
            return None
        now = time.time()
        with self._lock, self._connection() as conn:
            row = conn.execute(
                """
                SELECT token_id, agent_id, team_id, profile, scope_json, expires_at, revoked_at
                FROM scoped_credentials WHERE token_hash = ?
                """,
                (self._hash(token),),
            ).fetchone()
        if row is None or row["revoked_at"] is not None or float(row["expires_at"]) <= now:
            return None
        try:
            scope = ResourceScope.from_dict(json.loads(str(row["scope_json"])))
        except (ValueError, TypeError, json.JSONDecodeError):
            return None
        return ScopedCredential(
            token_id=str(row["token_id"]),
            agent_id=str(row["agent_id"]),
            team_id=str(row["team_id"]) if row["team_id"] else None,
            profile=str(row["profile"]),
            scope=scope,
            expires_at=float(row["expires_at"]),
        )

    def revoke_token_id(self, token_id: str) -> None:
        with self._lock, self._connection() as conn:
            conn.execute(
                "UPDATE scoped_credentials SET revoked_at = ? WHERE token_id = ? AND revoked_at IS NULL",
                (time.time(), token_id),
            )

    def revoke_agent(self, agent_id: str) -> None:
        with self._lock, self._connection() as conn:
            conn.execute(
                "UPDATE scoped_credentials SET revoked_at = ? WHERE agent_id = ? AND revoked_at IS NULL",
                (time.time(), agent_id),
            )


_STORE: Optional[ScopedCredentialStore] = None
_STORE_LOCK = threading.Lock()


def get_scoped_credential_store() -> ScopedCredentialStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = ScopedCredentialStore()
        return _STORE


def _bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    if not authorization.lower().startswith("bearer "):
        return None
    return authorization[7:].strip()


def resolve_request_identity(
    settings: Settings,
    authorization: Optional[str],
) -> tuple[str, PolicyContext]:
    """Resolve scoped agent bearer first, then the normal server credential/no-auth path.

    A bearer that looks like a scoped agent token must never downgrade to no-auth.
    """

    token = _bearer(authorization)
    if token and token.startswith(_TOKEN_PREFIX):
        credential = get_scoped_credential_store().resolve(token)
        if credential is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired scoped agent token.")
        return credential.token_id, credential.policy_context()

    auth_key = authenticate(settings, authorization)
    return auth_key, environment_policy_context(
        actor="no-auth" if auth_key == "no-auth" else "authenticated",
    )
