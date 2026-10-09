"""Deterministic catalog/schema/result contract checks (no server, no user state)."""
from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import asyncio
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException

from mcp_server import error_contract, result_pages, rest_v2, tool_manifest, tools_files
from tests import _state_isolation
from tests.test_core_catalog_descriptions import _build_mcp

V1_SCHEMA = Path(__file__).resolve().parents[1] / "openapi" / "custom-gpt-actions.json"


def _tool(name: str, description: str = "Does a thing.", properties=None, required=()):
    schema = {"type": "object", "properties": dict(properties or {}), "required": list(required)}
    return SimpleNamespace(name=name, description=description, inputSchema=schema)


class CatalogContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tools = asyncio.run(rest_v2.contract_tools(_build_mcp()))
        v1 = json.loads(V1_SCHEMA.read_text(encoding="utf-8"))
        cls.v1_operations = [item["post"]["operationId"] for item in v1["paths"].values()]
        cls.v2 = json.loads(rest_v2.SCHEMA_PATH.read_text(encoding="utf-8"))
        cls.manifest = tool_manifest.build_manifest(
            cls.tools, v1_operations=cls.v1_operations, v2_operations=rest_v2.V2_TOOLS,
        )

    def test_runs_against_isolated_state(self) -> None:
        self.assertTrue(os.environ["MAC_MCP_STATE_DIR"].startswith(_state_isolation.ROOT))
        self.assertTrue(os.environ["MAC_MCP_TELEMETRY_DIR"].startswith(_state_isolation.ROOT))

    def test_registered_catalog_meets_its_contract(self) -> None:
        problems = tool_manifest.contract_problems(
            self.manifest, v2_document=self.v2, v1_operations=self.v1_operations,
        )
        self.assertEqual([], problems)

    def test_manifest_is_plain_data(self) -> None:
        json.dumps(self.manifest)
        names = [entry["name"] for entry in self.manifest]
        self.assertEqual(sorted(names), names)
        act = next(entry for entry in self.manifest if entry["name"] == "browser_act")
        self.assertEqual(["compact", "full", "none"], sorted(act["enums"]["return_state"]))
        self.assertTrue(act["side_effect"] and act["advertises_idempotency_key"] and act["rest_v2"])

    def test_checks_catch_clipped_descriptions_and_bad_enums(self) -> None:
        manifest = tool_manifest.build_manifest([
            _tool("browser_act", "x" * 400),
            _tool("browser_observe", properties={"scope": {"type": "string", "enum": ["a"], "default": "b"}},
                  required=["missing"]),
        ])
        manifest[0]["compact_description"] = "y" * 217 + "..."
        problems = "\n".join(tool_manifest.contract_problems(manifest))
        self.assertIn("browser_act: compact description is clipped", problems)
        self.assertIn("default 'b' of 'scope' is not one of its enum values", problems)
        self.assertIn("required field 'missing' is not an input", problems)
        self.assertIn("idempotency_key advertised=False but side_effect=True", problems)

    def test_checks_catch_rest_drift(self) -> None:
        drifted = copy.deepcopy(self.v2)
        body = drifted["paths"]["/api/v2/browser_act"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        body["properties"]["return_state"]["enum"] = ["none"]
        body["properties"].pop("observation_id")
        drifted["paths"]["/api/v2/not_a_tool"] = {"post": {"operationId": "not_a_tool"}}
        problems = "\n".join(tool_manifest.contract_problems(
            self.manifest, v2_document=drifted, v1_operations=[*self.v1_operations, "ghost_operation"],
        ))
        self.assertIn("REST v2 browser_act: inputs differ from the MCP tool", problems)
        self.assertIn("REST v2 browser_act: enum 'return_state' differs", problems)
        self.assertIn("'not_a_tool' is not a registered MCP tool", problems)
        self.assertIn("REST v1 ghost_operation: not a registered MCP tool", problems)


class ResultShapeContractTests(unittest.TestCase):
    def test_page_metadata_shape(self) -> None:
        self.assertEqual({"limit", "returned", "has_more", "next_cursor"},
                         set(result_pages.page_meta(10, 10, True, "c")))
        self.assertIsNone(result_pages.page_meta(10, 3, False, "ignored")["next_cursor"])

    def test_error_contract_shape(self) -> None:
        contract = error_contract.describe(HTTPException(409, {"error": "stale_tab_handle"}), tool="browser_act")
        self.assertTrue({"code", "stage", "outcome", "retry", "http_status", "message", "tool"} <= set(contract))
        self.assertIn(contract["stage"], {"validation", "policy", "preflight", "execution"})
        self.assertIn(contract["outcome"], {"not_executed", "completed", "unknown"})
        self.assertIn(contract["retry"], {"fix_arguments", "safe_retry", "observe_again", "wait_for_user", "never_retry"})
        for code, spec in error_contract.REGISTRY.items():
            with self.subTest(code=code):
                self.assertFalse(spec.outcome == "unknown" and spec.retry == "safe_retry")

    def test_truncated_multi_file_result_shape(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            paths = []
            for index in range(3):
                path = Path(td) / f"{index}.txt"
                path.write_text("z\n" * 1_500)
                paths.append(str(path))
            result = tools_files.read_multiple_files(None, paths, max_total_chars=4_000)
        self.assertEqual({"ok", "files", "budget", "truncated", "not_read"}, set(result))
        self.assertTrue(result["truncated"])
        cut = next(item for item in result["files"] if item.get("truncated"))
        self.assertEqual({"reason", "limit_chars"}, set(cut["truncation"]))
        self.assertEqual({"tool", "path", "offset"}, set(cut["continue"]))
        skipped = [item for item in result["files"] if item["status"] == "skipped"]
        self.assertEqual([item["path"] for item in skipped], result["not_read"])


if __name__ == "__main__":
    unittest.main()
