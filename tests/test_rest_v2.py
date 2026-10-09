from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp.server.fastmcp import FastMCP
from starlette.testclient import TestClient

import mcp_server.main as main
from mcp_server import rest_v2
from mcp_server.version import __version__
from tests.test_core_catalog_descriptions import _build_mcp


class RestV2SchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mcp = _build_mcp()
        cls.tools = asyncio.run(rest_v2.contract_tools(cls.mcp))
        cls.published = json.loads(rest_v2.SCHEMA_PATH.read_text(encoding="utf-8"))

    def test_published_schema_matches_the_mcp_contract(self) -> None:
        generated = rest_v2.render(rest_v2.build_openapi(self.tools, product_version=__version__))
        self.assertEqual(
            generated, rest_v2.SCHEMA_PATH.read_text(encoding="utf-8"),
            "openapi/mac-mcp-v2.json drifted from the MCP tools; run: python -m mcp_server.rest_v2 --write",
        )

    def test_every_operation_is_a_registered_mcp_tool_with_the_same_inputs(self) -> None:
        registered = {tool.name: tool for tool in asyncio.run(FastMCP.list_tools(self.mcp))}
        for path, item in self.published["paths"].items():
            name = item["post"]["operationId"]
            with self.subTest(path=path):
                self.assertEqual(f"/api/v2/{name}", path)
                self.assertIn(name, registered)
                body = item["post"]["requestBody"]["content"]["application/json"]["schema"]
                mcp_properties = set(registered[name].inputSchema.get("properties") or {})
                self.assertEqual(mcp_properties, set(body["properties"]) - {"description", "idempotency_key"})
                self.assertEqual(set(registered[name].inputSchema.get("required") or []) - {"description"},
                                 set(body.get("required") or []))
        self.assertIn("idempotency_key", self.published["paths"]["/api/v2/write_file"]["post"]["requestBody"]
                      ["content"]["application/json"]["schema"]["properties"])
        self.assertNotIn("idempotency_key", self.published["paths"]["/api/v2/read_file"]["post"]["requestBody"]
                         ["content"]["application/json"]["schema"]["properties"])

    def test_api_and_product_versions_are_separate(self) -> None:
        self.assertEqual(rest_v2.API_VERSION, self.published["info"]["version"])
        self.assertEqual(__version__, self.published["info"]["x-product-version"])
        self.assertNotIn("$defs", json.dumps(self.published))
        self.assertNotIn(str(Path.home()), json.dumps(self.published))


class RestV2RouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(main.create_app(), base_url="http://127.0.0.1:8765")
        cls.auth = {"authorization": "Bearer " + os.environ["MCP_API_KEY"]}

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.dict(os.environ, {"MAC_MCP_PERMISSION_PROFILE": "trusted",
                                          "MAC_MCP_STATE_DIR": self.tmp.name}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _post(self, tool: str, body, headers=None):
        return self.client.post(f"/api/v2/{tool}", headers=self.auth if headers is None else headers, json=body)

    def test_completed_calls_return_the_tool_result(self) -> None:
        target = Path(self.tmp.name) / "a.txt"
        target.write_text("hello")
        read = self._post("read_file", {"path": str(target)})
        self.assertEqual(200, read.status_code, read.text)
        self.assertEqual("hello", read.json()["content"])
        shell = self._post("run_command", {"command": "exit 3"})
        self.assertEqual(200, shell.status_code)
        self.assertEqual(3, shell.json()["exit_code"])

    def test_failures_carry_the_error_contract(self) -> None:
        missing = self._post("run_command", {})
        self.assertEqual(422, missing.status_code)
        self.assertEqual("invalid_arguments", missing.json()["error"]["code"])
        bad_body = self.client.post("/api/v2/run_command", headers=self.auth, json=[1, 2])
        self.assertEqual(422, bad_body.status_code)
        not_found = self._post("get_job_status", {"job_id": "job_does_not_exist"})
        self.assertEqual(404, not_found.status_code)
        self.assertEqual(("not_found", "fix_arguments"),
                         (not_found.json()["error"]["code"], not_found.json()["error"]["retry"]))
        unauthenticated = self._post("read_file", {"path": "/tmp"}, headers={})
        self.assertEqual(401, unauthenticated.status_code)

    def test_policy_and_idempotency_run_through_the_mcp_path(self) -> None:
        with patch.dict(os.environ, {"MAC_MCP_PERMISSION_PROFILE": "read_only"}):
            denied = self._post("run_command", {"command": "true"})
        self.assertEqual(403, denied.status_code)
        self.assertEqual("profile_denied", denied.json()["error"]["code"])
        target = Path(self.tmp.name) / "w.txt"
        body = {"path": str(target), "content": "one", "idempotency_key": "rest-v2-key-01"}
        self.assertEqual(200, self._post("write_file", body).status_code)
        target.unlink()
        replay = self._post("write_file", body).json()
        self.assertTrue(replay["idempotent_replay"])
        self.assertFalse(target.exists())

    def test_v1_routes_keep_working(self) -> None:
        target = Path(self.tmp.name) / "v1.txt"
        target.write_text("legacy")
        response = self.client.post("/api/files", headers=self.auth, json={"tool": "read_file", "path": str(target)})
        self.assertEqual(200, response.status_code)
        self.assertEqual("legacy", response.json()["content"])


if __name__ == "__main__":
    unittest.main()
