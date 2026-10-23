"""A delegated agent reaches the Mac MCP that started it, on that server's port."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from mcp_server import tools_agents as agents


class AgentEndpointTests(unittest.TestCase):
    def endpoint(self, **env: str) -> str:
        clean = {key: value for key, value in os.environ.items()
                 if key not in {"MAC_MCP_AGENT_ENDPOINT", "MAC_MCP_PORT"}}
        with patch.dict(os.environ, {**clean, **env}, clear=True):
            return agents._agent_mcp_endpoint()

    def test_default_port(self) -> None:
        self.assertEqual("http://127.0.0.1:8765/mcp", self.endpoint())

    def test_the_servers_own_port(self) -> None:
        self.assertEqual("http://127.0.0.1:8877/mcp", self.endpoint(MAC_MCP_PORT="8877"))

    def test_an_explicit_endpoint_wins(self) -> None:
        self.assertEqual("http://127.0.0.1:9000/mcp",
                         self.endpoint(MAC_MCP_PORT="8877", MAC_MCP_AGENT_ENDPOINT="http://127.0.0.1:9000/mcp"))

    def test_a_bad_port_falls_back(self) -> None:
        self.assertEqual("http://127.0.0.1:8765/mcp", self.endpoint(MAC_MCP_PORT="eighty"))


if __name__ == "__main__":
    unittest.main()
