from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import provider_compat
from mcp_server import tools_agents as agents

CODEX_EXEC = "--json --color --skip-git-repo-check --output-last-message --config --model --sandbox --dangerously-bypass-approvals-and-sandbox"
CODEX_RESUME = "--json --skip-git-repo-check --output-last-message --config --model --dangerously-bypass-approvals-and-sandbox"


class ProviderCompatTests(unittest.TestCase):
    def setUp(self) -> None:
        provider_compat.clear_cache()
        self.addCleanup(provider_compat.clear_cache)
        self.root = Path(tempfile.mkdtemp(prefix="mac-mcp-compat-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.binary = self.root / "codex"
        self.binary.write_text("#!/bin/sh\n", encoding="utf-8")
        self.calls = 0

    def helper(self, exec_help: str, resume_help: str = CODEX_RESUME):
        def help_text(binary, args, env):
            self.calls += 1
            return resume_help if "resume" in args else exec_help
        return help_text

    def test_compatible_cli_is_cached_until_the_binary_changes(self) -> None:
        helper = self.helper(CODEX_EXEC)
        self.assertEqual("compatible", provider_compat.check("codex", str(self.binary), help_text=helper)["status"])
        provider_compat.check("codex", str(self.binary), help_text=helper)
        self.assertEqual(2, self.calls, "two help commands, then cached")
        time.sleep(0.01)
        self.binary.write_text("#!/bin/sh\n# upgraded\n", encoding="utf-8")
        provider_compat.check("codex", str(self.binary), help_text=helper)
        self.assertEqual(4, self.calls, "an upgraded binary is probed again")

    def test_a_missing_option_is_incompatible_with_a_remediation(self) -> None:
        result = provider_compat.check("codex", str(self.binary),
                                       help_text=self.helper(CODEX_EXEC.replace("--output-last-message", "")))
        self.assertEqual("incompatible", result["status"])
        self.assertEqual(["exec: --output-last-message"], result["missing"])
        self.assertIn("Update codex", result["remediation"])

    def test_unreadable_help_does_not_block(self) -> None:
        result = provider_compat.check("codex", str(self.binary), help_text=lambda b, a, e: None)
        self.assertEqual("unverified", result["status"])
        self.assertEqual("unverified", provider_compat.check("codex", str(self.root / "missing"))["status"])

    def test_spawn_stops_before_any_worktree_or_admission(self) -> None:
        incompatible = {"status": "incompatible", "missing": ["exec: --json"], "remediation": "Update codex."}
        with patch.object(agents, "_provider_compatibility", return_value=incompatible), \
             patch.object(agents, "_version", return_value="codex-cli 9.9"), \
             patch.object(agents, "prepare_worktree") as worktree, \
             patch.object(agents, "admission_acquire", create=True) as admission:
            with self.assertRaises(HTTPException) as ctx:
                agents._require_provider_compatible("codex", str(self.binary))
        self.assertEqual(409, ctx.exception.status_code)
        self.assertEqual("provider_incompatible", ctx.exception.detail["error"])
        self.assertEqual(["exec: --json"], ctx.exception.detail["missing_options"])
        worktree.assert_not_called()
        admission.assert_not_called()


class UnknownEventTests(unittest.TestCase):
    def test_unknown_events_are_recorded_known_ones_are_not(self) -> None:
        meta = {"provider": "codex", "status": "running"}
        for event_type in ("item.updated", "thread.renamed", "thread.renamed", "turn.started"):
            agents._apply_provider_event(meta, {"type": event_type}, time.time())
        self.assertEqual(["thread.renamed"], meta["unknown_event_types"])
        opencode = {"provider": "opencode", "status": "running"}
        agents._apply_provider_event(opencode, {"type": "step_start"}, time.time())
        agents._apply_provider_event(opencode, {"type": "patch_applied"}, time.time())
        self.assertEqual(["patch_applied"], opencode["unknown_event_types"])


if __name__ == "__main__":
    unittest.main()
