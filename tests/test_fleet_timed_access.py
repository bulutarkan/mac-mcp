"""#80: full access for a while. The owner picks how long in the invitation (no limit by default); when it ends
the other Mac keeps reading, and only the owner gives full access again, in Settings. Pairings from before keep
the access they had.
"""
from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from mcp_server import fleet
from mcp_server.fleet_routes import create_fleet_routes
from mcp_server.scoped_auth import resolve_request_identity
from mcp_server.security import load_settings

B_ID, A_ID = "mac_bbbbbbbbbbbbbbbb", "mac_aaaaaaaaaaaaaaaa"
NOW = 1_800_000_000.0


class TimedAccessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp()) / "fleet"
        self.store = fleet.FleetStore(self.root)
        self.clock = NOW
        me = {"device_id": B_ID, "name": "Studio", "model": "Mac", "available": True}
        for patcher in (patch.object(fleet, "_STORE", self.store), patch.object(fleet, "identity", return_value=me),
                        patch.object(fleet, "_now", side_effect=lambda: self.clock),
                        patch("mcp_server.policy.permission_profile_name", return_value="trusted")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(Starlette(routes=create_fleet_routes(lambda request: None)))

    def pair(self, permission: str, full_for: str = "none", code: str = "") -> str:
        code = code or self.store.issue_offer(permission, full_for=full_for)["code"]
        made = self.store.request_access(code, client_device_id=A_ID, client_name="Air", server_device_id=B_ID, source="s")
        self.waiting = self.store.pending_requests()
        self.clock += 30  # the owner says yes a little later: the hour runs from then
        self.store.decide(made["request_id"], allow=True)
        return self.store.poll_request(made["request_id"], made["poll_secret"])["credential"]

    def profile(self, credential: str) -> str:
        return resolve_request_identity(load_settings(), f"Bearer {credential}")[1].profile

    def test_full_for_an_hour_then_it_reads_only(self) -> None:
        with patch.object(fleet, "own_url", return_value="https://b.example.net"):
            invited = self.client.post("/dashboard/api/fleet/invite", json={"permission": "full", "full_for": "1h"}).json()
        self.assertEqual(("full", "1h"), (invited["permission"], invited["full_for"]))
        credential = self.pair("full", code=invited["code"])
        self.assertEqual("1h", self.waiting[0]["full_for"])  # the owner sees how long, before saying yes
        row = self.store.clients()[0]
        self.assertEqual(NOW + 30 + 3600, row["full_until"])
        self.assertEqual(("full", False), (row["permission"], row["full_expired"]))
        self.assertEqual("trusted", self.profile(credential))
        self.clock = NOW + 30 + 3600  # the hour is over
        row = self.store.clients()[0]
        self.assertEqual(("read_only", "full", True), (row["permission"], row["granted_permission"], row["full_expired"]))
        self.assertEqual("read_only", self.profile(credential))  # the pairing stays: it still reads
        status = self.client.get("/fleet/status", headers={"Authorization": f"Bearer {credential}"}).json()
        self.assertEqual(("read_only", None), (status["your_permission"], status["your_full_until"]))

    def test_no_limit_is_the_default_and_other_levels_have_none(self) -> None:
        self.assertIsNone(fleet.full_until("none"))
        self.assertEqual("none", self.store.issue_offer("full")["full_for"])
        self.assertEqual("none", self.store.issue_offer("read_only", full_for="1h")["full_for"])
        with self.assertRaises(fleet.FleetError):
            self.store.issue_offer("full", full_for="forever")

    def test_today_ends_at_local_midnight_and_seven_days_is_a_week(self) -> None:
        start = time.mktime((2026, 10, 21, 15, 30, 0, 0, 0, -1))
        self.assertEqual(time.mktime((2026, 10, 22, 0, 0, 0, 0, 0, -1)), fleet.full_until("today", now=start))
        self.assertEqual(start + 7 * 86400, fleet.full_until("7d", now=start))

    def test_the_owner_gives_full_again_or_less(self) -> None:
        credential = self.pair("full", "1h")
        client_id = self.store.clients()[0]["client_id"]
        self.clock += 7200
        self.assertEqual("read_only", self.profile(credential))
        renewed = self.client.post("/dashboard/api/fleet/clients/access",
                                   json={"client_id": client_id, "permission": "full", "full_for": "1h"}).json()
        self.assertEqual(("full", self.clock + 3600), (renewed["client"]["permission"], renewed["client"]["full_until"]))
        self.assertEqual("trusted", self.profile(credential))
        self.client.post("/dashboard/api/fleet/clients/access", json={"client_id": client_id, "permission": "status"})
        with self.assertRaises(HTTPException) as status_only:
            self.profile(credential)  # status only opens no tools
        self.assertEqual(401, status_only.exception.status_code)
        unknown = self.client.post("/dashboard/api/fleet/clients/access", json={"client_id": "fcl_nope", "permission": "full"})
        self.assertEqual(404, unknown.status_code)

    def test_only_the_owner_changes_access(self) -> None:
        refusing = TestClient(Starlette(routes=create_fleet_routes(lambda request: JSONResponse({}, status_code=401))))
        self.pair("read_only")
        client_id = self.store.clients()[0]["client_id"]
        response = refusing.post("/dashboard/api/fleet/clients/access", json={"client_id": client_id, "permission": "full"})
        self.assertEqual(401, response.status_code)
        self.assertEqual("read_only", self.store.clients()[0]["permission"])

    def test_pairings_from_before_keep_their_access(self) -> None:
        old_root = Path(tempfile.mkdtemp()) / "fleet"
        old_root.mkdir(parents=True)
        with closing(sqlite3.connect(old_root / "fleet.sqlite3")) as conn:  # the schema of 2.3.0
            conn.executescript("""
                CREATE TABLE clients (client_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, name TEXT NOT NULL,
                    permission TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, created_at REAL NOT NULL,
                    last_seen_at REAL, revoked_at REAL);
                CREATE TABLE offers (code_hash TEXT PRIMARY KEY, manual_hash TEXT NOT NULL, permission TEXT NOT NULL,
                    created_at REAL NOT NULL, expires_at REAL NOT NULL, consumed_at REAL,
                    failed_attempts INTEGER NOT NULL DEFAULT 0);
            """)
            conn.execute("INSERT INTO clients VALUES ('fcl_old', ?, 'Old Air', 'full', ?, 1.0, NULL, NULL)",
                         (A_ID, fleet._hash("mcfleet_old")))
            conn.commit()
        old = fleet.FleetStore(old_root)
        self.clock = NOW + 10 * 365 * 86400
        row = old.clients()[0]
        self.assertEqual(("full", None, False), (row["permission"], row["full_until"], row["full_expired"]))
        self.assertEqual("1h", old.issue_offer("full", full_for="1h")["full_for"])  # the new columns are there


if __name__ == "__main__":
    unittest.main()
