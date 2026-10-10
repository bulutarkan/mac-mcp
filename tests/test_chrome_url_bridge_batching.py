"""#148: the degraded Chrome URL bridge reads chunks in batches and refuses oversized results early."""
from __future__ import annotations

import base64
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import tools_browser
from mcp_server.foreground_guard import foreground_authorization
from mcp_server.tools_browser import _chrome_execute_js_via_url_bridge

MARKER = "__MAC_MCP_BRIDGE_abcdef123456__"


class Target:
    browser = "Google Chrome"
    window_index = 1
    tab_index = 2
    native_id = "123"
    url = "https://example.com/"
    title = "Example"


class FixedUUID:
    hex = "abcdef1234567890"


def _bridge(script_reply):
    """Run the bridge with a fake osascript; script_reply(script) returns its stdout."""
    scripts = []

    def run(script, timeout_s=0):
        scripts.append(script)
        return script_reply(script)

    with foreground_authorization("test_chrome_url_js_bridge"), \
         patch.object(tools_browser.uuid, "uuid4", return_value=FixedUUID()), \
         patch.object(tools_browser.time, "sleep"), \
         patch.object(tools_browser, "_run_osascript", side_effect=run):
        try:
            return _chrome_execute_js_via_url_bridge("(()=>'x')()", Target(), 6), scripts
        except HTTPException as exc:
            return exc, scripts


def _chunk_reply(encoded: str, *, corrupt: bool = False):
    size = tools_browser._CHROME_BRIDGE_CHUNK_CHARS

    def reply(script):
        if "__macMcpBridgeOriginalTitle=document.title" in script:
            return f"{MARKER}READY:{len(encoded)}"
        if "set chunkStarts to {" in script:
            starts = [int(v) for v in script.split("set chunkStarts to {", 1)[1].split("}", 1)[0].split(", ")]
            parts = [encoded[start:start + size] for start in starts]
            if corrupt:
                parts[-1] = parts[-1][:-1]
            return f"{MARKER}CHUNKS:" + "\n".join(parts)
        return ""  # restore

    return reply


class UrlBridgeBatchingTests(unittest.TestCase):
    def test_large_result_is_read_in_batches_not_one_script_per_chunk(self) -> None:
        text = "".join(f"{i}ü," for i in range(20_000))
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        chunks = -(-len(encoded) // tools_browser._CHROME_BRIDGE_CHUNK_CHARS)
        out, scripts = _bridge(_chunk_reply(encoded))
        self.assertEqual(text, out)
        batches = -(-chunks // tools_browser._CHROME_BRIDGE_CHUNKS_PER_SCRIPT)
        self.assertGreater(chunks, 25)
        self.assertEqual(1 + batches + 1, len(scripts))  # stage, chunk batches, restore

    def test_each_chunk_waits_for_a_title_carrying_its_own_offset(self) -> None:
        encoded = base64.b64encode(b"a" * 9000).decode("ascii")
        _, scripts = _bridge(_chunk_reply(encoded))
        batch = scripts[1]
        self.assertIn(f'set chunkPrefix to "{MARKER}CHUNK:" & chunkStart & ":"', batch)
        self.assertIn('slice(" & chunkStart & "," & chunkEnd & ")', batch)
        self.assertIn("if bridgeTitle starts with chunkPrefix then exit repeat", batch)

    def test_oversized_results_are_refused_before_any_chunk_is_read(self) -> None:
        limit = tools_browser._CHROME_BRIDGE_MAX_ENCODED_CHARS
        exc, scripts = _bridge(lambda script: f"{MARKER}READY:{limit + 4}" if "OriginalTitle=document" in script else "")
        self.assertIsInstance(exc, HTTPException)
        self.assertEqual(413, exc.status_code)
        self.assertEqual("chrome_bridge_result_too_large", exc.detail["error"])
        self.assertIn("companion", exc.detail["message"])
        self.assertEqual(2, len(scripts))  # stage and restore only
        self.assertNotIn("chunkStarts", scripts[-1])

    def test_a_short_chunk_fails_instead_of_returning_corrupt_data(self) -> None:
        encoded = base64.b64encode(b"b" * 12000).decode("ascii")
        exc, _ = _bridge(_chunk_reply(encoded, corrupt=True))
        self.assertIsInstance(exc, HTTPException)
        self.assertIn("incomplete", str(exc.detail))

    def test_apple_events_javascript_off_is_reported_as_the_setting_to_enable(self) -> None:
        def reply(script):
            if "OriginalTitle=document" in script:
                raise HTTPException(500, "execution error: Google Chrome got an error: Executing JavaScript "
                                         "through AppleScript is turned off. (12)")
            return ""

        exc, scripts = _bridge(reply)
        self.assertEqual(412, exc.status_code)
        self.assertIn("Allow JavaScript from Apple Events", str(exc.detail))
        self.assertEqual(2, len(scripts))  # no retries against a setting that will not change


if __name__ == "__main__":
    unittest.main()
