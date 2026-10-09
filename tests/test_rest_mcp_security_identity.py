from __future__ import annotations

import unittest

from mcp_server.policy import PolicyContext
from mcp_server.rest_routes import rest_security_identity
from mcp_server.security_context import SecurityContextManager


def context(agent_id=None, actor="owner", profile="trusted"):
    return PolicyContext(profile=profile, actor=actor, agent_id=agent_id)


class RestMcpSecurityIdentityTests(unittest.TestCase):
    def test_a_scoped_agent_has_one_security_state_across_transports(self) -> None:
        manager = SecurityContextManager()
        mcp_key = SecurityContextManager.identity_key(context("agt_child"), None)
        rest_key, rest_session = rest_security_identity(context("agt_child"))
        self.assertEqual(mcp_key, rest_key)
        self.assertEqual("agt_child", rest_session)
        # Taint through the MCP key; REST sees the same state.
        state = manager.touch(mcp_key, "agt_child")
        with manager._lock:
            manager._mark_untrusted_provenance_locked(
                state, origin="https://evil.example", tab_handle=None, tab_title=None, reason="test",
            )
        self.assertEqual("tainted_untrusted_web", manager.touch(rest_key, rest_session).provenance_class)

    def test_different_agents_stay_isolated(self) -> None:
        self.assertNotEqual(rest_security_identity(context("agt_a"))[0], rest_security_identity(context("agt_b"))[0])

    def test_owner_calls_key_by_actor_and_profile(self) -> None:
        key, session = rest_security_identity(context(None, actor="owner", profile="trusted"))
        self.assertEqual("actor:owner:trusted", key)
        self.assertEqual("actor:owner", session)
        self.assertNotEqual(key, rest_security_identity(context(None, actor="owner", profile="standard"))[0])


if __name__ == "__main__":
    unittest.main()
