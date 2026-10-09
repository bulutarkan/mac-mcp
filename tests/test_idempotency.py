from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import asyncio
import json
import os
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from mcp.server.fastmcp.exceptions import ToolError

import mcp_server.main as main
from mcp_server import error_contract, idempotency
from tests.test_core_catalog_descriptions import _build_mcp


class IdempotencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mcp = _build_mcp()

    def setUp(self) -> None:
        self.state = tempfile.TemporaryDirectory()
        self.addCleanup(self.state.cleanup)
        patcher = patch.dict(os.environ, {"MAC_MCP_STATE_DIR": self.state.name,
                                          "MAC_MCP_PERMISSION_PROFILE": "trusted"}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = 0

    def _write(self, path: str, content: str, key: str = "write-key-0001"):
        return asyncio.run(self.mcp.call_tool("write_file", {
            "description": "Idempotency check", "path": path, "content": content, "idempotency_key": key,
        }))

    @staticmethod
    def _payload(result) -> dict:
        blocks = result[0] if isinstance(result, tuple) else result
        return json.loads(blocks[0].text)

    def _contract(self, ctx) -> dict:
        contract = error_contract.parse(str(ctx.exception))
        self.assertIsNotNone(contract, str(ctx.exception))
        return contract

    def test_lost_response_repeat_returns_the_first_result_without_running_again(self) -> None:
        target = os.path.join(self.state.name, "out.txt")
        first = self._payload(self._write(target, "one"))
        self.assertTrue(first["ok"])
        self.assertNotIn("idempotent_replay", first)
        os.remove(target)  # a second real write would recreate it
        second = self._payload(self._write(target, "one"))
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(first["path"], second["path"])
        self.assertFalse(os.path.exists(target))

    def test_same_key_with_different_arguments_conflicts(self) -> None:
        target = os.path.join(self.state.name, "out.txt")
        self._write(target, "one")
        with self.assertRaises(ToolError) as ctx:
            self._write(target, "two")
        contract = self._contract(ctx)
        self.assertEqual(("idempotency_key_conflict", "fix_arguments"), (contract["code"], contract["retry"]))
        with open(target) as handle:
            self.assertEqual("one", handle.read())

    def test_unknown_outcome_is_never_replayed(self) -> None:
        def crash(*args, **kwargs):
            self.calls += 1
            raise RuntimeError("connection dropped after the signal was sent")

        with patch.object(main, "kill_process", side_effect=crash):
            for expected in ("tool_failed", "idempotency_outcome_unknown", "idempotency_outcome_unknown"):
                with self.assertRaises(ToolError) as ctx:
                    asyncio.run(self.mcp.call_tool("kill_process", {
                        "description": "Idempotency check", "pid": 999999, "idempotency_key": "kill-key-0001",
                    }))
                contract = self._contract(ctx)
                self.assertEqual(expected, contract["code"])
                self.assertEqual("unknown", contract["outcome"])
                self.assertNotEqual("safe_retry", contract["retry"])
        self.assertEqual(1, self.calls)

    def test_pending_call_is_reported_not_rerun(self) -> None:
        scope = idempotency.scope_for("actor:x", "write_file", "pending-key-01")
        self.assertEqual("new", idempotency.claim(scope, "write_file", "h")[0])
        self.assertEqual("started", idempotency.claim(scope, "write_file", "h")[0])

    def test_refused_before_dispatch_frees_the_key(self) -> None:
        def refuse(*args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise HTTPException(400, "bad path")
            return {"ok": True, "pid": 999999, "signal": "TERM"}

        with patch.object(main, "kill_process", side_effect=refuse):
            arguments = {"description": "Idempotency check", "pid": 999999, "idempotency_key": "kill-key-0002"}
            with self.assertRaises(ToolError):
                asyncio.run(self.mcp.call_tool("kill_process", dict(arguments)))
            result = self._payload(asyncio.run(self.mcp.call_tool("kill_process", dict(arguments))))
        self.assertTrue(result["ok"])
        self.assertEqual(2, self.calls)

    def test_invalid_key_and_read_only_tools(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            self._write(os.path.join(self.state.name, "x.txt"), "x", key="short")
        self.assertEqual("invalid_idempotency_key", self._contract(ctx)["code"])
        target = os.path.join(self.state.name, "r.txt")
        with open(target, "w") as handle:
            handle.write("v1")
        read = lambda: self._payload(asyncio.run(self.mcp.call_tool("read_file", {  # noqa: E731
            "description": "Idempotency check", "path": target, "idempotency_key": "read-key-0001"})))
        self.assertEqual("v1", read()["content"])
        with open(target, "w") as handle:
            handle.write("v2")
        self.assertEqual("v2", read()["content"])  # reads are never served from the journal

    def test_sensitive_or_large_results_are_not_stored(self) -> None:
        scope = idempotency.scope_for("actor:x", "run_command", "secret-key-01")
        idempotency.claim(scope, "run_command", "h")
        secret = "ghp_" + "A" * 36
        self.assertFalse(idempotency.complete(scope, {"content": [{"type": "text", "text": secret}]}))
        state, earlier = idempotency.claim(scope, "run_command", "h")
        self.assertEqual("completed", state)
        replay = idempotency.replay(earlier)
        self.assertFalse(self._payload(replay)["result_retained"])
        mode = stat.S_IMODE(os.stat(idempotency.journal_path()).st_mode)
        self.assertEqual(0o600, mode)

    def test_entries_expire(self) -> None:
        scope = idempotency.scope_for("actor:x", "write_file", "old-key-0001")
        idempotency.claim(scope, "write_file", "h")
        with sqlite3.connect(idempotency.journal_path()) as conn:
            conn.execute("UPDATE calls SET updated_at = 0")
        self.assertEqual("new", idempotency.claim(scope, "write_file", "other")[0])

    def test_key_is_advertised_only_on_state_changing_tools(self) -> None:
        tools = {tool.name: tool for tool in asyncio.run(self.mcp.list_tools())}
        for name in ("write_file", "run_command", "browser_act", "mac_act"):
            self.assertIn("idempotency_key", tools[name].inputSchema["properties"], name)
        for name in ("read_file", "browser_observe", "mac_snapshot", "browser_list_tabs"):
            self.assertNotIn("idempotency_key", tools[name].inputSchema["properties"], name)


if __name__ == "__main__":
    unittest.main()
