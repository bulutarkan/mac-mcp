from __future__ import annotations

import json
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import tools_browser_agent as agent


class RasterizerPatchTests(unittest.TestCase):
    def setUp(self) -> None:
        agent._DOM_RASTERIZER_RUNTIME.clear()
        self.addCleanup(agent._DOM_RASTERIZER_RUNTIME.clear)

    def test_every_dom_sink_and_color_abort_is_patched_once(self) -> None:
        source = agent._dom_rasterizer_source()
        self.assertTrue(source.startswith("var __macMcpTrustedHTML="))
        for original, patched in agent._TRUSTED_TYPES_SINK_PATCHES:
            self.assertNotIn(original, source)
            self.assertEqual(1, source.count(patched))
        self.assertNotIn("Attempting to parse an unsupported color function", source)

    def test_unexpected_vendor_build_fails_closed(self) -> None:
        with patch.object(Path, "read_text", return_value="/* different html2canvas build */"):
            with self.assertRaises(HTTPException) as raised:
                agent._dom_rasterizer_source()
        self.assertEqual(500, raised.exception.status_code)

    def test_safari_loader_copy_is_private_and_identical(self) -> None:
        path = agent._dom_rasterizer_runtime_path()
        self.addCleanup(shutil.rmtree, path.parent, True)
        self.assertEqual(agent._dom_rasterizer_source(), path.read_text(encoding="utf-8"))
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        self.assertEqual(path, agent._dom_rasterizer_runtime_path())


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TrustedTypesPreludeJsTests(unittest.TestCase):
    def _run(self, body: str) -> dict:
        script = (
            "const prelude = " + json.dumps(agent._TRUSTED_TYPES_PRELUDE) + ";\n"
            "const load = (tt) => new Function('window', prelude + '; return [__macMcpTrustedHTML, __macMcpUnsupportedColor];')({trustedTypes: tt});\n"
            "const tt = {createPolicy: (name, rules) => ({name, createHTML: (v) => ({trusted: rules.createHTML(v)})})};\n"
            "const attempt = (fn) => { try { return fn(); } catch (e) { return 'throws'; } };\n"
            + body
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "t.js"
            path.write_text(script, encoding="utf-8")
            out = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=20, check=True)
        return json.loads(out.stdout)

    def test_policy_accepts_only_the_rasterizer_shell(self) -> None:
        result = self._run(
            "const [html] = load(tt);\n"
            "console.log(JSON.stringify({\n"
            "  shell: attempt(() => html('<!DOCTYPE html><html></html>')),\n"
            "  bare: attempt(() => html('<html></html>')),\n"
            "  emoji: attempt(() => html('&#128104;'.repeat(10))),\n"
            "  script: attempt(() => html('<img src=x onerror=alert(1)>')),\n"
            "  smuggled: attempt(() => html('<!DOCTYPE html><html><script>x</script></html>')),\n"
            "  blocked: load({createPolicy() { throw new TypeError('no'); }})[0]('<html></html>'),\n"
            "}));"
        )
        self.assertEqual({"trusted": "<!DOCTYPE html><html></html>"}, result["shell"])
        self.assertEqual({"trusted": "<html></html>"}, result["bare"])
        self.assertIn("trusted", result["emoji"])
        self.assertEqual("throws", result["script"])
        self.assertEqual("throws", result["smuggled"])
        self.assertEqual("<html></html>", result["blocked"])

    def test_css_color4_values_fall_back_instead_of_aborting(self) -> None:
        result = self._run(
            "const [, color] = load(undefined);\n"
            "const n = (v) => ({type: 17, number: v}), p = (v) => ({type: 16, number: v});\n"
            "console.log(JSON.stringify({\n"
            "  red: color({name: 'color', values: [{type: 20, value: 'srgb'}, n(1), n(0), n(0)]}),\n"
            "  half: color({name: 'color', values: [{type: 20, value: 'srgb'}, n(0), n(0.5), n(1), n(0.5)]}),\n"
            "  pct: color({name: 'color', values: [p(100), p(0), p(0)]}),\n"
            "  oklch: color({name: 'oklch', values: [p(70), n(0.1), n(200)]}),\n"
            "  junk: color(null),\n"
            "}));"
        )
        self.assertEqual(0xFF0000FF, result["red"])
        self.assertEqual(0x0080FF80, result["half"])
        self.assertEqual(0xFF0000FF, result["pct"])
        self.assertEqual(0x808080FF, result["oklch"])
        self.assertEqual(0x808080FF, result["junk"])


class TrustedTypesErrorReportingTests(unittest.TestCase):
    def test_trusted_types_failure_has_explicit_reason_code(self) -> None:
        responses = iter([
            json.dumps({"ok": True, "status": "running"}),
            json.dumps({"status": "error", "error": "This assignment requires a TrustedHTML"}),
            "cleaned",
        ])
        with patch.object(agent, "_ensure_dom_rasterizer"), \
                patch.object(agent, "_wait_for_render_readiness", return_value={"ready": True}), \
                patch.object(agent, "_execute_js_unbounded", side_effect=lambda *a, **k: next(responses)):
            image, error, meta = agent._capture_dom_visual_locked("Safari", "viewport", None, 1, 1, "tab-1")
        self.assertIsNone(image)
        self.assertEqual("TRUSTED_TYPES_BLOCKED", meta["reason_code"])
        self.assertIn("Trusted Types", error)


if __name__ == "__main__":
    unittest.main()
