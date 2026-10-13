from __future__ import annotations

import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import tools_agents as agents

SYNTHETIC_OLD = b'{"tokens": "synthetic-old-fixture"}'
SYNTHETIC_NEW = b'{"tokens": "synthetic-new-fixture"}'


class CodexAuthRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="mac-mcp-codex-auth-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.codex_home = self.root / "owner-codex"
        self.codex_home.mkdir()
        self.source = self.codex_home / "auth.json"
        self.source.write_bytes(SYNTHETIC_OLD)
        for patcher in (
            patch.object(agents, "AGENTS_DIR", self.root / "agents"),
            patch.dict(os.environ, {"CODEX_HOME": str(self.codex_home)}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        (self.root / "agents" / "agt_owner").mkdir(parents=True)
        self.meta = {"provider": "codex"}

    def snapshot(self) -> Path:
        return agents._codex_secret_auth_path("agt_owner", self.meta)

    def test_a_new_sign_in_reaches_the_next_attempt(self) -> None:
        agents._restricted_codex_state("agt_owner", self.meta)
        self.assertEqual(SYNTHETIC_OLD, self.snapshot().read_bytes())
        self.source.write_bytes(SYNTHETIC_NEW)  # the owner ran `codex login`
        state = agents._restricted_codex_state("agt_owner", self.meta)
        self.assertEqual(SYNTHETIC_NEW, self.snapshot().read_bytes())
        self.assertEqual(0o600, stat.S_IMODE(self.snapshot().stat().st_mode))
        link = state / "codex-home" / "auth.json"
        self.assertEqual(self.snapshot(), Path(os.readlink(link)))
        self.assertEqual([], [p.name for p in self.snapshot().parent.glob(".auth.*")], "no temp files left")

    def test_the_owners_codex_folder_is_never_a_trusted_project(self) -> None:
        home = self.root / "home"
        home.mkdir()
        (home / ".codex").symlink_to(self.codex_home)  # an agent working in the home folder
        config = self.root / "agents" / "agt_owner" / "provider_state" / "codex" / "codex-home" / "config.toml"
        config.parent.mkdir(parents=True)
        folder = str(home.resolve())
        config.write_text(f'model = "x"\n\n[projects."{folder}"]\ntrust_level = "trusted"\n\n[projects."/other"]\n'
                          'trust_level = "trusted"\n', encoding="utf-8")
        agents._restricted_codex_state("agt_owner", {**self.meta, "cwd": str(home)})
        text = config.read_text(encoding="utf-8")
        self.assertIn(f'[projects."{folder}"]\ntrust_level = "untrusted"', text)
        self.assertEqual(1, text.count(f'[projects."{folder}"]'))
        self.assertIn('[projects."/other"]\ntrust_level = "trusted"', text)
        self.assertIn('model = "x"', text)
        self.assertEqual(0o600, stat.S_IMODE(config.stat().st_mode))
        other = self.root / "project"
        other.mkdir()
        before = config.read_text(encoding="utf-8")
        agents._restricted_codex_state("agt_owner", {**self.meta, "cwd": str(other)})
        self.assertEqual(before, config.read_text(encoding="utf-8"), "other folders are left to Codex")

    def test_scoped_server_has_its_own_name_and_turns_off_the_owners_entry(self) -> None:
        meta = {"scoped_mcp": True, "access_mode": "workspace_write"}
        args = agents._codex_scoped_mcp_args(meta)
        self.assertIn('mcp_servers.mac-mcp-agent.url="http://127.0.0.1:8765/mcp"', args)
        self.assertNotIn("mcp_servers.mac-mcp.enabled=false", args)  # the private Codex home has no owner entry
        full = {**meta, "access_mode": "full"}
        self.assertNotIn("mcp_servers.mac-mcp.enabled=false", agents._codex_scoped_mcp_args(full))
        (self.codex_home / "config.toml").write_text('[mcp_servers.mac-mcp]\ncommand = "mac-mcp"\nargs = ["stdio"]\n')
        self.assertIn("mcp_servers.mac-mcp.enabled=false", agents._codex_scoped_mcp_args(full))
        self.assertEqual([], agents._codex_scoped_mcp_args({"scoped_mcp": False}))

    def test_unchanged_or_missing_sign_in_is_not_rewritten(self) -> None:
        agents._restricted_codex_state("agt_owner", self.meta)
        self.assertFalse(agents._refresh_codex_auth_snapshot(self.source, self.snapshot()))
        self.source.unlink()
        self.assertFalse(agents._refresh_codex_auth_snapshot(self.source, self.snapshot()))
        self.assertEqual(SYNTHETIC_OLD, self.snapshot().read_bytes(), "the last good snapshot is kept")

    def test_expired_sign_in_is_typed_and_not_retried(self) -> None:
        stderr = self.root / "stderr.log"
        stderr.write_text("Error: refresh token expired; please log in again\n", encoding="utf-8")
        decision = agents._adaptive_retry_decision({}, 1, None, self.root / "missing-stdout", stderr, 0, 0)
        self.assertEqual({"retryable": False, "reason": "provider_auth_expired", "backoff_s": 0.0}, decision)


if __name__ == "__main__":
    unittest.main()
