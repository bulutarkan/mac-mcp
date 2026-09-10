import unittest
from types import SimpleNamespace

from fastapi import HTTPException

from mcp_server.security import authenticate, request_authorization


class AuthTests(unittest.TestCase):
    @staticmethod
    def settings(*, allow_no_auth: bool, api_key: str = "test-secret"):
        return SimpleNamespace(allow_no_auth=allow_no_auth, api_key=api_key)

    def test_bearer_auth_remains_supported(self):
        settings = self.settings(allow_no_auth=False)
        self.assertEqual("test-secret", authenticate(settings, "Bearer test-secret"))

    def test_query_api_key_becomes_bearer_when_auth_is_enabled(self):
        settings = self.settings(allow_no_auth=False)
        authorization = request_authorization(settings, None, ["test-secret"])
        self.assertEqual("Bearer test-secret", authorization)
        self.assertEqual("test-secret", authenticate(settings, authorization))

    def test_header_is_authoritative_over_query_api_key(self):
        settings = self.settings(allow_no_auth=False)
        authorization = request_authorization(settings, "Bearer wrong", ["test-secret"])
        self.assertEqual("Bearer wrong", authorization)
        with self.assertRaises(HTTPException) as ctx:
            authenticate(settings, authorization)
        self.assertEqual(401, ctx.exception.status_code)

    def test_wrong_empty_or_duplicate_query_api_key_is_rejected(self):
        settings = self.settings(allow_no_auth=False)
        for query_values in (["wrong"], [""], ["test-secret", "test-secret"]):
            with self.subTest(query_values=query_values):
                with self.assertRaises(HTTPException) as ctx:
                    request_authorization(settings, None, list(query_values))
                self.assertEqual(401, ctx.exception.status_code)
                self.assertEqual("Invalid API key.", ctx.exception.detail)

    def test_missing_credentials_still_use_existing_missing_header_error(self):
        settings = self.settings(allow_no_auth=False)
        authorization = request_authorization(settings, None, [])
        self.assertIsNone(authorization)
        with self.assertRaises(HTTPException) as ctx:
            authenticate(settings, authorization)
        self.assertEqual(401, ctx.exception.status_code)
        self.assertEqual("Missing Authorization header.", ctx.exception.detail)

    def test_no_auth_mode_keeps_existing_behavior(self):
        settings = self.settings(allow_no_auth=True)
        self.assertIsNone(request_authorization(settings, None, ["wrong"]))
        self.assertEqual("no-auth", authenticate(settings, None))
        self.assertEqual("no-auth", authenticate(settings, "Bearer wrong"))


if __name__ == "__main__":
    unittest.main()
