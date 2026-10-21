"""#44: find_notes can look in a note's text, not only its title.

find_notes only ever asked Notes for `name contains`, so a note whose body held the phrase was never found.
Notes' own `contains` also misses Turkish case and accent variants ("istanbul" does not find "İstanbul"),
so body search reads the plain text once and matches here.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mcp_server.app_adapters import _RS, mac_app
from mcp_server.privacy_policy import PrivacyBlocked

SETTINGS = SimpleNamespace()
GS = chr(29)


def library(*notes: tuple[str, str, str]) -> str:
    ids, names, texts = zip(*notes) if notes else ((), (), ())
    return GS.join(_RS.join(part) for part in (ids, names, texts))


class NotesBodySearchTests(unittest.TestCase):
    def find(self, raw: str, query: str, search_in: str, **extra) -> tuple[dict, list[str]]:
        scripts: list[str] = []

        def run(script, **_kw):
            scripts.append(script)
            if "plaintext of every note" in script:
                return raw
            return _RS.join("Work" for _ in range(script.count('"x-')))

        with patch("mcp_server.app_adapters._run", side_effect=run), \
                patch("mcp_server.privacy_policy.check_app", return_value=None):
            result = mac_app(SETTINGS, app="Notes", action="find_notes", query=query, search_in=search_in, **extra)
        return result, scripts

    def test_a_phrase_only_in_the_body_is_found_with_a_snippet(self) -> None:
        raw = library(("x-1", "Weekly plan", "Call the supplier about the invoice on Friday."),
                      ("x-2", "Groceries", "Milk, bread"))
        result, scripts = self.find(raw, "the invoice", "body")
        self.assertTrue(result["ok"], result)
        self.assertEqual(["x-1"], [row["id"] for row in result["notes"]])
        self.assertEqual(["body"], result["notes"][0]["matched_in"])
        self.assertIn("invoice", result["notes"][0]["snippet"])
        self.assertEqual("Work", result["notes"][0]["folder"])
        self.assertIn("plaintext of every note", scripts[0])  # plain text, not the HTML body
        # Notes does not resolve `note id "x-coredata…"`; the folder is read through a whose clause.
        self.assertIn("every note whose id is (contents of noteID)", scripts[1])
        self.assertNotIn("note id noteID", scripts[1])

    def test_turkish_case_and_accents_match(self) -> None:
        raw = library(("x-1", "Ofis", "Toplantı İSTANBUL ofisinde; Işık ve zeytinyağı konuşulacak."))
        for query in ("istanbul", "Istanbul", "isik", "IŞIK", "zeytinyagi", "ZEYTİNYAĞI"):
            with self.subTest(query=query):
                result, _ = self.find(raw, query, "body")
                self.assertEqual(1, result["count"], result)

    def test_title_and_body_can_be_chosen_apart(self) -> None:
        raw = library(("x-1", "Budget 2026", "numbers"), ("x-2", "Notes", "the budget is final"))
        both, _ = self.find(raw, "budget", "both")
        self.assertEqual([("x-1", ["title"]), ("x-2", ["body"])], [(r["id"], r["matched_in"]) for r in both["notes"]])
        body, _ = self.find(raw, "budget", "body")
        self.assertEqual(["x-2"], [r["id"] for r in body["notes"]])

    def test_title_search_still_asks_notes_by_name(self) -> None:
        captured = {}
        with patch("mcp_server.app_adapters._run", side_effect=lambda script, **kw: captured.setdefault("s", script) and ""):
            result = mac_app(SETTINGS, app="Notes", action="find_notes", query="Plan")
        self.assertTrue(result["ok"])
        self.assertIn('name contains "Plan"', captured["s"])
        self.assertNotIn("plaintext", captured["s"])

    def test_same_titles_keep_their_own_ids(self) -> None:
        raw = library(("x-1", "Ideas", "rocket"), ("x-2", "Ideas", "rocket fuel"))
        result, _ = self.find(raw, "rocket", "body")
        self.assertEqual(["x-1", "x-2"], [row["id"] for row in result["notes"]])

    def test_snippets_are_short_and_mask_secrets(self) -> None:
        text = ("lorem " * 80) + "deploy key api_key=abcd1234efgh5678 and token ghp_" + "A" * 30 + " " + ("ipsum " * 80)
        result, _ = self.find(library(("x-1", "Ops", text)), "deploy key", "body")
        snippet = result["notes"][0]["snippet"]
        self.assertLessEqual(len(snippet), 220)
        self.assertNotIn("abcd1234efgh5678", snippet)
        self.assertNotIn("ghp_AAAA", snippet)
        self.assertTrue(snippet.startswith("…") and snippet.endswith("…"))

    def test_many_matches_are_cut_and_counted(self) -> None:
        raw = library(*[(f"x-{i}", f"Note {i}", "shared phrase") for i in range(12)])
        result, _ = self.find(raw, "shared", "body", limit=5)
        self.assertEqual((5, 12, True), (result["count"], result["total_matches"], result["truncated"]))

    def test_unknown_search_in_and_excluded_notes_say_why(self) -> None:
        bad = mac_app(SETTINGS, app="Notes", action="find_notes", query="x", search_in="everywhere")
        self.assertEqual("APP_ADAPTER_ARGUMENT_INVALID", bad["reason_code"])
        with patch("mcp_server.privacy_policy.check_app", side_effect=PrivacyBlocked("privacy_excluded_app", "Notes is excluded.")), \
                patch("mcp_server.app_adapters._run") as run:
            blocked = mac_app(SETTINGS, app="Notes", action="find_notes", query="x", search_in="body")
        self.assertFalse(blocked["ok"])
        self.assertEqual("privacy_excluded_app", blocked["reason_code"])
        run.assert_not_called()

    def test_an_empty_library_finds_nothing(self) -> None:
        result, _ = self.find(GS + GS, "anything", "both")
        self.assertEqual((True, 0, []), (result["ok"], result["count"], result["notes"]))


if __name__ == "__main__":
    unittest.main()
