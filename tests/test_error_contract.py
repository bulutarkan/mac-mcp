from __future__ import annotations

import asyncio
import json
import os
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from mcp.server.fastmcp.exceptions import ToolError
from starlette.testclient import TestClient

import mcp_server.main as main
from mcp_server import error_contract
from mcp_server.workflow_checkpoints import mark_not_executed
from tests.test_core_catalog_descriptions import _build_mcp

_FIELDS = ("code", "stage", "outcome", "retry")


def _key(contract: dict) -> tuple:
    return tuple(contract[field] for field in _FIELDS)


class DescribeTests(unittest.TestCase):
    def test_unknown_outcome_is_never_a_safe_retry(self) -> None:
        crash = error_contract.describe(RuntimeError("boom"), tool="write_file", mutating=True)
        self.assertEqual(("tool_failed", "execution", "unknown", "observe_again"), _key(crash))
        timeout = error_contract.describe(HTTPException(504, "slow"), tool="browser_act", mutating=True)
        self.assertEqual(("timeout", "unknown", "observe_again"), (timeout["code"], timeout["outcome"], timeout["retry"]))
        for status in (408, 409, 500, 502, 503, 504):
            with self.subTest(status=status):
                contract = error_contract.describe(HTTPException(status, "x"), mutating=True)
                self.assertFalse(contract["outcome"] == "unknown" and contract["retry"] == "safe_retry")

    def test_read_only_tools_and_marked_refusals_are_not_executed(self) -> None:
        read = error_contract.describe(RuntimeError("boom"), tool="read_file", mutating=False)
        self.assertEqual(("not_executed", "safe_retry"), (read["outcome"], read["retry"]))
        refused = mark_not_executed(HTTPException(500, "refused"))
        self.assertEqual("not_executed", error_contract.describe(refused, mutating=True)["outcome"])

    def test_tool_codes_come_from_structured_details(self) -> None:
        stale = HTTPException(409, {"error": "stale_tab_handle", "message": "tab moved"})
        contract = error_contract.describe(stale, tool="browser_act")
        self.assertEqual(("stale_tab_handle", "execution", "not_executed", "observe_again"), _key(contract))
        self.assertEqual("tab moved", contract["message"])
        policy = error_contract.describe(ToolError("scope_denied: tool=x; reasons=y"))
        self.assertEqual(("scope_denied", "policy", "not_executed", "never_retry"), _key(policy))

    def test_annotate_and_parse_round_trip(self) -> None:
        contract = error_contract.describe(HTTPException(404, "File not found"), tool="read_file", mutating=False)
        text = error_contract.annotate("Error executing tool read_file: 404: File not found", contract)
        self.assertTrue(text.startswith("Error executing tool read_file"))
        self.assertEqual(contract, error_contract.parse(text))
        self.assertTrue(error_contract.has_contract(ToolError(text)))
        self.assertTrue(error_contract.has_contract(ToolError("mac_mcp_steering_preempted: {}")))


class TransportParityTests(unittest.TestCase):
    """The same failure carries the same code/stage/outcome/retry over MCP, tool_invoke and REST."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mcp = _build_mcp()
        cls.client = TestClient(main.create_app(), base_url="http://127.0.0.1:8765")
        cls.auth = {"authorization": "Bearer " + os.environ["MCP_API_KEY"]}

    def _profile(self, name: str) -> None:
        patcher = patch.dict(os.environ, {"MAC_MCP_PERMISSION_PROFILE": name}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _mcp_error(self, tool: str, arguments: dict) -> dict:
        with self.assertRaises(ToolError) as ctx:
            asyncio.run(self.mcp.call_tool(tool, {"description": "Contract check", **arguments}))
        contract = error_contract.parse(str(ctx.exception))
        self.assertIsNotNone(contract, str(ctx.exception)[:300])
        self.assertEqual(1, str(ctx.exception).count(error_contract.CONTRACT_PREFIX))
        return contract

    def _invoke_error(self, tool: str, arguments: dict) -> dict:
        return self._mcp_error("tool_invoke", {"tool_name": tool, "arguments": {"description": "Contract check", **arguments}})

    def _rest_error(self, path: str, body: dict, status: int) -> dict:
        response = self.client.post(path, headers=self.auth, json=body)
        self.assertEqual(status, response.status_code, response.text[:300])
        payload = response.json()
        self.assertIn("detail", payload)
        return payload["error"]

    def test_invalid_arguments(self) -> None:
        self._profile("trusted")
        mcp = self._mcp_error("run_command", {})
        invoke = self._invoke_error("run_command", {})
        rest = self._rest_error("/api/run", {}, 422)
        self.assertEqual(("invalid_arguments", "validation", "not_executed", "fix_arguments"), _key(mcp))
        self.assertEqual(_key(mcp), _key(invoke))
        self.assertEqual(_key(mcp), _key(rest))
        self.assertEqual("run_command", invoke["tool"])

    def test_policy_denial(self) -> None:
        self._profile("read_only")
        mcp = self._mcp_error("run_command", {"command": "true"})
        rest = self._rest_error("/api/run", {"command": "true"}, 403)
        self.assertEqual(("profile_denied", "policy", "not_executed", "never_retry"), _key(mcp))
        self.assertEqual(_key(mcp), _key(rest))

    def test_tool_reported_not_found(self) -> None:
        self._profile("trusted")
        mcp = self._mcp_error("get_job_status", {"job_id": "job_does_not_exist"})
        invoke = self._invoke_error("get_job_status", {"job_id": "job_does_not_exist"})
        rest = self._rest_error("/api/jobs/status", {"job_id": "job_does_not_exist"}, 404)
        self.assertEqual("not_found", mcp["code"])
        self.assertEqual("fix_arguments", mcp["retry"])
        self.assertEqual(_key(mcp), _key(invoke))
        self.assertEqual(_key(mcp), _key(rest))

    def test_crash_in_a_mutating_tool_is_an_unknown_outcome(self) -> None:
        self._profile("trusted")
        with patch.object(main, "kill_process", side_effect=RuntimeError("signal failed midway")):
            mcp = self._mcp_error("kill_process", {"pid": 999999})
            invoke = self._invoke_error("kill_process", {"pid": 999999})
        self.assertEqual(("tool_failed", "execution", "unknown", "observe_again"), _key(mcp))
        self.assertEqual(_key(mcp), _key(invoke))

    def test_nonzero_shell_exit_stays_a_completed_result(self) -> None:
        self._profile("trusted")
        result = asyncio.run(self.mcp.call_tool("run_command", {"description": "Contract check", "command": "exit 3"}))
        blocks = result[0] if isinstance(result, tuple) else result
        payload = json.loads(blocks[0].text)
        self.assertFalse(payload["ok"])
        self.assertEqual(3, payload.get("exit_code", payload.get("returncode")))
        response = self.client.post("/api/run", headers=self.auth, json={"command": "exit 3"})
        self.assertEqual(200, response.status_code)
        self.assertNotIn("error_contract", response.text)


if __name__ == "__main__":
    unittest.main()
