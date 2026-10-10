"""#126: observations carry selection, placeholder, help and visibility cues when the app has them."""
from __future__ import annotations

import unittest
from pathlib import Path

from mcp_server import tools_ui

FS, RS = "\x1f", "\x1e"
SWIFT = (Path(tools_ui.__file__).with_name("ax_observe.swift")).read_text(encoding="utf-8")


def _node(element_id, role, *, x=10, y=10, w=80, h=20, title="", value="", subrole="", cues=None):
    fields = ["__NODE__", element_id, element_id.rsplit("/", 1)[0] if "/" in element_id else "", role, subrole,
              title, "", value, str(x), str(y), str(w), str(h), "true", "false", "", "0", ""]
    if cues is not None:
        fields += list(cues)
    return FS.join(fields)


def _parse(*nodes):
    meta = FS.join(["__META__", "DemoApp", "false", "1", "Main", "4321", "dev.demo"])
    window = FS.join(["__WINDOW__", "1", "Main", "", "", "0", "0", "800", "600", "AXStandardWindow", "false", "false"])
    return tools_ui._parse_observation(RS.join([meta, window, *nodes]))[1]


class CueParsingTests(unittest.TestCase):
    def test_cues_are_read_when_reported(self) -> None:
        row, field, icon = _parse(
            _node("w1/1", "AXRow", cues=["true", "", ""]),
            _node("w1/2", "AXTextField", cues=["", "Search messages", ""]),
            _node("w1/3", "AXButton", cues=["false", "", "Attach a file"]),
        )
        self.assertTrue(row["selected"])
        self.assertEqual("Search messages", field["placeholder"])
        self.assertEqual("Attach a file", icon["help"])
        self.assertFalse(icon["selected"])

    def test_unsupported_cues_stay_absent_not_false(self) -> None:
        node, = _parse(_node("w1/1", "AXTextField", cues=["", "", ""]))
        for key in ("selected", "placeholder", "help"):
            self.assertNotIn(key, node)
        legacy, = _parse(_node("w1/1", "AXTextField"))  # an older observer sends no cue fields at all
        self.assertNotIn("selected", legacy)

    def test_selection_noise_from_containers_is_dropped(self) -> None:
        group, chosen_group = _parse(_node("w1/1", "AXGroup", cues=["false", "", ""]),
                                     _node("w1/2", "AXGroup", cues=["true", "", ""]))
        self.assertNotIn("selected", group)
        self.assertTrue(chosen_group["selected"])

    def test_cues_are_bounded_and_secure_values_stay_redacted(self) -> None:
        secure, = _parse(_node("w1/1", "AXTextField", subrole="AXSecureTextField", value="[redacted]",
                               cues=["", "Password " + "x" * 400, ""]))
        self.assertEqual("[redacted]", secure["value"])
        self.assertEqual(200, len(secure["placeholder"]))

    def test_nodes_outside_their_window_are_flagged(self) -> None:
        inside, below = _parse(_node("w1/1", "AXRow", y=100), _node("w1/2", "AXRow", y=900))
        self.assertNotIn("visible_in_window", inside)
        self.assertFalse(below["visible_in_window"])


class ObserverTests(unittest.TestCase):
    def test_swift_reads_the_cues_in_its_one_batched_call(self) -> None:
        self.assertIn('"AXSelected", "AXPlaceholderValue", "AXHelp"', SWIFT)
        self.assertIn('a["AXSelected"] == nil ? "" : bool(a["AXSelected"])', SWIFT)

    def test_applescript_reads_cues_only_for_roles_that_use_them(self) -> None:
        script = tools_ui._observation_script("DemoApp", 1, 5, 30)
        self.assertIn('if roleText is in {"AXTextField", "AXTextArea", "AXSearchField", "AXComboBox"} then', script)
        self.assertIn('value of attribute "AXPlaceholderValue" of nodeRef', script)
        self.assertIn('(titleText is "" or titleText is "missing value") and roleText is in', script)
        self.assertIn("my cleanText(helpText, fs, rs)", script)


if __name__ == "__main__":
    unittest.main()
