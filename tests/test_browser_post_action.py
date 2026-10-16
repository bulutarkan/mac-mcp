"""{type:'post'}: one browser_act action types a comment or reply, presses the box's own
submit button and confirms the text is on the page, instead of 8-10 separate calls."""
from __future__ import annotations

import itertools
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from mcp_server import browser_schemas, data_guard
from mcp_server import tools_browser_agent as agent
from mcp_server.tools_browser_agent import _browser_act_locked, _pick_post_field, _post_snippet
from mcp_server.workflow_checkpoints import exception_not_executed

COMMENT = "Mac MCP runs subagents on my Mac without hitting limits; the repo is linked below."


def box(element_id: str, label: str, **extra):
    return {"element_id": element_id, "role": "textbox", "tag": "div", "aria_label": label, "confidence": 0.8, **extra}


def found(matches, status="ok"):
    return {"ok": True, "best_match": matches[0] if matches else None, "matches": matches,
            **({"within": {"status": status, "levels": 6, "anchor_count": 1}} if status else {})}


class PostValidationTests(unittest.TestCase):
    def test_post_without_text_is_refused_before_anything_runs(self) -> None:
        for actions in ([{"type": "post"}], [{"type": "post", "text": "   "}]):
            with self.subTest(actions=actions), \
                 patch.object(agent, "_tab_lease") as lease, patch.object(agent, "_run_json_js") as run_js:
                with self.assertRaises(HTTPException) as ctx:
                    agent.browser_act(MagicMock(), "Safari", actions, tab_handle="tab")
                self.assertEqual(400, ctx.exception.status_code)
                self.assertTrue(exception_not_executed(ctx.exception))
                lease.assert_not_called()
                run_js.assert_not_called()
        with self.assertRaises(ValueError):
            browser_schemas.validate_action(0, {"type": "post"})
        self.assertEqual("post", browser_schemas.validate_action(0, {"type": "post", "text": "hi"})["type"])

    def test_schema_advertises_post(self) -> None:
        schema = browser_schemas.ACTION_ITEM_SCHEMA
        self.assertIn("post", schema["properties"]["type"]["enum"])
        self.assertIn("post", schema["properties"]["text"]["description"])


class PostTextSafetyTests(unittest.TestCase):
    def test_post_text_is_redacted_in_telemetry_and_scanned_for_secrets(self) -> None:
        args = {"browser": "Safari", "actions": [{"type": "post", "text": COMMENT}]}
        clean = data_guard.sanitize_tool_arguments("browser_act", args)
        self.assertEqual("[BROWSER INPUT REDACTED]", clean["actions"][0]["text"])
        secret = "Zq8vL2mX9pR4tY7wK3nB6cF1hJ5sD0gA"
        known = data_guard.secret_fingerprints(secret, include_entropy=True, include_whole=True)
        leaking = {"actions": [{"type": "post", "text": f"my key is {secret}"}]}
        self.assertTrue(data_guard.scan_sensitive_egress("browser_act", leaking, known).sensitive)
        self.assertFalse(data_guard.scan_sensitive_egress("browser_act", args, known).sensitive)


class PostChoiceTests(unittest.TestCase):
    def test_field_choice_skips_search_and_never_guesses(self) -> None:
        search = box("e_search", "Search Reddit")
        typed_search = box("e_q", "", input_type="search")
        comment = box("e_comment", "Join the conversation")
        other = box("e_note", "Notes")
        self.assertEqual(("e_comment", "ok"), (_pick_post_field([search, typed_search, comment], scoped=False)[0]["element_id"], "ok"))
        self.assertEqual("e_comment", _pick_post_field([search, comment, other], scoped=False)[0]["element_id"])
        self.assertEqual((None, "ambiguous"), _pick_post_field([other, box("e_x", "Bio")], scoped=False))
        self.assertEqual((None, "not_found"), _pick_post_field([search], scoped=False))
        # Inside one item (a reply) the nearest box wins.
        self.assertEqual("e_note", _pick_post_field([other, comment], scoped=True)[0]["element_id"])

    def test_snippet_matches_how_a_page_renders_the_text(self) -> None:
        self.assertEqual("mac mcp runs subagents on my mac without", _post_snippet(COMMENT))
        self.assertEqual("see the repo here", _post_snippet("See **the** [repo](https://github.com/x/y) https://x.y here"))
        self.assertEqual("guzel calısıyor", _post_snippet("Güzel çalışıyor!"))
        self.assertEqual("", _post_snippet("🙂 👍"))


class PostFlowTests(unittest.TestCase):
    def _post(self, action, *, finds, submit, counts, field_text_after=True):
        """Run one post action; the page answers the submit finder and the text counter."""
        calls = {"verified": [], "checks": 0}
        counts = iter(counts)

        def fake_verified(_settings, _browser, item, *_args, **_kwargs):
            calls["verified"].append((item["type"], item["element_id"], item.get("text")))
            return {"ok": True, "type": item["type"], "element_id": item["element_id"], "_js_calls": 1}

        def fake_js(_settings, _browser, js, *_args, **_kwargs):
            if "POST_FIELD_DETACHED" in js:
                return submit
            if "field_has_text" in js:
                calls["checks"] += 1
                count = next(counts)
                return {"ok": True, "count": count, "field_connected": True, "field_has_text": field_text_after}
            return {"ok": True, "url": "u", "title": "t", "dom_revision": 1}

        clock = itertools.count(0.0, 0.5)
        with patch.object(agent, "_resolve_tab_target", return_value=(1, 1)), \
             patch.object(agent, "browser_find", side_effect=finds) as find, \
             patch.object(agent, "_verified_dom_action", side_effect=fake_verified), \
             patch.object(agent, "_run_json_js", side_effect=fake_js), \
             patch.object(agent, "cancellable_sleep"), patch.object(agent.time, "sleep"), \
             patch.object(agent.time, "monotonic", side_effect=lambda: next(clock)):
            result = _browser_act_locked(MagicMock(), "Safari", [action], observation_id=None,
                                         window_index=1, tab_index=1, tab_handle="tab", return_state="none")
        return result, find, calls

    def test_one_action_types_presses_the_box_button_and_confirms(self) -> None:
        result, find, calls = self._post(
            {"type": "post", "text": COMMENT},
            finds=[found([box("e_search", "Search Reddit"), box("e_box", "Join the conversation")], status=None)],
            submit={"ok": True, "element_id": "e_send", "label": "comment", "level": 3},
            counts=[0, 0, 1],
        )
        self.assertTrue(result["ok"], result)
        post = result["actions"][0]
        self.assertEqual((True, True, "text_visible"), (post["posted"], post["submitted"], post["verification"]))
        self.assertEqual([("type", "e_box", COMMENT), ("click", "e_send", None)], calls["verified"])
        self.assertEqual("textbox", find.call_args.kwargs["role"])
        self.assertTrue(result["mutation_dispatched"])

    def test_unconfirmed_post_is_reported_once_and_never_retried(self) -> None:
        result, _find, calls = self._post(
            {"type": "post", "text": COMMENT, "timeout_s": 2},
            finds=[found([box("e_box", "Add a comment")], status=None)],
            submit={"ok": True, "element_id": "e_send", "label": "post"},
            # The text was already quoted on the page once; nothing new shows up.
            counts=[1] * 20,
        )
        self.assertFalse(result["ok"])
        post = result["actions"][0]
        self.assertEqual(("post_not_confirmed", None, True), (post["error"], post["posted"], post["submitted"]))
        self.assertTrue(result["outcome_unknown"])
        self.assertFalse(post["automatic_retry"])
        self.assertEqual(1, sum(1 for typ, *_ in calls["verified"] if typ == "click"))

    def test_missing_submit_button_stops_before_clicking(self) -> None:
        result, _find, calls = self._post(
            {"type": "post", "text": COMMENT},
            finds=[found([box("e_box", "Add a comment")], status=None)],
            submit={"ok": True, "element_id": None, "buttons": ["bold", "italic", "cancel"]},
            counts=[],
        )
        self.assertFalse(result["ok"])
        post = result["actions"][0]
        self.assertEqual(("post_submit_not_found", "submit", False), (post["error"], post["stage"], post["submitted"]))
        self.assertEqual(["bold", "italic", "cancel"], post["buttons"])
        self.assertEqual([("type", "e_box", COMMENT)], calls["verified"])

    def test_ambiguous_box_stops_before_typing(self) -> None:
        result, _find, calls = self._post(
            {"type": "post", "text": COMMENT},
            finds=[found([box("e_a", "Notes"), box("e_b", "Bio")], status=None)],
            submit={}, counts=[],
        )
        self.assertFalse(result["ok"])
        self.assertEqual("post_field_ambiguous", result["actions"][0]["error"])
        self.assertFalse(result["mutation_dispatched"])
        self.assertEqual([], calls["verified"])

    def test_reply_searches_inside_the_item_and_explicit_submit_is_scoped_to_the_box(self) -> None:
        opener = {"element_id": "e_open", "text": "Reply", "role": "button", "within_up": 3, "within_down": 1}
        send = {"element_id": "e_reply_send", "text": "Reply", "role": "button", "confidence": 1.0,
                "within_up": 2, "within_down": 2}
        result, find, calls = self._post(
            {"type": "post", "text": COMMENT, "within": "do you let users teach it", "submit": "Reply"},
            finds=[found([opener]), found([box("e_reply", "", within_up=3, within_down=2)]), found([send])],
            submit={}, counts=[0, 1],
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual("do you let users teach it", find.call_args_list[0].kwargs["within"])
        submit_call = find.call_args_list[2].kwargs
        self.assertEqual(("e_reply", "button", "Reply"), (submit_call["within_element_id"], submit_call["role"], submit_call["query"]))
        self.assertEqual([("type", "e_reply", COMMENT), ("click", "e_reply_send", None)], calls["verified"])
        self.assertNotIn("opened_reply", result["actions"][0])

    def test_closed_reply_box_is_opened_and_the_page_composer_is_never_used(self) -> None:
        # Live run: post within=<comment> without opening its box typed into the post's main
        # composer (the nearest box in scope) and published the reply as a top-level comment.
        opener = {"element_id": "e_open", "text": "Reply", "role": "button", "within_up": 1, "within_down": 1}
        page_box = box("e_main", "Join the conversation", within_up=3, within_down=4)
        own_box = box("e_own", "Write a reply", within_up=1, within_down=2)
        result, _find, calls = self._post(
            {"type": "post", "text": COMMENT, "within": "do you let users teach it"},
            finds=[found([opener]), found([page_box]), found([own_box, page_box])],
            submit={"ok": True, "element_id": "e_send", "label": "reply"}, counts=[0, 1],
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual("Reply", result["actions"][0]["opened_reply"])
        self.assertEqual([("click", "e_open", None), ("type", "e_own", COMMENT), ("click", "e_send", None)], calls["verified"])

    def test_box_that_belongs_to_something_else_is_refused_before_typing(self) -> None:
        opener = {"element_id": "e_open", "text": "Reply", "role": "button", "within_up": 1, "within_down": 1}
        page_box = box("e_main", "Join the conversation", within_up=3, within_down=4)
        result, _find, calls = self._post(
            {"type": "post", "text": COMMENT, "within": "do you let users teach it", "wait_s": 0},
            finds=[found([opener])] + [found([page_box])] * 30,
            submit={}, counts=[],
        )
        self.assertFalse(result["ok"])
        post = result["actions"][0]
        self.assertEqual(("post_field_outside_item", "field", False), (post["error"], post["stage"], post["submitted"]))
        self.assertEqual([("click", "e_open", None)], calls["verified"])


class PostGuidanceTests(unittest.TestCase):
    def test_agents_are_told_about_post_and_scan(self) -> None:
        from mcp_server.main import BROWSER_ACT_DESCRIPTION, BROWSER_OBSERVE_DESCRIPTION, MCP_AGENT_INSTRUCTIONS
        from mcp_server.tool_summaries import CORE_TOOL_SUMMARIES

        self.assertIn("{type:'post', text, within?}", MCP_AGENT_INSTRUCTIONS)
        self.assertIn("{type:'scan'}", MCP_AGENT_INSTRUCTIONS)
        self.assertIn("{type:'post', text, within?}", BROWSER_ACT_DESCRIPTION)
        self.assertIn("POST_NOT_CONFIRMED", BROWSER_ACT_DESCRIPTION)
        self.assertIn("It reads what is on screen", BROWSER_OBSERVE_DESCRIPTION)
        self.assertIn("{type:'post',text}", CORE_TOOL_SUMMARIES["browser_act"])
        # Reading a page is one browser_do read; scan stays the route for item-by-item lists.
        self.assertIn("browser_do read", CORE_TOOL_SUMMARIES["browser_observe"])
        self.assertIn("{type:'read'}", MCP_AGENT_INSTRUCTIONS)


if __name__ == "__main__":
    unittest.main()
