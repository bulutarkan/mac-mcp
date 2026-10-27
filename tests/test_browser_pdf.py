"""#137: {type:'pdf'} saves a tab as a PDF artifact in the background, never bringing anything forward."""
from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import base64
import json
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from mcp_server import browser_pdf
from mcp_server import tools_browser_agent as agent

ROOT = Path(__file__).resolve().parents[1]
PDF = b"%PDF-1.4\n%fake\n"


class SnapshotPdfTests(unittest.TestCase):
    def _page(self, html: str, fail_at: int = -1):
        calls = []

        def run_js(options):
            calls.append(dict(options))
            if options["phase"] == "start":
                return {"ok": True, "token": "tk", "length": len(html), "url": "https://shop.test/order/7",
                        "title": "Order #7 / receipt", "width": 1024}
            if options["phase"] == "chunk":
                if len([c for c in calls if c["phase"] == "chunk"]) - 1 == fail_at:
                    return {"ok": False, "error": "snapshot_gone"}
                return {"ok": True, "text": html[options["offset"]:options["offset"] + options["size"]]}
            return {"ok": True}
        return run_js, calls

    def _helper_writes_pdf(self, sent):
        def run(argv, input, **_kwargs):  # noqa: A002 - subprocess keyword
            request = json.loads(input)
            sent.append(request)
            Path(request["out"]).write_bytes(PDF)
            return subprocess.CompletedProcess(argv, 0, stdout='{"ok":true,"bytes":15}\n', stderr="")
        return run

    def test_the_page_is_read_in_parts_rendered_off_screen_and_registered(self) -> None:
        html = "<html><body>" + "x" * 45_000 + "</body></html>"
        run_js, calls = self._page(html)
        sent = []
        with patch.object(browser_pdf, "_helper", return_value=Path("/bin/true")), \
                patch.object(browser_pdf.subprocess, "run", side_effect=self._helper_writes_pdf(sent)):
            out = browser_pdf.snapshot_pdf(run_js)
        self.assertTrue(out["ok"], out)
        self.assertEqual(("webkit_snapshot", True), (out["method"], out["background"]))
        self.assertEqual(html, sent[0]["html"])
        self.assertEqual(("https://shop.test/order/7", 1024.0), (sent[0]["base_url"], sent[0]["width"]))
        self.assertEqual(["start", "chunk", "chunk", "chunk", "clear"], [c["phase"] for c in calls])
        path = Path(out["path"])
        self.assertEqual(PDF, path.read_bytes())
        self.assertTrue(path.name.startswith("Order-7-receipt-"))
        self.assertEqual(browser_pdf.pdf_dir().resolve(), path.parent)
        self.assertRegex(out["artifact_id"], r"^artifact_[0-9a-f]{16}$")
        self.assertEqual(len(PDF), out["size"])

    def test_a_page_that_changes_while_read_fails_and_forgets_the_snapshot(self) -> None:
        run_js, calls = self._page("y" * 50_000, fail_at=1)
        with patch.object(browser_pdf.subprocess, "run", side_effect=AssertionError("rendered")):
            out = browser_pdf.snapshot_pdf(run_js)
        self.assertEqual("pdf_failed", out["error"])
        self.assertEqual("clear", calls[-1]["phase"])

    def test_too_large_and_helper_failures_are_clear_errors(self) -> None:
        out = browser_pdf.snapshot_pdf(lambda _o: {"ok": False, "error": "page_too_large"})
        self.assertEqual("pdf_page_too_large", out["error"])
        run_js, _calls = self._page("<p>x</p>")
        with patch.object(browser_pdf, "_helper", side_effect=RuntimeError("swiftc is unavailable")):
            self.assertEqual("pdf_unavailable", browser_pdf.snapshot_pdf(run_js)["error"])


class PdfRoutingTests(unittest.TestCase):
    def _route(self, browser: str, connected: bool, supports: bool):
        bridge = MagicMock()
        bridge.is_connected.return_value = connected
        bridge.supports.return_value = supports
        with patch.object(agent, "chrome_background_bridge", bridge), \
                patch.object(agent, "_chrome_native_id", return_value="42"), \
                patch.object(agent, "_run_json_js", return_value={"url": "u", "title": "t"}), \
                patch.object(browser_pdf, "chrome_print_pdf", return_value={"ok": True, "method": "chrome_print"}) as chrome, \
                patch.object(browser_pdf, "snapshot_pdf", return_value={"ok": True, "method": "webkit_snapshot"}) as snap:
            out = agent._pdf_action(MagicMock(), browser, 1, 1, "tab")
        return out["method"], chrome, snap

    def test_chrome_prints_with_its_own_engine_only_when_the_companion_can(self) -> None:
        method, chrome, _snap = self._route("Google Chrome", True, True)
        self.assertEqual("chrome_print", method)
        chrome.assert_called_once()
        self.assertEqual("42", chrome.call_args.args[0])
        self.assertEqual("webkit_snapshot", self._route("Google Chrome", True, False)[0])
        self.assertEqual("webkit_snapshot", self._route("Google Chrome", False, False)[0])
        self.assertEqual("webkit_snapshot", self._route("Safari", True, True)[0])

    def test_a_failed_chrome_print_falls_back_to_the_page_copy(self) -> None:
        from fastapi import HTTPException

        bridge = MagicMock()
        bridge.is_connected.return_value = True
        bridge.supports.return_value = True
        with patch.object(agent, "chrome_background_bridge", bridge), \
                patch.object(agent, "_chrome_native_id", return_value="42"), \
                patch.object(agent, "_run_json_js", return_value={"url": "u", "title": "t"}), \
                patch.object(browser_pdf, "chrome_print_pdf", side_effect=HTTPException(502, {"error": "pdf_too_large"})), \
                patch.object(browser_pdf, "snapshot_pdf", return_value={"ok": True, "method": "webkit_snapshot", "_js_calls": 4}):
            out = agent._pdf_action(MagicMock(), "Google Chrome", 1, 1, "tab")
        self.assertEqual(("webkit_snapshot", "pdf_too_large", 5), (out["method"], out["chrome_print_error"], out["_js_calls"]))

    def test_chrome_print_writes_a_new_file(self) -> None:
        bridge = MagicMock()
        bridge.request_print_pdf.return_value = {"ok": True, "data": base64.b64encode(PDF).decode()}
        with patch.object(browser_pdf, "chrome_background_bridge", bridge):
            out = browser_pdf.chrome_print_pdf("42", {"url": "https://a.test/", "title": "A"})
        self.assertEqual(("chrome_print", PDF), (out["method"], Path(out["path"]).read_bytes()))
        bridge.request_print_pdf.return_value = {"ok": True, "data": base64.b64encode(b"<html>").decode()}
        with patch.object(browser_pdf, "chrome_background_bridge", bridge):
            self.assertEqual("pdf_failed", browser_pdf.chrome_print_pdf("42", {"title": "A"})["error"])

    def test_companion_prints_without_activating_the_tab(self) -> None:
        worker = (ROOT / "menu_app" / "ChromeVisualCompanion" / "background.js").read_text(encoding="utf-8")
        body = worker[worker.index("async function handlePrintPdf"):worker.index("function connect(")]
        self.assertIn("'Page.printToPDF'", body)
        self.assertIn("print_pdf: handlePrintPdf", worker)
        for focus in ("chrome.tabs.update", "chrome.windows.update", "active: true", "focused"):
            self.assertNotIn(focus, body)

    def test_snapshot_never_carries_page_scripts_or_passwords(self) -> None:
        script = (ROOT / "mcp_server" / "browser_js" / "page_snapshot.js").read_text(encoding="utf-8")
        self.assertIn("querySelectorAll('script,noscript,", script)
        self.assertIn("t==='password'", script)
        helper = (ROOT / "mcp_server" / "page_pdf.swift").read_text(encoding="utf-8")
        for rule in ("allowsContentJavaScript = false", ".nonPersistent()", "setActivationPolicy(.prohibited)"):
            self.assertIn(rule, helper)


@unittest.skipUnless(shutil.which("swiftc"), "swiftc is not installed")
class PagePdfHelperTests(unittest.TestCase):
    def test_helper_renders_html_to_a_pdf_off_screen(self) -> None:
        out = browser_pdf.pdf_dir() / "helper-check.pdf"
        out.unlink(missing_ok=True)
        request = {"html": "<h1>Mac MCP</h1><p>PDF</p><script>document.title='ran'</script>",
                   "base_url": "https://example.invalid/", "out": str(out), "width": 800, "timeout_s": 30}
        proc = subprocess.run([str(browser_pdf._helper())], input=json.dumps(request), capture_output=True,
                              text=True, timeout=240, check=False)
        self.assertTrue(json.loads(proc.stdout.strip().splitlines()[-1])["ok"], proc.stdout + proc.stderr)
        self.assertTrue(out.read_bytes().startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main()
