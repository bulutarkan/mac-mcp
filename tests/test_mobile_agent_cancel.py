from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

from tests import test_mobile_dashboard as mobile_fixture

REPLY = {"status": "cancelled", "cancellation": {"state": "confirmed", "requested_by": "user"}}


class MobileAgentCancelTests(unittest.TestCase):
    def paired_phone(self, td: str) -> TestClient:
        helper = mobile_fixture.MobileDashboardTests()
        app, _telemetry, _store = helper.make_app(Path(td))
        manager = TestClient(app, base_url="https://testserver")
        with patch("mcp_server.mobile_routes._public_mobile_url", return_value="https://mobile.example.test/mobile"):
            body = manager.post("/dashboard/api/mobile/pairings",
                                headers={"authorization": "Bearer mobile-dashboard-test-token-0123456789"}).json()
        phone = TestClient(app, base_url="https://testserver")
        self.assertEqual(200, phone.post("/mobile/pair", json={"code": helper.pair_code(body["pair_url"]),
                                                               "device_name": "Phone"}).status_code)
        return phone

    def test_the_paired_owner_stops_an_agent_as_the_user(self) -> None:
        with tempfile.TemporaryDirectory() as td, \
             patch("mcp_server.mobile_routes.agent_action", return_value=REPLY) as action:
            phone = self.paired_phone(td)
            response = phone.post("/mobile/api/agents/cancel", json={"agent_id": "agt_0123abcd"},
                                  headers={"origin": "https://testserver"})
        self.assertEqual(200, response.status_code)
        self.assertEqual("confirmed", response.json()["cancellation_state"])
        self.assertEqual("user", action.call_args.kwargs["requested_by"])

    def test_unpaired_cross_site_form_and_bad_ids_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as td, \
             patch("mcp_server.mobile_routes.agent_action", return_value=REPLY) as action:
            phone = self.paired_phone(td)
            stranger = TestClient(phone.app, base_url="https://testserver")
            self.assertEqual(401, stranger.post("/mobile/api/agents/cancel", json={"agent_id": "agt_x"}).status_code)
            form = phone.post("/mobile/api/agents/cancel", data={"agent_id": "agt_x"})
            self.assertEqual(415, form.status_code, "a cross-site form post cannot send JSON")
            other_site = phone.post("/mobile/api/agents/cancel", json={"agent_id": "agt_x"},
                                    headers={"origin": "https://evil.example"})
            self.assertEqual(403, other_site.status_code)
            bad = phone.post("/mobile/api/agents/cancel", json={"agent_id": "../agents"})
            self.assertEqual(400, bad.status_code)
        action.assert_not_called()


if __name__ == "__main__":
    unittest.main()
