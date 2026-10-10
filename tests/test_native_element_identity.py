"""#42: a native action must reach the control that was observed, not whatever now sits at its path."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from mcp_server import tools_ui
from mcp_server.native_action_verification import (
    identity_basis,
    node_label,
    observed_identity_matches,
    observed_subtree_labels,
    readiness_reason,
)
from mcp_server.security import load_settings


def _node(element_id, role, *, title="", value="", description="", identifier="", subrole=""):
    parent = element_id.rsplit("/", 1)[0] if "/" in element_id else None
    return {
        "element_id": element_id, "parent_id": parent, "role": role, "subrole": subrole, "title": title,
        "description": description, "value": value, "identifier": identifier, "child_count": 0,
        "position": {"x": 10, "y": 10, "width": 200, "height": 20}, "enabled": True, "focused": False, "actions": [],
    }


def _file_list(first: str, second: str) -> dict:
    """A list whose two rows look alike (AXRow, no title) and differ only by the file name inside."""
    nodes = [
        _node("w1", "AXWindow", title="Files"),
        _node("w1/1", "AXTable"),
        _node("w1/1/1", "AXRow"), _node("w1/1/1/1", "AXCell"), _node("w1/1/1/1/1", "AXTextField", value=first),
        _node("w1/1/2", "AXRow"), _node("w1/1/2/1", "AXCell"), _node("w1/1/2/1/1", "AXTextField", value=second),
    ]
    return {node["element_id"]: node for node in nodes}


def _live(**overrides):
    base = {
        "connected": True, "role": "AXRow", "subrole": "", "title": "", "description": "", "value": "",
        "enabled": True, "position": {"x": 10, "y": 10, "width": 200, "height": 20},
        "window_position": {"x": 0, "y": 0, "width": 800, "height": 600}, "identifier": "", "subtree_labels": [],
    }
    base.update(overrides)
    return base


class IdentityRulesTests(unittest.TestCase):
    def test_subtree_labels_are_collected_two_levels_down_in_order(self) -> None:
        nodes = _file_list("Alpha.txt", "Beta.txt")
        self.assertEqual(["Alpha.txt"], observed_subtree_labels(nodes, "w1/1/1", max_depth=5, max_children=30))
        self.assertEqual(["Beta.txt"], observed_subtree_labels(nodes, "w1/1/2", max_depth=5, max_children=30))

    def test_labels_are_unknown_when_the_observation_stopped_above_them(self) -> None:
        nodes = _file_list("Alpha.txt", "Beta.txt")
        self.assertIsNone(observed_subtree_labels(nodes, "w1/1/1", max_depth=3, max_children=30))

    def test_secure_text_never_becomes_a_label(self) -> None:
        self.assertEqual("", node_label(_node("w1/1", "AXTextField", subrole="AXSecureTextField", value="hunter2")))
        self.assertEqual("", node_label(_node("w1/1", "AXTextField", value="[redacted]")))
        self.assertEqual("Name", node_label(_node("w1/1", "AXTextField", value="Name")))
        self.assertEqual("", node_label(_node("w1/1", "AXCheckBox", value="1")))

    def test_reordered_rows_cannot_redirect_the_action(self) -> None:
        observed = {**_file_list("Alpha.txt", "Beta.txt")["w1/1/1"], "subtree_labels": ["Alpha.txt"]}
        self.assertEqual("STALE_ELEMENT_PATH", readiness_reason(_live(subtree_labels=["Beta.txt"]), observed_node=observed))
        self.assertIsNone(readiness_reason(_live(subtree_labels=["Alpha.txt"]), observed_node=observed))

    def test_changed_identifier_at_the_same_geometry_is_stale(self) -> None:
        observed = _node("w1/2", "AXButton", title="Send", identifier="send-button")
        live = _live(role="AXButton", title="Send", identifier="schedule-button")
        self.assertFalse(observed_identity_matches(live, observed))
        self.assertTrue(observed_identity_matches(_live(role="AXButton", title="Send", identifier="send-button"), observed))

    def test_unlabeled_controls_are_told_apart_by_description(self) -> None:
        observed = _node("w1/2", "AXButton", description="Delete")
        self.assertFalse(observed_identity_matches(_live(role="AXButton", description="Archive"), observed))
        # A titled control keeps the existing rule: its title decides, not its (often dynamic) description.
        titled = _node("w1/2", "AXButton", title="Play", description="Play")
        self.assertTrue(observed_identity_matches(_live(role="AXButton", title="Play", description="Now playing"), titled))

    def test_missing_live_signals_do_not_reject_a_control(self) -> None:
        observed = {**_node("w1/1/1", "AXRow", identifier="row-a"), "subtree_labels": ["Alpha.txt"]}
        self.assertTrue(observed_identity_matches(_live(), observed))

    def test_identity_basis_says_what_told_the_target_apart(self) -> None:
        self.assertEqual("identifier", identity_basis(_node("w1/1", "AXButton", identifier="ok")))
        row = {**_node("w1/1/1", "AXRow"), "subtree_labels": ["Alpha.txt"]}
        twin_other = {**_node("w1/1/2", "AXRow"), "subtree_labels": ["Beta.txt"]}
        twin_same = {**_node("w1/1/2", "AXRow"), "subtree_labels": ["Alpha.txt"]}
        self.assertEqual("labels", identity_basis(row, [twin_other]))
        self.assertEqual("position_only", identity_basis(row, [twin_same]))
        self.assertEqual("role_title", identity_basis(_node("w1/1", "AXButton", title="OK"), []))


class IdentityNodeTests(unittest.TestCase):
    def test_labels_are_read_only_when_a_same_looking_sibling_exists(self) -> None:
        stored = {"nodes": _file_list("Alpha.txt", "Beta.txt"), "max_depth": 5, "max_children": 30}
        row = tools_ui._native_identity_node(stored, stored["nodes"]["w1/1/1"])
        self.assertEqual(["Alpha.txt"], row["subtree_labels"])
        self.assertEqual("labels", row["identity_basis"])
        table = tools_ui._native_identity_node(stored, stored["nodes"]["w1/1"])
        self.assertNotIn("subtree_labels", table)  # unique among its siblings: no extra probe cost
        self.assertEqual("role_title", table["identity_basis"])

    def test_probe_reads_labels_on_the_first_poll_only_and_only_as_many_as_observed(self) -> None:
        observed = {**_node("w1/1/1", "AXRow"), "subtree_labels": ["Alpha.txt"], "subtree_child_limit": 30}
        unstable = _live(subtree_labels=["Alpha.txt"], position={"x": 10, "y": 11, "width": 200, "height": 20})
        calls = []

        def probe(*_args, **kwargs):
            calls.append(kwargs["label_limit"])
            return (unstable if len(calls) == 1 else _live()), None

        with patch.object(tools_ui, "_probe_native_action_state", side_effect=probe), \
             patch.object(tools_ui.time, "sleep"):
            result = tools_ui._wait_for_native_readiness(
                "DemoApp", {"type": "click", "element_id": "w1/1/1", "readiness_stable_ms": 0}, observed, app_pid=1,
            )
        self.assertTrue(result["ready"], result)
        self.assertEqual(1, calls[0])
        self.assertTrue(all(limit == 0 for limit in calls[1:]))

    def test_label_walk_is_left_out_of_the_probe_when_not_needed(self) -> None:
        plain = tools_ui._native_action_state_script("DemoApp", "w1/1", app_pid=1)
        self.assertIn('value of attribute "AXIdentifier" of targetElement', plain)
        self.assertNotIn("axLabel", plain)
        walking = tools_ui._native_action_state_script("DemoApp", "w1/1", app_pid=1, label_limit=2)
        self.assertIn("on axLabel(r)", walking)
        self.assertIn("labelCount ≥ 2", walking)
        self.assertNotIn('perform action "AXPress"', walking)
        self.assertNotIn("click targetElement", walking)

    def test_parser_reads_identifier_and_labels(self) -> None:
        fields = ["__STATE__", "true"] + [""] * 34 + ["row-a", "Alpha.txt\x1emissing value\x1e  Beta  .txt \x1e"]
        state = tools_ui._parse_native_action_state("\x1f".join(fields))
        self.assertEqual("row-a", state["identifier"])
        self.assertEqual(["Alpha.txt", "Beta .txt"], state["subtree_labels"])


class ReorderedRowActTests(unittest.TestCase):
    def test_a_row_replaced_at_the_observed_path_is_never_clicked(self) -> None:
        nodes = list(_file_list("Alpha.txt", "Beta.txt").values())
        observation_id = tools_ui._save_observation(
            "DemoApp", 1, nodes, {"pid": 4321, "windows": []}, max_depth=5, max_children=30,
        )
        target = {"app": "DemoApp", "pid": 4321, "window_index": 1, "app_handle": "mapp", "window_handle": "mwin"}
        swapped = _live(subtree_labels=["Beta.txt"])
        with patch.object(tools_ui, "_resolve_action_native_target", return_value=(target, None)), \
             patch.object(tools_ui, "_probe_native_action_state", return_value=(swapped, None)), \
             patch.object(tools_ui, "_perform_action") as perform:
            result = tools_ui.act_ui(
                load_settings(), [{"type": "click", "element_id": "w1/1/1"}], app="DemoApp",
                observation_id=observation_id, return_state=False, allow_risky=True, preserve_focus=False,
            )
        perform.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertEqual("STALE_ELEMENT_PATH", result["reason_code"])
        failed = result["actions"][0]
        self.assertTrue(failed["observe_again"])
        self.assertEqual("labels", failed["readiness"]["identity_basis"])


if __name__ == "__main__":
    unittest.main()
