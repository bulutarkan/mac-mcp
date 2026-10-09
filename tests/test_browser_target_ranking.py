from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from mcp_server import decision_engine
from mcp_server import tools_browser_agent as agent
from mcp_server.tools_browser_agent import _demote_redundant_candidates, _find_candidates_js


def _el(element_id: str, tag: str = "div", role: str = "", text: str = "", actionable: bool = False, **extra):
    return {"element_id": element_id, "tag": tag, "role": role, "text": text, "actionable": actionable, **extra}


def _ranked(scored):
    return [element["element_id"] for _, element in sorted(scored, key=lambda item: item[0], reverse=True)]


class RedundantCandidateTests(unittest.TestCase):
    def test_calendar_containers_yield_to_the_day_cell(self) -> None:
        # Google Flights shape: every container's aggregated text includes the day.
        day = _el("day", text="25 Kasım 2026 Çarşamba")
        grid = _el("grid", role="rowgroup", text="Kasım P S Ç 1 2 25", nested_match_ids=["day"])
        dialog = _el("dialog", role="dialog", text="Gidiş dönüş Ekim Kasım 25", nested_match_ids=["grid", "day"])
        out = _demote_redundant_candidates([(0.74, dialog), (0.74, grid), (0.74, day)])
        self.assertEqual("day", _ranked(out)[0])
        self.assertFalse(decision_engine.assess_ambiguity(sorted((s for s, _ in out), reverse=True))["ambiguous"])

    def test_actionable_cell_wins_over_its_text_node(self) -> None:
        cell = _el("cell", role="gridcell", text="25 ₺1.234", actionable=True, nested_match_ids=["num"])
        num = _el("num", text="25")
        out = _demote_redundant_candidates([(0.77, cell), (0.74, num)])
        self.assertEqual(["cell", "num"], _ranked(out))
        self.assertEqual(0.59, round(dict((e["element_id"], s) for s, e in out)["num"], 2))

    def test_button_keeps_rank_over_inner_span_with_same_text(self) -> None:
        button = _el("btn", tag="button", role="button", text="Devam et", actionable=True, nested_match_ids=["span"])
        span = _el("span", tag="span", text="Devam et")
        out = _demote_redundant_candidates([(1.0, button), (0.98, span)])
        scores = {e["element_id"]: s for s, e in out}
        self.assertEqual(1.0, scores["btn"])
        self.assertLess(scores["span"], 0.9)

    def test_label_yields_to_its_associated_control(self) -> None:
        label = _el("lbl", tag="label", text="E-posta adresi", actionable=True,
                    associated_control={"element_id": "inp"})
        control = _el("inp", tag="input", role="textbox", actionable=True)
        out = _demote_redundant_candidates([(1.0, control), (1.0, label)])
        self.assertEqual(["inp", "lbl"], _ranked(out))

    def test_wrapping_label_yields_to_its_checkbox(self) -> None:
        label = _el("lbl", tag="label", text="Bülten aboneliği", actionable=True,
                    associated_control={"element_id": "chk"}, nested_match_ids=["chk"])
        checkbox = _el("chk", tag="input", role="checkbox", actionable=True, associated_label={"element_id": "lbl"})
        out = _demote_redundant_candidates([(1.0, label), (1.0, checkbox)])
        self.assertEqual("chk", _ranked(out)[0])

    def test_distinct_targets_keep_their_scores(self) -> None:
        link = _el("a", tag="a", role="link", text="Antalya otelleri", actionable=True)
        paragraph = _el("p", tag="p", text="En uygun Antalya otelleri burada")
        scored = [(1.0, link), (0.86, paragraph)]
        self.assertEqual(scored, _demote_redundant_candidates(scored))

    def test_container_that_matches_clearly_better_is_kept(self) -> None:
        card = _el("card", text="Antalya Kemer otelleri", nested_match_ids=["word"])
        word = _el("word", tag="span", text="Kemer")
        out = _demote_redundant_candidates([(0.86, card), (0.48, word)])
        self.assertEqual(0.86, {e["element_id"]: s for s, e in out}["card"])

    def test_actionable_container_does_not_yield_to_non_actionable_descendant(self) -> None:
        link = _el("card", tag="a", role="link", text="Antalya otelleri 4 yıldız", actionable=True,
                   nested_match_ids=["title"])
        title = _el("title", tag="h3", text="Antalya otelleri")
        out = _demote_redundant_candidates([(0.89, link), (0.86, title)])
        self.assertEqual("card", _ranked(out)[0])


class FindScriptTests(unittest.TestCase):
    def test_find_script_reports_nested_matching_candidates(self) -> None:
        script = _find_candidates_js("25 Kasım", None, None, 60)
        self.assertIn("out.push(d);els.push(el);", script)
        self.assertIn("out[ci].nested_match_ids=nested", script)

    def test_browser_find_ranks_day_cell_first_without_tie(self) -> None:
        payload = {"ok": True, "elements": [
            _el("dialog", role="dialog", text="Gidiş dönüş Kasım 2026 25", nested_match_ids=["day"]),
            _el("day", text="25 Kasım 2026 Çarşamba"),
        ]}
        with patch.object(agent, "_resolve_tab_target", return_value=(1, 1)), \
                patch.object(agent, "_ensure_visual_companion"), \
                patch.object(agent, "_run_json_js", return_value=payload):
            found = agent.browser_find(MagicMock(), "Safari", query="25 Kasım", tab_handle="t")
        self.assertEqual("day", found["best_match"]["element_id"])
        scores = [m["confidence"] for m in found["matches"]]
        self.assertFalse(decision_engine.assess_ambiguity(scores)["ambiguous"])
        self.assertNotIn("nested_match_ids", found["best_match"])


if __name__ == "__main__":
    unittest.main()
