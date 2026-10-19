"""Your Macs (#147): invitations, the owner's decision, credentials and what a paired Mac may do.

Two servers talking to each other are checked live (two Mac MCP servers on this machine); here the
store and the routes are checked on their own.
"""
from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp_server import fleet
from mcp_server.fleet_routes import create_fleet_routes
from mcp_server.policy import Capability, resolve_risk

B_ID, A_ID = "mac_bbbbbbbbbbbbbbbb", "mac_aaaaaaaaaaaaaaaa"


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.store = fleet.FleetStore(Path(tempfile.mkdtemp()) / "fleet")

    def ask(self, code: str, source: str = "1.2.3.4", device: str = A_ID):
        return self.store.request_access(code, client_device_id=device, client_name="Tarkan's MacBook Air",
                                         server_device_id=B_ID, source=source)


class InvitationTests(StoreCase):
    def test_an_invitation_is_used_once_by_link_or_typed_code(self) -> None:
        offer = self.store.issue_offer("status")
        self.assertRegex(offer["code"], r"^[2-9A-HJ-NP-Z]{4}-[2-9A-HJ-NP-Z]{4}$")
        made = self.ask(offer["code"].lower())  # typed codes ignore case and the dash
        self.assertEqual("status", made["permission"])
        self.assertRegex(made["check_code"], r"^\d{3} \d{3}$")
        with self.assertRaises(fleet.FleetError):
            self.ask(offer["code"])
        link = self.store.issue_offer("recipes")
        self.assertEqual("recipes", self.ask(link["token"])["permission"])

    def test_a_new_invitation_ends_the_last_and_old_ones_expire(self) -> None:
        first = self.store.issue_offer("status")
        self.store.issue_offer("status")
        with self.assertRaises(fleet.FleetError):
            self.ask(first["code"])
        offer = self.store.issue_offer("status", ttl_s=15)
        with patch.object(fleet, "_now", return_value=offer["expires_at"] + 1):
            with self.assertRaises(fleet.FleetError):
                self.ask(offer["code"])

    def test_wrong_codes_lock_their_sender_and_then_the_window(self) -> None:
        offer = self.store.issue_offer("status")
        for _ in range(fleet.MAX_ATTEMPTS_PER_SOURCE):
            with self.assertRaises(fleet.FleetError):
                self.ask("ZZZZ-ZZZZ", source="9.9.9.9")
        with self.assertRaises(fleet.FleetError):  # even the right code, from that sender
            self.ask(offer["code"], source="9.9.9.9")
        self.assertTrue(self.ask(offer["code"], source="1.2.3.4")["request_id"])  # another sender is not locked
        offer = self.store.issue_offer("status")
        with patch.object(fleet, "MAX_ATTEMPTS_TOTAL", 3):
            for index in range(3):
                with self.assertRaises(fleet.FleetError):
                    self.ask("ZZZZ-ZZZZ", source=f"10.0.0.{index}")
            with self.assertRaises(fleet.FleetError):
                self.ask(offer["code"], source="1.2.3.5")

    def test_permission_and_device_are_checked(self) -> None:
        with self.assertRaises(fleet.FleetError):
            self.store.issue_offer("everything")
        offer = self.store.issue_offer("status")
        with self.assertRaises(fleet.FleetError):
            self.ask(offer["code"], device="not-a-mac")


class DecisionTests(StoreCase):
    def test_allowing_issues_a_credential_once_and_only_its_hash_is_kept(self) -> None:
        made = self.ask(self.store.issue_offer("recipes")["code"])
        self.assertEqual("pending", self.store.poll_request(made["request_id"], made["poll_secret"])["status"])
        self.assertEqual(1, len(self.store.pending_requests()))
        self.store.decide(made["request_id"], allow=True)
        first = self.store.poll_request(made["request_id"], made["poll_secret"])
        self.assertTrue(first["credential"].startswith(fleet.CREDENTIAL_PREFIX))
        self.assertNotIn("credential", self.store.poll_request(made["request_id"], made["poll_secret"]))
        client = self.store.client_for(first["credential"])
        self.assertEqual(("recipes", A_ID), (client["permission"], client["device_id"]))
        raw = (self.store.root / "fleet.sqlite3").read_bytes()
        self.assertNotIn(first["credential"].encode(), raw)
        self.assertEqual(0o600, stat.S_IMODE((self.store.root / "fleet.sqlite3").stat().st_mode))

    def test_refused_unknown_and_wrong_secret_requests(self) -> None:
        made = self.ask(self.store.issue_offer("status")["code"])
        with self.assertRaises(fleet.FleetError):
            self.store.poll_request(made["request_id"], "mcfleetpoll_wrong")
        self.store.decide(made["request_id"], allow=False)
        self.assertEqual("denied", self.store.poll_request(made["request_id"], made["poll_secret"])["status"])
        with self.assertRaises(fleet.FleetError):
            self.store.decide(made["request_id"], allow=True)

    def test_revoking_and_pairing_again_replace_access(self) -> None:
        def pair() -> str:
            made = self.ask(self.store.issue_offer("status")["code"])
            self.store.decide(made["request_id"], allow=True)
            return self.store.poll_request(made["request_id"], made["poll_secret"])["credential"]
        old = pair()
        new = pair()
        self.assertIsNone(self.store.client_for(old))  # the same Mac holds one access, not two
        self.assertEqual(1, len(self.store.clients()))
        self.assertTrue(self.store.revoke_client(self.store.clients()[0]["client_id"]))
        self.assertIsNone(self.store.client_for(new))


class AddressTests(unittest.TestCase):
    def test_addresses_are_https_or_this_machine(self) -> None:
        self.assertEqual("https://mac.example.com", fleet.normalize_base_url("mac.example.com/mcp"))
        self.assertEqual("http://127.0.0.1:8878", fleet.normalize_base_url("http://127.0.0.1:8878/"))
        with self.assertRaises(fleet.FleetError):
            fleet.normalize_base_url("http://mac.example.com")
        self.assertEqual(("https://mac.example.com", "mcfleetpair_x"),
                         fleet.invitation_parts("https://mac.example.com/fleet#pair=mcfleetpair_x"))
        self.assertEqual(("https://b.example.net", "ABCD-EFGH"), fleet.invitation_parts(" b.example.net  ABCD-EFGH "))
        with self.assertRaises(fleet.FleetError):
            fleet.invitation_parts("just words here")


class RouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = fleet.FleetStore(Path(tempfile.mkdtemp()) / "fleet")
        patcher = patch.object(fleet, "_STORE", self.store)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.runs = []

        async def runner(recipe_id, values, **kwargs):
            self.runs.append((recipe_id, values, kwargs["actor"]))
            return {"ok": True, "run_id": "run_1"}
        owner_ok = lambda request: None  # noqa: E731  (the dashboard guard has its own tests)
        self.client = TestClient(Starlette(routes=create_fleet_routes(owner_ok, recipe_runner=runner)))

    def paired(self, permission: str) -> str:
        offer = self.store.issue_offer(permission)
        made = self.client.post("/fleet/pair", json={"code": offer["code"], "device_id": A_ID, "name": "Air"}).json()
        self.store.decide(made["request_id"], allow=True)
        return self.client.post("/fleet/pair/status", json={"request_id": made["request_id"],
                                                            "poll_secret": made["poll_secret"]}).json()["credential"]

    def test_status_needs_a_credential_and_reveals_no_api_key(self) -> None:
        self.assertEqual(401, self.client.get("/fleet/status").status_code)
        self.assertEqual(401, self.client.get("/fleet/status", headers={"Authorization": "Bearer mcfleet_nope"}).status_code)
        token = self.paired("status")
        body = self.client.get("/fleet/status", headers={"Authorization": f"Bearer {token}"}).json()
        self.assertEqual("status", body["your_permission"])
        self.assertTrue(body["version"])
        self.assertNotIn("api_key", json.dumps(body).lower())

    def test_status_only_cannot_list_or_run_recipes(self) -> None:
        auth = {"Authorization": f"Bearer {self.paired('status')}"}
        self.assertEqual(403, self.client.get("/fleet/recipes", headers=auth).status_code)
        self.assertEqual(403, self.client.post("/fleet/recipes/run", headers=auth,
                                               json={"recipe_id": "rcp_0123456789ab"}).status_code)
        self.assertEqual([], self.runs)

    def test_recipes_run_as_the_paired_mac_and_inputs_are_checked(self) -> None:
        auth = {"Authorization": f"Bearer {self.paired('recipes')}"}
        ran = self.client.post("/fleet/recipes/run", headers=auth,
                               json={"recipe_id": "rcp_0123456789ab", "values": {"city": "Izmir"}})
        self.assertEqual(200, ran.status_code)
        self.assertEqual([("rcp_0123456789ab", {"city": "Izmir"}, "fleet:Air")], self.runs)
        for bad in ({"recipe_id": "../x"}, {"recipe_id": "rcp_0123456789ab", "values": {"a": {"nested": 1}}}):
            self.assertEqual(400, self.client.post("/fleet/recipes/run", headers=auth, json=bad).status_code)

    def test_pairing_is_rate_limited_and_needs_json(self) -> None:
        self.assertEqual(415, self.client.post("/fleet/pair", data="code=x").status_code)
        codes = [self.client.post("/fleet/pair", json={"code": "ZZZZ-ZZZZ", "device_id": A_ID}).status_code
                 for _ in range(9)]
        self.assertEqual(429, codes[-1])

    def test_forget_ends_the_callers_own_access(self) -> None:
        auth = {"Authorization": f"Bearer {self.paired('status')}"}
        self.assertEqual(200, self.client.post("/fleet/forget", headers=auth, json={}).status_code)
        self.assertEqual(401, self.client.get("/fleet/status", headers=auth).status_code)

    def test_failed_credentials_are_limited_per_source(self) -> None:
        codes = [self.client.get("/fleet/status", headers={"Authorization": "Bearer mcfleet_guess"}).status_code
                 for _ in range(21)]
        self.assertEqual(429, codes[-1])


class PolicyTests(unittest.TestCase):
    def test_reading_is_read_only_and_running_acts_elsewhere(self) -> None:
        _, listed = resolve_risk("fleet", {"action": "list"})
        _, run = resolve_risk("fleet", {"action": "run_recipe", "mac": "Studio", "recipe_id": "rcp_0123456789ab"})
        self.assertNotIn(Capability.EXTERNAL_SIDE_EFFECT, listed.capabilities)
        self.assertIn(Capability.EXTERNAL_SIDE_EFFECT, run.capabilities)


if __name__ == "__main__":
    unittest.main()
