"""#172: browser actions say what was actually confirmed, and expect checks an action's intended result."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest

from fastapi import HTTPException

from mcp_server import browser_schemas
from mcp_server import tools_browser_agent as agent


def _extract(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    end = source.find("\nfunction ", start + 10)
    return source[start:] if end < 0 else source[start:end]


class TypographicMatchTests(unittest.TestCase):
    """Medium turned ' into ’ and -- into —; the text arrived, so it is not input_not_applied."""

    PAIRS = [
        ("It’s a “test” — really…", "It's a \"test\" -- really..."),
        ("Don’t stop", "Don't stop"),
        ("a – b", "a - b"),
    ]

    def test_python_check_folds_editor_punctuation(self) -> None:
        for shown, typed in self.PAIRS:
            with self.subTest(shown=shown):
                self.assertEqual(agent._fold_typed_text(typed), agent._fold_typed_text(shown))
        self.assertNotEqual(agent._fold_typed_text("Hello world"), agent._fold_typed_text("Hello"))

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_page_check_folds_editor_punctuation(self) -> None:
        matches = _extract(agent._bootstrap_functions_source(), "__mcpTextMatches")
        script = matches + "\nconsole.log(JSON.stringify(" + json.dumps(self.PAIRS) + ".map(([a,b])=>__mcpTextMatches(a,b))"
        script += ".concat([__mcpTextMatches('Hello','Hello world')])));"
        proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual([True, True, True, False], json.loads(proc.stdout))


class ExpectTests(unittest.TestCase):
    def test_an_action_with_expect_is_followed_by_its_check(self) -> None:
        actions = [{"type": "click", "query": "Send"}, {"type": "click", "query": "Share", "expect": {"text": "Copied"}}]
        expanded = agent._expand_expectations(actions)
        self.assertEqual(3, len(expanded))
        self.assertNotIn("expect", expanded[1])
        check = expanded[2]
        self.assertEqual(("wait", "text", "Copied", 1, True), (check["type"], check["for"], check["text"],
                                                               check["_expect_for"], check["required"]))
        self.assertEqual(5.0, check["timeout_s"])

    def test_expect_kinds_map_to_waits(self) -> None:
        cases = {"gone": ("text_gone", "text"), "selector": ("selector", "selector")}
        for kind, (wait_for, field) in cases.items():
            with self.subTest(kind=kind):
                check = agent._expand_expectations([{"type": "click", "query": "x", "expect": {kind: "Upload"}}])[1]
                self.assertEqual(wait_for, check["for"])
                self.assertEqual("Upload", check[field])
        check = agent._expand_expectations([{"type": "click", "query": "x", "expect": {"url_change": True, "timeout_s": 99}}])[1]
        self.assertEqual(("url_change", 30.0), (check["for"], check["timeout_s"]))

    def test_bad_expectations_are_refused_before_anything_runs(self) -> None:
        for expect in ("Saved", {}, {"text": "a", "gone": "b"}, {"text": " "}, {"url_change": "yes"}, {"colour": "red"}):
            with self.subTest(expect=expect), self.assertRaises(HTTPException):
                agent._expand_expectations([{"type": "click", "query": "x", "expect": expect}])
        with self.assertRaises(HTTPException):
            agent._expand_expectations([{"type": "wait", "for": "text", "text": "a", "expect": {"text": "b"}}])
        with self.assertRaisesRegex(ValueError, "expect must be an object"):
            browser_schemas.validate_action(0, {"type": "click", "query": "x", "expect": "Saved"})

    def test_met_expectation_confirms_the_action(self) -> None:
        results = [{"type": "click", "ok": True, "verification": "state_changed", "effect_observed": True},
                   {"type": "wait", "ok": True, "matched": True, "duration_ms": 40, "_expect_for": 0,
                    "_expect": {"text": "Pending review"}}]
        folded = agent._fold_expectations(results)
        self.assertEqual(1, len(folded))
        self.assertEqual({"text": "Pending review", "met": True, "waited_ms": 40}, folded[0]["expect"])
        self.assertEqual("confirmed", agent._action_outcome(folded[0]))

    def test_unmet_expectation_fails_without_a_retry(self) -> None:
        results = [{"type": "click", "ok": True, "verification": "async_network_activity", "effect_observed": True},
                   {"type": "wait", "ok": True, "matched": False, "timed_out": True, "_expect_for": 0,
                    "_expect": {"gone": "Choose an image"}}]
        action = agent._fold_expectations(results)[0]
        self.assertEqual((False, "expectation_not_met", True, False),
                         (action["ok"], action["error"], action["outcome_unknown"], action["automatic_retry"]))
        self.assertEqual("unconfirmed", agent._action_outcome(action))

    def test_text_gone_condition_checks_the_page_text(self) -> None:
        js = agent._condition_js({"for": "text_gone", "text": "Choose an image"}, "https://example.com/")
        self.assertIn("!(document.body&&String(document.body.innerText||'').toLowerCase().indexOf(\"choose an image\")>=0)", js)


class OutcomeTests(unittest.TestCase):
    def test_outcomes_name_what_was_checked(self) -> None:
        cases = [
            ({"type": "type", "ok": True, "verification": "value_applied"}, "confirmed"),
            ({"type": "type", "ok": True, "verification": "editor_model_unconfirmed"}, "unconfirmed"),
            ({"type": "type", "ok": False, "error": "input_not_applied"}, "failed"),
            # A click that changed the page has not shown its intended result (Medium: Share, image, review).
            ({"type": "click", "ok": True, "verification": "state_changed", "effect_observed": True}, "page_reacted"),
            ({"type": "click", "ok": True, "verification": "async_network_activity", "effect_observed": True}, "page_reacted"),
            ({"type": "click", "ok": True, "verification": "deferred_pending"}, "unconfirmed"),
            ({"type": "click", "ok": False, "error": "action_no_effect"}, "failed"),
            ({"type": "click", "ok": False, "error": "tab_target_closed", "outcome_unknown": True}, "unconfirmed"),
            ({"type": "post", "ok": True, "posted": True}, "confirmed"),
            ({"type": "post", "ok": False, "error": "post_not_confirmed", "posted": None}, "unconfirmed"),
            ({"type": "fill", "ok": True}, "confirmed"),
            ({"type": "click", "ok": False, "error": "foreground_not_authorized"}, "blocked"),
            ({"type": "click", "ok": False, "human_priority": True}, "blocked"),
        ]
        for result, outcome in cases:
            with self.subTest(result=result):
                self.assertEqual(outcome, agent._action_outcome(result))

    def test_the_response_names_unconfirmed_actions(self) -> None:
        response = {"actions": [{"outcome": "confirmed"}, {"outcome": "page_reacted"}, {"outcome": "unconfirmed"}]}
        agent._flag_unconfirmed(response)
        self.assertEqual([1, 2], response["unconfirmed_actions"])
        self.assertIn("expect", response["confirm_hint"])
        quiet = {"actions": [{"outcome": "confirmed"}]}
        agent._flag_unconfirmed(quiet)
        self.assertNotIn("unconfirmed_actions", quiet)


if __name__ == "__main__":
    unittest.main()
