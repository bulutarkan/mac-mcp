"""#248: the menu calls AI app conversations Conversations / Chat N and says in plain words what a
sent message does, so they are not confused with delegated agents."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

SOURCES = Path(__file__).resolve().parents[1] / "menu_app" / "Sources"


class ConversationWordingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.menu = (SOURCES / "MenuBarView.swift").read_text(encoding="utf-8")
        self.state = (SOURCES / "AppState.swift").read_text(encoding="utf-8")
        self.detail = (SOURCES / "SessionDetailView.swift").read_text(encoding="utf-8")

    def test_rows_and_section_say_conversation_not_agent(self) -> None:
        self.assertIn('Label("Conversations", systemImage: "rectangle.stack")', self.menu)
        self.assertIn('Text("Chat \\(session.flowNumber)")', self.menu)
        self.assertIn('Text("Chat \\(session.flowNumber)").font(.headline)', self.detail)
        for source in (self.menu, self.detail):
            self.assertNotIn('"Agent \\(session.flowNumber)', source)

    def test_steering_messages_are_plain_words(self) -> None:
        visible = " ".join(re.findall(r'"([^"\\]*(?:\\.[^"\\]*)*)"', self.state + self.menu))
        for jargon in ("preempted.", "agent session", "steering instruction", "Steering prompt", "steer."):
            self.assertNotIn(jargon, visible)
        self.assertIn("Sent. This conversation will see your message before its next step.", self.state)


if __name__ == "__main__":
    unittest.main()
