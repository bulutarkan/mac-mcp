"""Team and agent metadata locks are reentrant in the thread that holds them.

Live bug: spawn_agents hung forever. _team_tick holds the team lock while it starts a
task, and the start reads the team again (_inherited_provenance_seed -> _read_team). The
thread lock is reentrant, but flock belongs to an open file: the second open + flock of
the same lock file in the same process waited for itself.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from mcp_server import tools_agents as agents

_TAINTED = {"provenance_class": "tainted_untrusted_web", "source": "test"}


class LockReentrancyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.old = (agents.AGENTS_DIR, agents.TEAMS_DIR)
        agents.AGENTS_DIR, agents.TEAMS_DIR = root / "agents", root / "teams"
        # One team per test: a deadlocked thread keeps its team's thread lock forever.
        for team_id in ("team_nested", "team_probe"):
            agents._write_team(team_id, {"team_id": team_id, "inherited_provenance": _TAINTED})
        agents._write_meta("agt_reentrant", {"agent_id": "agt_reentrant", "status": "running"})

    def tearDown(self) -> None:
        agents.AGENTS_DIR, agents.TEAMS_DIR = self.old
        self.temp.cleanup()

    def _finishes(self, work) -> object:
        out: dict = {}
        thread = threading.Thread(target=lambda: out.setdefault("value", work()), daemon=True)
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive(), "nested lock acquisition deadlocked")
        return out.get("value")

    def test_team_start_reads_the_team_it_is_holding(self) -> None:
        def nested():
            with agents._locked_team("team_nested"):
                return agents._inherited_provenance_seed(team_id="team_nested", parent_agent_id=None)

        self.assertEqual(_TAINTED, self._finishes(nested))

    def test_agent_meta_lock_is_reentrant_too(self) -> None:
        def nested():
            with agents._locked_meta("agt_reentrant"):
                return agents._update_meta("agt_reentrant", lambda meta: meta.update(status="done"))["status"]

        self.assertEqual("done", self._finishes(nested))

    def test_other_processes_are_still_locked_out(self) -> None:
        probe = (
            "import fcntl,sys\n"
            "f=open(sys.argv[1],'a')\n"
            "try:\n fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB);print('acquired')\n"
            "except BlockingIOError:\n print('blocked')\n"
        )
        lock_file = str(agents._team_dir("team_probe") / ".team.lock")
        with agents._locked_team("team_probe"):
            with agents._locked_team("team_probe"):
                inside = subprocess.run([sys.executable, "-c", probe, lock_file], capture_output=True, text=True, timeout=10)
            # Leaving the nested block must not release the outer hold.
            still = subprocess.run([sys.executable, "-c", probe, lock_file], capture_output=True, text=True, timeout=10)
        after = subprocess.run([sys.executable, "-c", probe, lock_file], capture_output=True, text=True, timeout=10)
        self.assertEqual(("blocked", "blocked", "acquired"),
                         (inside.stdout.strip(), still.stdout.strip(), after.stdout.strip()))


if __name__ == "__main__":
    unittest.main()
