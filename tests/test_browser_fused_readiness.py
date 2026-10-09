from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from mcp_server import tools_browser_agent as agent

READY = {"ready": True, "stable_for_ms": 400, "rect": {"x": 1, "y": 1, "w": 10, "h": 10}}
BATCH = {"ok": True, "actions": [{"ok": True, "type": "type", "element_id": "e_1", "effect_observed": True}]}


class FusedReadinessTests(unittest.TestCase):
    def test_a_ready_target_is_checked_and_acted_on_in_one_call(self) -> None:
        with patch.object(agent, "_run_json_js", return_value={"fused": True, "ready": True, "readiness": READY, "batch": BATCH}) as run, \
             patch.object(agent, "_wait_for_element_readiness") as stepwise:
            result = agent._verified_dom_action(MagicMock(), "Safari", {"type": "type", "element_id": "e_1", "text": "x"}, None, 1, 1, "tab")
        self.assertTrue(result["ok"])
        self.assertTrue(result["readiness_fused"])
        self.assertEqual(1, result["_js_calls"])
        self.assertEqual(1, run.call_count)
        stepwise.assert_not_called()
        self.assertEqual({"readiness_ms", "action_ms", "verify_ms"}, set(result["phase_ms"]))

    def test_a_target_that_is_not_ready_falls_back_to_bounded_polling(self) -> None:
        replies = [{"fused": True, "ready": False, "readiness": {"ready": False}}, BATCH]
        with patch.object(agent, "_run_json_js", side_effect=replies) as run, \
             patch.object(agent, "_wait_for_element_readiness", return_value={**READY, "_js_calls": 2}) as stepwise:
            result = agent._verified_dom_action(MagicMock(), "Safari", {"type": "type", "element_id": "e_1", "text": "x"}, None, 1, 1, "tab")
        self.assertTrue(result["ok"])
        self.assertNotIn("readiness_fused", result)
        stepwise.assert_called_once()
        self.assertEqual(2, run.call_count)
        self.assertEqual(4, result["_js_calls"])

    def test_still_not_ready_after_polling_never_acts(self) -> None:
        with patch.object(agent, "_run_json_js", return_value={"fused": True, "ready": False, "readiness": {}}) as run, \
             patch.object(agent, "_wait_for_element_readiness", return_value={"ready": False, "reason_code": "ELEMENT_OCCLUDED", "_js_calls": 3}):
            result = agent._verified_dom_action(MagicMock(), "Safari", {"type": "click", "element_id": "e_1"}, None, 1, 1, "tab")
        self.assertEqual(("element_not_ready", "ELEMENT_OCCLUDED"), (result["error"], result["reason_code"]))
        self.assertEqual(1, run.call_count, "only the fused check ran; no separate action call")

    def test_delegated_revalidation_happens_before_the_fused_call(self) -> None:
        blocked = {"ok": False, "error": "stale_tab_handle", "reason_code": "STALE_TAB_HANDLE"}
        revalidate = MagicMock(return_value=(None, blocked))
        with patch.object(agent, "_run_json_js") as run:
            result = agent._verified_dom_action(
                MagicMock(), "Safari", {"type": "click", "element_id": "e_1"}, None, 1, 1, "tab",
                mutation_revalidator=revalidate,
            )
        self.assertEqual("STALE_TAB_HANDLE", result["reason_code"])
        run.assert_not_called()

    def test_trusted_input_keeps_the_stepwise_path(self) -> None:
        with patch.object(agent, "_try_fused_action") as fused, \
             patch.object(agent, "_wait_for_element_readiness", return_value={"ready": False, "_js_calls": 1}):
            agent._verified_dom_action(MagicMock(), "Google Chrome", {"type": "click", "element_id": "e_1", "input_mode": "trusted"}, None, 1, 1, "tab")
        fused.assert_not_called()

    def test_script_acts_only_after_readiness_and_passes_the_missing_library_sentinel(self) -> None:
        js = agent._ready_then_batch_js({"type": "click", "element_id": "e_1"}, None, "click", 300)
        self.assertLess(js.index("if(!ready.ready)return"), js.index("var done="))
        self.assertEqual(2, js.count(agent._BOOT_BEGIN))
        light = agent._with_binding_only(js)
        self.assertEqual(0, light.count("function __mcpRoots"))
        self.assertEqual(2, light.count("__mcpApi.__mcpRoots"))
        self.assertIn("if(raw==='" + agent._AGENT_API_MISSING + "')return raw;", js)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_fused_script_is_valid_javascript(self) -> None:
        js = agent._ready_then_batch_js({"type": "type", "element_id": "e_1", "text": "a"}, "obs", "type", 300)
        with tempfile.TemporaryDirectory() as td:
            for name, code in (("full.js", js), ("binding.js", agent._with_binding_only(js))):
                path = Path(td) / name
                path.write_text(code, encoding="utf-8")
                result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
                self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
