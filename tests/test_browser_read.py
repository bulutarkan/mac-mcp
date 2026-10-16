"""{type:'read'}: a whole page's readable text in one call (lazy content loaded first)."""
from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import asyncio
import json
import os
import unittest
from unittest.mock import patch

from pydantic import TypeAdapter

from mcp_server import browser_schemas as schemas
from mcp_server import tools_browser_agent as agent
import mcp_server.main as main
from tests.test_core_catalog_descriptions import _build_mcp


def fake_page(heights, text="Title\nFirst paragraph.\nSecond paragraph.", view=800):
    """A page whose height grows to each value in `heights` as it is scrolled down."""
    state = {"top": 0, "step": 0, "reads": [], "restored": None}

    def run(_settings, _browser, js, *_args, **_kwargs):
        if "mcp-read-scroll" in js:
            options = json.loads(js.split("var opts=", 1)[1].split(";", 1)[0])
            height = heights[min(state["step"], len(heights) - 1)]
            if options["phase"] == "restore":
                state["restored"] = options["top"]
                state["top"] = options["top"]
                return {"ok": True, "top": state["top"], "height": height}
            if options["phase"] == "step":
                state["step"] += 1
                height = heights[min(state["step"], len(heights) - 1)]
                state["top"] = min(height - view, state["top"] + int(view * options["factor"]))
            return {"ok": True, "top": state["top"], "height": height, "view": view,
                    "at_end": state["top"] + view >= height - 2}
        if "mcp-read-text" in js:
            options = json.loads(js.split("var opts=", 1)[1].split(";", 1)[0])
            state["reads"].append(options)
            start, cap = options["offset"], options["max_chars"]
            piece = text[start:start + cap]
            end = start + len(piece)
            return {"ok": True, "type": "read", "url": "https://example.test/", "title": "Example", "text": piece,
                    "offset": start, "total_chars": len(text), "returned_chars": len(piece),
                    "next_offset": end if end < len(text) else None, "complete": end >= len(text), "headings": ["Title"]}
        raise AssertionError("unexpected script")

    return run, state


class ReadActionTests(unittest.TestCase):
    def read(self, action, heights=(2000,), **page):
        run, state = fake_page(list(heights), **page)
        with patch.object(agent, "_run_json_js", side_effect=run), patch.object(agent, "cancellable_sleep"):
            result = agent._read_action(None, "Safari", action, 1, 1, "tab-1")
        return result, state

    def test_read_is_an_action_and_needs_no_target(self):
        self.assertIn("read", agent._ACT_TYPES)
        actions = [{"type": "read"}]
        self.assertEqual(actions, TypeAdapter(schemas.BrowserActions).validate_python(actions))

    def test_read_loads_lazy_content_then_reads_the_page_once(self):
        result, state = self.read({"type": "read"}, heights=(2000, 4000, 6000, 6000))
        self.assertTrue(result["ok"], result)
        self.assertEqual("Title\nFirst paragraph.\nSecond paragraph.", result["text"])
        self.assertTrue(result["complete"])
        self.assertEqual(1, len(state["reads"]))
        self.assertEqual(6000, result["loaded"]["height"])
        self.assertEqual("end_of_page", result["loaded"]["stopped"])
        self.assertEqual(0, state["restored"], "the reader is put back where it was")

    def test_a_long_page_continues_from_next_offset_without_loading_again(self):
        text = "word " * 600
        first, _ = self.read({"type": "read", "max_chars": 1000}, text=text)
        self.assertFalse(first["complete"])
        self.assertEqual(1000, first["next_offset"])
        second, state = self.read({"type": "read", "max_chars": 1000, "offset": first["next_offset"]}, text=text)
        self.assertNotIn("loaded", second, "a continuation reads the page as it already is")
        self.assertEqual(text[1000:2000], second["text"])

    def test_load_more_false_reads_at_once(self):
        result, state = self.read({"type": "read", "load_more": False}, heights=(2000, 9000))
        self.assertNotIn("loaded", result)
        self.assertEqual(0, state["step"])

    def test_loading_stops_at_its_time_budget(self):
        heights = [2000 + 1000 * index for index in range(500)]  # a page that never stops growing
        clock = iter(range(0, 10_000))
        with patch.object(agent.time, "perf_counter", side_effect=lambda: next(clock) * 0.5):
            result, _ = self.read({"type": "read", "timeout_s": 3}, heights=heights)
        self.assertTrue(result["ok"])
        self.assertEqual("time_budget", result["loaded"]["stopped"])

    def step_factors(self, busy_total):
        run, _ = fake_page([60000])
        factors = []

        def spy(settings, browser, js, *args, **kwargs):
            reply = run(settings, browser, js, *args, **kwargs)
            if "mcp-read-scroll" in js:
                options = json.loads(js.split("var opts=", 1)[1].split(";", 1)[0])
                if options["phase"] == "step":
                    factors.append(options["factor"])
                reply = {**reply, "busy": 0, "busy_total": busy_total}
            return reply

        with patch.object(agent, "_run_json_js", side_effect=spy), patch.object(agent, "cancellable_sleep"):
            agent._read_action(None, "Safari", {"type": "read", "timeout_s": 30}, 1, 1, "tab-1")
        return factors

    def test_no_screen_is_skipped_while_something_on_the_page_is_still_loading(self):
        # A section that loads when it comes into view (a review list behind a skeleton) is never jumped over.
        factors = self.step_factors(busy_total=1)
        self.assertTrue(factors)
        self.assertTrue(all(factor <= 1.0 for factor in factors), factors)

    def test_a_quiet_page_is_crossed_in_bigger_steps(self):
        factors = self.step_factors(busy_total=0)
        self.assertLessEqual(factors[0], 1.0, "it starts one screen at a time")
        self.assertGreater(max(factors), 2.0, "it speeds up once nothing loads")
        self.assertLessEqual(max(factors), 4.0)

    def scripted(self, steps, action=None):
        """Scroll replies from a script: each item is (height, at_end, busy); 'check' replies repeat the last one
        until its busy count runs out."""
        calls = {"steps": 0, "checks": 0, "busy": 0}

        def run(_settings, _browser, js, *_args, **_kwargs):
            if "mcp-read-text" in js:
                return {"ok": True, "type": "read", "text": "page", "offset": 0, "total_chars": 4, "returned_chars": 4,
                        "next_offset": None, "complete": True, "headings": []}
            options = json.loads(js.split("var opts=", 1)[1].split(";", 1)[0])
            height, at_end, busy = steps[min(calls["steps"], len(steps) - 1)]
            if options["phase"] == "step":
                calls["steps"] += 1
                height, at_end, busy = steps[min(calls["steps"], len(steps) - 1)]
                calls["busy"] = busy
            elif options["phase"] == "check":
                calls["checks"] += 1
                calls["busy"] = calls["busy"] if calls["busy"] >= 99 else max(0, calls["busy"] - 1)  # 99: never loads
            return {"ok": True, "top": 0, "height": height, "view": 800, "at_end": at_end, "busy": calls["busy"]}

        with patch.object(agent, "_run_json_js", side_effect=run), patch.object(agent, "cancellable_sleep"):
            result = agent._read_action(None, "Safari", action or {"type": "read"}, 1, 1, "tab-1")
        return result, calls

    def test_a_loading_placeholder_on_screen_is_waited_for(self):
        # Step 2 shows a skeleton that goes away after three checks; then the page grows and ends.
        result, calls = self.scripted([(3000, False, 0), (3000, False, 0), (3000, False, 3), (9000, False, 0),
                                       (9000, True, 0), (9000, True, 0), (9000, True, 0)])
        self.assertTrue(result["ok"])
        self.assertEqual(3, calls["checks"])
        self.assertEqual("end_of_page", result["loaded"]["stopped"])
        self.assertEqual(0, result["loaded"]["still_loading"])

    def test_a_placeholder_that_never_finishes_is_reported(self):
        result, calls = self.scripted([(3000, False, 0), (3000, True, 99), (3000, True, 99)],
                                      action={"type": "read", "timeout_s": 2})
        self.assertTrue(result["ok"])
        self.assertGreater(result["loaded"]["still_loading"], 0, "the reader is told some content was still loading")

    def test_max_chars_is_bounded(self):
        _, state = self.read({"type": "read", "max_chars": 10_000_000, "load_more": False})
        self.assertEqual(agent._READ_MAX_CHARS, state["reads"][0]["max_chars"])


class BrowserDoReadTests(unittest.TestCase):
    def setUp(self):
        profile = patch.dict(os.environ, {"MAC_MCP_PERMISSION_PROFILE": "trusted"})
        profile.start()
        self.addCleanup(profile.stop)
        self.mcp = _build_mcp()

    def test_browser_do_returns_the_whole_read_under_read(self):
        text = "Paragraph. " * 3000  # far over browser_do's 8 KB answer budget
        read = {"ok": True, "type": "read", "url": "https://example.test/", "title": "Example", "text": text,
                "offset": 0, "total_chars": len(text), "returned_chars": len(text), "next_offset": None,
                "complete": True, "headings": []}
        with patch.object(main, "browser_act", return_value={"ok": True, "actions": [read], "action_count": 1}):
            result = asyncio.run(self.mcp.call_tool("browser_do", {
                "browser": "Safari", "tab_handle": "test", "actions": [{"type": "read"}], "description": "Read the page"}))
        blocks = result[0] if isinstance(result, tuple) else result
        payload = json.loads(blocks[0].text)
        self.assertEqual(text, payload["read"]["text"])
        self.assertTrue(payload["read"]["complete"])
        self.assertEqual("Example", payload["title"])


if __name__ == "__main__":
    unittest.main()


class ReadScrollScanGuidanceTests(unittest.TestCase):
    """An agent can tell from the descriptions alone when to read, scroll or scan."""

    def test_browser_act_says_how_to_read_and_how_to_scroll(self):
        text = main.BROWSER_ACT_DESCRIPTION
        self.assertIn("{type:'read'}", text)
        self.assertIn("put back where it was", text)
        self.assertIn("{type:'scroll', dy:", text)
        self.assertIn("stays where it was scrolled", text)

    def test_instructions_name_all_three_routes(self):
        text = main.MCP_AGENT_INSTRUCTIONS
        for phrase in ("{type:'read'}", "{type:'scroll'", "{type:'scan'}"):
            self.assertIn(phrase, text)

    def test_the_action_schema_explains_read_and_scroll_options(self):
        props = schemas.ACTION_ITEM_SCHEMA["properties"]
        for name in ("dy", "offset", "load_more", "max_chars"):
            with self.subTest(option=name):
                self.assertIn("description", props[name])
