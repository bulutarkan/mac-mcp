"""What a delegated browser agent is told must match what it may do.

Live run: a Codex agent with capability_profile=browser_only, asked to reply on a test
thread, gave up after two calls. Its first browser_do was refused for an 86-character
description and the error said retry=never_retry, although nothing ran and a shorter
description makes the call valid. Its prompt also said "do not modify external state",
while the browser_only profile allows browser side effects, so it would not post anyway.
"""
from __future__ import annotations

import unittest

from mcp.server.fastmcp.exceptions import ToolError

from mcp_server import error_contract
from mcp_server import tools_agents as agents
from mcp_server.policy import PROFILES, Capability


class DescriptionLengthContractTests(unittest.TestCase):
    def test_too_long_description_says_fix_arguments_not_never_retry(self) -> None:
        contract = error_contract.describe(ToolError("intent_description_too_long: tool=browser_do; max=80"), tool="browser_do")
        self.assertEqual(("not_executed", "fix_arguments"), (contract["outcome"], contract["retry"]))
        for code in ("intent_description_required", "intent_description_sensitive"):
            self.assertEqual("fix_arguments", error_contract.describe(ToolError(f"{code}: tool=x"), tool="x")["retry"])


class DelegatedAccessInstructionTests(unittest.TestCase):
    def test_browser_only_agents_may_act_in_the_browser(self) -> None:
        text = agents._access_instruction("read_only", "browser_only")
        self.assertIn("In the browser, act as the task asks", text)
        self.assertNotIn("external state", text)
        profile = PROFILES["browser_only"]
        self.assertIn(Capability.EXTERNAL_SIDE_EFFECT, profile.allowed_capabilities)

    def test_other_read_only_agents_keep_the_strict_rule(self) -> None:
        self.assertIn("Do not modify files, configuration, services, repositories, or external state",
                      agents._access_instruction("read_only", "read_only"))
        self.assertEqual("", agents._access_instruction("workspace_write", "developer"))


if __name__ == "__main__":
    unittest.main()
