"""The Live Island says once per session that iPhone steps borrow the pointer and keyboard.

iphone act brings iPhone Mirroring forward, hides the owner's pointer and sends keys on every step.
The first time a session does that, the island opens a short heads-up peek. see, read and find only
capture the screen, so they show nothing; tool_invoke calls of iphone act count as well.
Some clients (Grok) open a new MCP session for every tool call, so steps less than ten minutes apart
are one stretch of iPhone use and show the note once, whatever session they come from.
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "menu_app" / "Sources"

HARNESS = r'''
import Foundation

@main
struct IPhoneNoticeHarness {
    static func call(_ tool: String, _ action: String?, session: String? = "s1", agent: String? = nil,
                     invoked: String? = nil) -> LiveToolCall {
        LiveToolCall(eventID: "e", tool: tool, description: "d", sessionID: session, agentID: agent, client: nil,
                     target: nil, startedAt: Date(), action: action, invokedTool: invoked)
    }

    static func main() {
        let cases: [(String, LiveToolCall)] = [
            ("direct_act", call("iphone", "act")),
            ("claude_name", call("mcp__mac-mcp__iphone", "act")),
            ("invoke_act", call("tool_invoke", "act", invoked: "iphone")),
            ("see", call("iphone", "see")),
            ("read", call("iphone", "read")),
            ("other_tool", call("mac_act", "act")),
            ("invoke_other", call("tool_invoke", "act", invoked: "fleet")),
            ("agent_only", call("iphone", "act", session: nil, agent: "agt_1")),
        ]
        for (name, item) in cases {
            print(name, IPhoneNotice.sessionKey(for: item) ?? "-")
        }
        let start = Date()
        print("stretch_first", IPhoneNotice.startsNewStretch(at: start, lastStep: nil))
        print("stretch_next_step", IPhoneNotice.startsNewStretch(at: start.addingTimeInterval(40), lastStep: start))
        print("stretch_after_9m", IPhoneNotice.startsNewStretch(at: start.addingTimeInterval(540), lastStep: start))
        print("stretch_after_10m", IPhoneNotice.startsNewStretch(at: start.addingTimeInterval(600), lastStep: start))
        print("title", IPhoneNotice.title)
        print("detail", IPhoneNotice.detail)
    }
}
'''


class IPhoneNoticeTests(unittest.TestCase):
    def test_only_iphone_act_starts_the_notice_for_its_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source, binary = Path(temp) / "harness.swift", Path(temp) / "harness"
            source.write_text(HARNESS, encoding="utf-8")
            built = subprocess.run(
                ["xcrun", "swiftc", "-parse-as-library", str(SOURCES / "AppModels+Live.swift"), str(source), "-o", str(binary)],
                capture_output=True, text=True, timeout=180,
            )
            self.assertEqual(0, built.returncode, built.stderr)
            out = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30)
        self.assertEqual(0, out.returncode, out.stderr)
        result = dict(line.split(" ", 1) for line in out.stdout.splitlines())
        self.assertEqual({"direct_act": "s1", "claude_name": "s1", "invoke_act": "s1", "see": "-", "read": "-",
                          "other_tool": "-", "invoke_other": "-", "agent_only": "agt_1"},
                         {key: result[key] for key in result
                          if key not in {"title", "detail"} and not key.startswith("stretch_")})
        self.assertEqual({"stretch_first": "true", "stretch_next_step": "false", "stretch_after_9m": "false",
                          "stretch_after_10m": "true"},
                         {key: result[key] for key in result if key.startswith("stretch_")})
        self.assertEqual("iPhone Mirroring", result["title"])
        self.assertEqual("Each step briefly takes your pointer and keyboard.", result["detail"])

    def test_island_shows_it_once_per_session_and_stretch_as_a_quiet_peek(self) -> None:
        island = (SOURCES / "LiveIsland.swift").read_text(encoding="utf-8")
        started = island[island.index("private func toolStarted(_ call: LiveToolCall)"):island.index("private func toolFinished(")]
        self.assertIn("if let session = IPhoneNotice.sessionKey(for: call) {", started)
        self.assertIn("let newStretch = IPhoneNotice.startsNewStretch(at: now, lastStep: lastIPhoneStepAt)", started)
        self.assertIn("lastIPhoneStepAt = now", started)
        self.assertIn("if iPhoneNoticeShown.insert(session).inserted, newStretch {", started)
        self.assertIn('symbol: "iphone"', started)
        self.assertIn("tone: .notice", started)
        self.assertIn("next.tone == .attention || next.tone == .notice ? 5_000_000_000", island)
        state = (SOURCES / "AppState.swift").read_text(encoding="utf-8")
        self.assertIn('case toolName = "tool_name"', state)
        self.assertIn("action: arguments.action ?? arguments.arguments?.action,", state)


if __name__ == "__main__":
    unittest.main()
