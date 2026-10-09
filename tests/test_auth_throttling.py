from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import os
import time
import unittest
from dataclasses import replace
from unittest.mock import patch

from starlette.testclient import TestClient

from mcp_server import security
from mcp_server.main import create_app
from mcp_server.security import AuthFailureLimiter, RateLimiter, load_settings, validate_bootstrap_security


class AuthThrottlingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(create_app(), base_url="http://127.0.0.1:8765")
        self.limit = AuthFailureLimiter().limit
        self.valid = "Bearer " + os.environ["MCP_API_KEY"]

    def test_wrong_keys_are_throttled_before_lookup_on_mcp(self) -> None:
        statuses = [
            self.client.post("/mcp", headers={"authorization": f"Bearer wrong-{index}"}).status_code
            for index in range(self.limit)
        ]
        self.assertEqual([401] * self.limit, statuses)
        blocked = self.client.post("/mcp", headers={"authorization": "Bearer wrong-final"})
        self.assertEqual(429, blocked.status_code)
        self.assertEqual("60", blocked.headers.get("retry-after"))
        query = self.client.post("/mcp?ApiKey=still-wrong")
        self.assertEqual(429, query.status_code)

    def test_spoofed_forwarding_headers_do_not_reset_the_budget(self) -> None:
        for index in range(self.limit):
            self.client.post(
                "/mcp",
                headers={"authorization": "Bearer wrong", "x-forwarded-for": f"198.51.100.{index}"},
            )
        blocked = self.client.post(
            "/mcp", headers={"authorization": "Bearer wrong", "x-forwarded-for": "203.0.113.250"},
        )
        self.assertEqual(429, blocked.status_code)

    def test_rest_routes_share_the_same_throttle(self) -> None:
        for index in range(self.limit):
            response = self.client.post("/api/system_info", headers={"authorization": f"Bearer wrong-{index}"}, json={})
            self.assertEqual(401, response.status_code)
        self.assertEqual(429, self.client.post("/api/system_info", headers={"authorization": "Bearer x"}, json={}).status_code)
        self.assertEqual(429, self.client.post("/mcp", headers={"authorization": "Bearer y"}).status_code)

    def test_non_ascii_credential_is_rejected_not_crashed(self) -> None:
        response = self.client.post("/mcp", headers={"authorization": "Bearer şifre-ü".encode("utf-8")})
        self.assertEqual(401, response.status_code)

    def test_correct_key_still_authenticates_after_a_few_failures(self) -> None:
        for index in range(3):
            self.client.post("/mcp", headers={"authorization": f"Bearer wrong-{index}"})
        response = self.client.post("/api/system_info", headers={"authorization": self.valid}, json={})
        self.assertNotIn(response.status_code, {401, 429})


class BootstrapKeyStrengthTests(unittest.TestCase):
    def test_short_api_key_fails_bootstrap(self) -> None:
        settings = replace(load_settings(), api_key="short-key", allow_no_auth=False)
        with self.assertRaises(RuntimeError) as ctx:
            validate_bootstrap_security(settings)
        self.assertIn("secure_bootstrap_weak_api_key", str(ctx.exception))
        validate_bootstrap_security(replace(settings, api_key="k" * security.MIN_API_KEY_LENGTH))


class LimiterBoundTests(unittest.TestCase):
    def test_limiter_state_stays_bounded(self) -> None:
        limiter = RateLimiter(5)
        failures = AuthFailureLimiter(5)
        with patch.object(security, "_MAX_LIMITER_KEYS", 50):
            for index in range(500):
                limiter.check(f"token-{index}:ip")
                failures.record_failure(f"203.0.113.{index}")
        self.assertLessEqual(len(limiter._hits), 50)
        self.assertLessEqual(len(failures._failures), 50)

    def test_failures_expire_after_a_minute(self) -> None:
        failures = AuthFailureLimiter(2)
        failures.record_failure("a")
        failures.record_failure("a")
        self.assertTrue(failures.blocked("a"))
        with patch.object(security.time, "time", return_value=time.time() + 61):
            self.assertFalse(failures.blocked("a"))


if __name__ == "__main__":
    unittest.main()
