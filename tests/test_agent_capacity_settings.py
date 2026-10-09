from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import agent_admission


class CapacitySettingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="mac-mcp-capacity-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.settings = self.root / "settings.json"
        env = patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(self.settings), "MAC_MCP_AGENT_MEMORY_GATE": "0"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("MAC_MCP_AGENT_GLOBAL_ACTIVE_LIMIT", None)

    def write(self, max_active) -> None:
        self.settings.write_text(json.dumps({"subagents": {"max_active": max_active}}), encoding="utf-8")

    def test_default_is_eight(self) -> None:
        self.assertEqual(8, agent_admission.configured_global_limit())
        self.assertEqual(8, agent_admission.global_limit())

    def test_settings_value_applies_live_and_is_clamped(self) -> None:
        self.write(3)
        self.assertEqual(3, agent_admission.global_limit())
        self.write(12)
        self.assertEqual(12, agent_admission.global_limit())
        self.assertEqual(12, agent_admission.provider_limit("codex"), "one provider can use the whole cap")
        self.write(500)
        self.assertEqual(agent_admission.MAX_LIMIT, agent_admission.global_limit())
        self.write("eight")
        self.assertEqual(8, agent_admission.global_limit())

    def test_environment_still_wins(self) -> None:
        self.write(3)
        with patch.dict(os.environ, {"MAC_MCP_AGENT_GLOBAL_ACTIVE_LIMIT": "5"}):
            self.assertEqual(5, agent_admission.global_limit())

    def test_lower_limit_than_running_agents_only_queues_new_work(self) -> None:
        self.write(1)
        state = {"leases": {"a": {"provider": "codex", "weight": 1}, "b": {"provider": "codex", "weight": 1}}}
        with patch.object(agent_admission, "_active_counts", return_value=(2, {"codex": 2})):
            self.assertEqual("global_capacity", agent_admission._capacity_reason(state, "codex"))


class MemoryPressureTests(unittest.TestCase):
    def setUp(self) -> None:
        agent_admission._memory_state.update(sampled_at=0.0, level="normal", warning_until=0.0, critical_until=0.0)
        self.addCleanup(agent_admission._memory_state.update, sampled_at=0.0, level="normal",
                        warning_until=0.0, critical_until=0.0)
        env = patch.dict(os.environ, {"MAC_MCP_AGENT_MEMORY_GATE": "1"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("MAC_MCP_AGENT_GLOBAL_ACTIVE_LIMIT", None)

    def sample(self, level: int, now: float) -> str:
        with patch.object(agent_admission, "_sample_memory_pressure", return_value=level), \
             patch.object(agent_admission, "_now", return_value=now):
            return agent_admission.memory_pressure()

    def test_critical_blocks_new_admissions_and_warning_halves_capacity(self) -> None:
        self.assertEqual("critical", self.sample(4, 1000.0))
        with patch.object(agent_admission, "_now", return_value=1001.0), \
             patch.object(agent_admission, "_active_counts", return_value=(0, {})):
            self.assertEqual("memory_pressure", agent_admission._capacity_reason({}, "codex"))
        agent_admission._memory_state.update(sampled_at=0.0, level="normal", warning_until=0.0, critical_until=0.0)
        self.assertEqual("warning", self.sample(2, 2000.0))
        with patch.object(agent_admission, "_now", return_value=2001.0), \
             patch.object(agent_admission, "configured_global_limit", return_value=8):
            self.assertEqual(4, agent_admission.global_limit())

    def test_pressure_is_held_briefly_after_it_clears(self) -> None:
        self.sample(4, 1000.0)
        self.assertEqual("critical", self.sample(1, 1010.0), "a brief dip keeps the gate closed")
        self.assertEqual("normal", self.sample(1, 1000.0 + agent_admission.MEMORY_PRESSURE_HOLD_S + 6))

    def test_kill_switch(self) -> None:
        with patch.dict(os.environ, {"MAC_MCP_AGENT_MEMORY_GATE": "0"}):
            self.assertEqual("normal", self.sample(4, 1000.0))


if __name__ == "__main__":
    unittest.main()
