from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import browser_tabs, tools_browser_agent as agent

TARGET = browser_tabs.TabTarget(
    browser="Safari", window_index=1, tab_index=1, tab_handle="btab_safari_x", native_id="777",
    title="T", url="https://example.com/", active=False,
)


class HelperInstallOnceTests(unittest.TestCase):
    def setUp(self) -> None:
        agent._WARM_DOCUMENTS.clear()
        self.sent: list[str] = []

    def run_js(self, js: str, replies: list[str]) -> str:
        def raw(_browser, script, _target, _timeout):
            self.sent.append(script)
            return replies.pop(0)

        with patch.object(agent, "_raw_execute_js_for_target", side_effect=raw):
            return agent._execute_js_for_target("Safari", js, TARGET, 5)

    def test_builders_still_emit_complete_scripts(self) -> None:
        js = agent._light_state_js()
        api = agent._agent_api()
        self.assertIn(api["full"], js)
        self.assertEqual(56, len(api["names"]))
        self.assertEqual(api["names"], agent._top_level_function_names(agent._bootstrap_functions_source()))
        for name in api["names"]:
            self.assertIn(f"{name}=__mcpApi.{name}", api["binding"])
        self.assertLess(len(api["binding"]), 4096)

    def test_first_call_sends_the_library_then_only_the_binding(self) -> None:
        js = agent._light_state_js()
        self.assertEqual("first", self.run_js(js, ["first"]))
        self.assertEqual("second", self.run_js(js, ["second"]))
        self.assertIn(agent._bootstrap_functions_source().strip()[:200], self.sent[0])
        self.assertNotIn("function __mcpRoots", self.sent[1])
        self.assertIn("__mcpApi.__mcpRoots", self.sent[1])
        self.assertLess(len(self.sent[1]), len(self.sent[0]) / 5)

    def test_a_reloaded_document_gets_the_library_again(self) -> None:
        js = agent._light_state_js()
        self.run_js(js, ["warm-up"])
        out = self.run_js(js, [agent._AGENT_API_MISSING, "after reload"])
        self.assertEqual("after reload", out)
        self.assertEqual(3, len(self.sent))
        self.assertIn("function __mcpRoots", self.sent[2])
        self.assertEqual("again", self.run_js(js, ["again"]))
        self.assertNotIn("function __mcpRoots", self.sent[3])

    def test_scripts_without_the_library_pass_through(self) -> None:
        self.assertEqual("ok", self.run_js("'OK'", ["ok"]))
        self.assertEqual(["'OK'"], self.sent)
        self.assertEqual({}, dict(agent._WARM_DOCUMENTS))

    def test_warm_document_memory_is_bounded(self) -> None:
        js = agent._light_state_js()
        with patch.object(agent, "_WARM_DOCUMENT_LIMIT", 3), \
             patch.object(agent, "_raw_execute_js_for_target", return_value="x"):
            for index in range(5):
                target = browser_tabs.TabTarget(**{**TARGET.__dict__, "native_id": str(index)})
                agent._execute_js_for_target("Safari", js, target, 5)
        self.assertEqual([("Safari", "2", ""), ("Safari", "3", ""), ("Safari", "4", "")], list(agent._WARM_DOCUMENTS))

    def test_transport_metrics_separate_sent_bytes_and_injections(self) -> None:
        js = agent._light_state_js()
        with agent._measure_transport() as stats:
            self.run_js(js, ["aaaa"])
            self.run_js(js, ["bb"])
        self.assertEqual(2, stats["js_calls"])
        self.assertEqual(1, stats["helper_injections"])
        self.assertEqual(6, stats["received_bytes"])
        self.assertEqual(sum(len(s.encode()) for s in self.sent), stats["sent_bytes"])

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_complete_and_binding_scripts_are_valid_javascript(self) -> None:
        builders = [
            agent._light_state_js(),
            agent._observe_js("interactive", 50),
            agent._element_effect_state_js("e_1"),
            agent._network_idle_state_js(),
        ]
        with tempfile.TemporaryDirectory() as td:
            for index, js in enumerate(builders):
                for variant, code in (("full", js), ("binding", agent._with_binding_only(js))):
                    path = Path(td) / f"{index}-{variant}.js"
                    path.write_text(code, encoding="utf-8")
                    result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
                    self.assertEqual(0, result.returncode, f"{index} {variant}: {result.stderr}")


if __name__ == "__main__":
    unittest.main()
