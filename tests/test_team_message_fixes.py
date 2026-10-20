"""Team message delivery fixes found by a live team review (2026-10-20).

Nothing is shown twice, nothing claimed is lost when it could not be written, a hook that fails half-way leaves
no live token behind, a silent Codex app-server cannot hold an agent's start, and delivery rows go with their
messages.
"""
from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import sqlite3
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from mcp_server import agent_message_bridge as bridge_mod
from mcp_server import team_messages as tm
from mcp_server import tools_team_messages
from tests.test_agent_message_bridge import Fixture
from tests.test_team_messages import A1, A2, ROOM, StoreFixture


class ShownOnceTests(StoreFixture):
    def test_a_message_opened_in_the_latest_view_is_not_shown_again_as_a_note(self) -> None:
        first = self.store.send(ROOM, A2, "first", to="a")
        self.store.send(ROOM, A2, "second", to="a")
        # The latest view with room for one: "second" is opened, "first" stays unread before it.
        self.assertEqual(["second"], [m["body"] for m in self.store.inbox(A1.member_id, limit=1)["messages"]])
        claimed = self.store.claim(A1.member_id, channel="hook")
        self.assertEqual([first["message_id"]], [m["message_id"] for m in claimed])
        self.assertEqual([], self.store.claim(A1.member_id, channel="hook"))

    def test_unclaimed_messages_come_back_once(self) -> None:
        self.store.send(ROOM, A2, "lost on the way", to="a")
        claimed = self.store.claim(A1.member_id, channel="hook")
        self.store.unclaim(A1.member_id, [m["seq"] for m in claimed], channel="hook")
        self.assertEqual(["lost on the way"], [m["body"] for m in self.store.claim(A1.member_id, channel="hook")])
        self.assertEqual([], self.store.claim(A1.member_id, channel="hook"))


class PruneTests(StoreFixture):
    def test_delivery_rows_go_with_their_messages(self) -> None:
        with patch.object(tm, "ROOM_HISTORY", 2):
            for index in range(4):
                self.store.send(ROOM, A2, f"note {index}", to="a")
                self.store.claim(A1.member_id, channel="hook")
            self.store.send(ROOM, A2, "last", to="a")
        with closing(sqlite3.connect(self.store.path)) as conn:
            orphans = conn.execute("SELECT COUNT(*) FROM deliveries WHERE seq NOT IN (SELECT seq FROM messages)").fetchone()[0]
        self.assertEqual(0, orphans)


class BridgeTests(Fixture):
    def test_a_goose_push_that_cannot_be_written_gives_the_messages_back(self) -> None:
        env: dict = {}
        bridge = bridge_mod.prepare("agt_goose", self.meta("goose"), env, hook_dir=self.dir / "hook",
                                    harness_home=self.dir / "goose-home")
        note_file = Path(env["GOOSE_MOIM_MESSAGE_FILE"])
        lead = tm.Caller(member_id="session:lead", label="lead")
        self.as_caller(lead)
        real_write = bridge_mod._private_file

        def failing(path, text):
            raise OSError("disk full")

        with patch.object(bridge_mod, "_private_file", side_effect=failing):
            sent = tools_team_messages.team_messages(action="send", team_id="team_t", body="do not lose me", to="api")
        self.assertTrue(sent["ok"])  # the sender is not failed by the receiver's file
        self.assertNotIn("do not lose me", note_file.read_text())
        with patch.object(bridge_mod, "_private_file", side_effect=real_write):
            bridge_mod.push_pending("agent:agt_goose")
        self.assertIn("do not lose me", note_file.read_text())
        bridge_mod.finish(bridge, terminal=True)

    def test_a_hook_that_fails_half_way_leaves_no_live_token(self) -> None:
        with patch.object(bridge_mod, "_private_dir", side_effect=OSError("read-only")):
            bridge = bridge_mod.prepare("agt_c", self.meta("claude"), {}, hook_dir=self.dir / "hook")
        self.assertEqual(("inbox_only", "hook_setup_failed:OSError"), (bridge.mode, bridge.reason))
        with closing(sqlite3.connect(self.store.path)) as conn:
            live = conn.execute("SELECT COUNT(*) FROM hook_tokens WHERE member_id = ? AND revoked_at IS NULL",
                                ("agent:agt_c",)).fetchone()[0]
        self.assertEqual(0, live)

    def test_a_silent_codex_app_server_cannot_hold_the_start(self) -> None:
        fake = self.dir / "codex"
        fake.write_text("#!/bin/sh\nexec sleep 30\n")
        fake.chmod(0o700)
        started = time.monotonic()
        with patch.object(bridge_mod, "CODEX_TRUST_TIMEOUT_S", 0.5):
            problem = bridge_mod.trust_codex_hooks(codex_home=self.dir, hooks_path=self.dir / "hooks.json",
                                                   cwd=str(self.dir), env={}, binary=str(fake))
        self.assertEqual("TimeoutError", problem)
        self.assertLess(time.monotonic() - started, 5)


if __name__ == "__main__":
    unittest.main()
