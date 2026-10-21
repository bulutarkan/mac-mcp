"""#81: text seen on a page is not proof the site saved it when the page keeps its own copy.

A site that keeps a draft in localStorage or sessionStorage shows it again after a reload. An expect={text} check
says what it showed (the page, and the browser's own storage when the text is there too) and never that the site
saved it.
"""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import unittest

from mcp_server import tools_browser_agent as agent


def _storage(items: dict) -> str:
    return ("{_d:%s,get length(){return Object.keys(this._d).length;},key:function(i){return Object.keys(this._d)[i];},"
            "getItem:function(k){return this._d[k];}}" % json.dumps(items))


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class LocalCopyScanTests(unittest.TestCase):
    def scan(self, text: str, local: dict, session: dict | None = None, throwing: bool = False) -> dict:
        session_js = "{get length(){throw new Error('denied');}}" if throwing else _storage(session or {})
        script = (f"globalThis.window={{localStorage:{_storage(local)},sessionStorage:{session_js}}};"
                  f"console.log({agent._local_copy_js(text)});")
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20, check=True).stdout
        return json.loads(base64.b64decode(out.strip()).decode("utf-8"))

    def test_a_draft_kept_as_json_is_found_and_its_value_never_returned(self) -> None:
        draft = {"r/mcp:reply": json.dumps({"body": "Thanks, \"that\" helps\na lot", "ts": 1})}
        result = self.scan("thanks, \"that\" helps a lot", draft)
        self.assertEqual(["localStorage"], result["local_copy"])
        self.assertNotIn("helps", json.dumps(result))

    def test_plain_text_in_session_storage_and_nothing_elsewhere(self) -> None:
        self.assertEqual(["sessionStorage"], self.scan("Pending review", {}, {"k": "x Pending   review y"})["local_copy"])
        self.assertEqual([], self.scan("Pending review", {"k": "something else"})["local_copy"])

    def test_storage_that_refuses_access_is_skipped(self) -> None:
        self.assertEqual(["localStorage"], self.scan("hello", {"k": "hello"}, throwing=True)["local_copy"])


class ExpectEvidenceTests(unittest.TestCase):
    def fold(self, local_copy) -> dict:
        results = [{"ok": True, "type": "type", "verification": "value_applied"},
                   {"type": "wait", "matched": True, "duration_ms": 40, "_expect_for": 0,
                    "_expect": {"text": "Thanks"}, "_local_copy": local_copy}]
        return agent._fold_expectations(results)[0]["expect"]

    def test_a_local_copy_makes_the_check_say_so(self) -> None:
        check = self.fold(["localStorage"])
        self.assertEqual(("page_and_browser_storage", "unverified", ["localStorage"]),
                         (check["evidence"], check["saved_on_site"], check["local_copy"]))
        self.assertIn("does not show the site saved it", check["hint"])

    def test_without_a_copy_the_check_is_as_before(self) -> None:
        for local_copy in ([], None):  # none found, or the page could not be asked
            self.assertEqual({"text": "Thanks", "met": True, "waited_ms": 40}, self.fold(local_copy))

    def test_other_expectations_are_unchanged(self) -> None:
        results = [{"ok": True, "type": "click"},
                   {"type": "wait", "matched": True, "_expect_for": 0, "_expect": {"url_change": True}}]
        self.assertNotIn("evidence", agent._fold_expectations(results)[0]["expect"])


if __name__ == "__main__":
    unittest.main()
