"""Agents remember on their own (memory that learns): memory_add and memory_update are in the default tool
list, the instructions tell every AI app what to save without being asked and what never to save, and the
server refuses secrets and keeps an identical memory once."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import tools_memory as tm
from mcp_server.main import MCP_AGENT_INSTRUCTIONS
from mcp_server.tool_summaries import CORE_TOOL_NAMES, CORE_TOOL_SUMMARIES


class MemoryLearningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {
            "MAC_MCP_MEMORY_DIR": str(Path(self.temp.name) / "memory"),
            "MAC_MCP_MEMORY_EMBEDDING": "feature_hash",
            "MAC_MCP_MEMORY_MODEL_CACHE": str(Path(self.temp.name) / "model-cache"),
            "MAC_MCP_SETTINGS_PATH": str(Path(self.temp.name) / "settings.json"),
        })
        env.start()
        self.addCleanup(env.stop)
        tm._last_retention_check.clear()

    def test_secrets_are_refused_on_add_and_update(self) -> None:
        for content in ("API key: sk-ant-api03-" + "x" * 40, "password=hunter2hunter2", "token ghp_" + "a" * 36,
                        "Use Bearer abcdefghijklmnopqrstuvwxyz123456 for the API"):
            with self.subTest(content=content[:20]), self.assertRaises(Exception) as raised:
                tm.memory_add(content)
            self.assertIn("MEMORY_CONTAINS_SECRET", str(raised.exception.detail))
        saved = tm.memory_add("The deploy key is in the Keychain item deploy-prod", tags=["deploy"])
        self.assertEqual("added", saved["action"])
        with self.assertRaises(Exception):
            tm.memory_update(saved["memory_id"], content="deploy key sk-ant-api03-" + "y" * 40)

    def test_an_identical_memory_is_kept_once(self) -> None:
        first = tm.memory_add("Tarkan wants every reply in Turkish", tags=["preference"])
        again = tm.memory_add("Tarkan wants every reply in Turkish", tags=["preference"])
        self.assertEqual(("added", "already_saved"), (first["action"], again["action"]))
        self.assertEqual(first["memory_id"], again["memory_id"])
        self.assertEqual(1, tm.memory_search(query="reply language")["total_matches"])

    def test_the_tools_are_listed_and_say_to_save_on_their_own(self) -> None:
        self.assertTrue({"memory_add", "memory_update", "memory_search"} <= CORE_TOOL_NAMES)
        self.assertIn("own initiative", CORE_TOOL_SUMMARIES["memory_add"])
        self.assertIn("Secrets are refused", CORE_TOOL_SUMMARIES["memory_update"])

    def test_the_instructions_say_what_to_learn_and_what_never(self) -> None:
        for phrase in ("Learn as you work, without being asked", "corrects you", "memory_update the memory it changes",
                       "Do not ask first or announce each save", "Never save secrets", "one-off task details",
                       "other people's private details", "lesson_record it"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, MCP_AGENT_INSTRUCTIONS)


if __name__ == "__main__":
    unittest.main()
