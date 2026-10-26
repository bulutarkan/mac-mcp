"""Delegated agents and chat agents stay bounded (#119).

A provider that prints a 100 MB single line is cut off within the output budget and says so; a symlinked
worktree folder is refused before git runs; an ACP agent that floods requests or sends an oversized line is
answered quietly within fixed limits; a prompt that starts with "-" reaches every CLI as the prompt.
"""
from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)

import asyncio
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import mcp_server.tools_agents as agents
from mcp_server import acp_client, agent_message_bridge, chat_sessions
from mcp_server import extra_harness_providers as extra
from mcp_server import harness_providers as harness
from mcp_server.agent_worktrees import AgentWorktreeError, prepare_worktree

MIB = 1024 * 1024


class OutputBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mac-mcp-output-budget-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.agents_root = self.root / "agents"
        env = patch.dict(os.environ, {"MAC_MCP_STATE_DIR": str(self.root / "state"),
                                      "MAC_MCP_LESSON_DIR": str(self.root / "lessons")})
        env.start()
        self.addCleanup(env.stop)

    def _agent(self, agent_id: str, retries: int = 0) -> Path:
        adir = self.agents_root / agent_id
        adir.mkdir(parents=True)
        for name, value in {"prompt.txt": "p", "effective_prompt.txt": "p", "stdout.log": "", "stderr.log": "",
                            "worker.log": "", "result.txt": ""}.items():
            (adir / name).write_text(value, encoding="utf-8")
        meta = {
            "agent_id": agent_id, "provider": "opencode", "binary": "/tmp/opencode", "model": None, "reasoning": None,
            "cwd": str(self.root), "access_mode": "read_only", "permission_profile": "read_only",
            "scope": {"access_mode": "read_only", "path_roots": [str(self.root)]},
            "status": "starting", "phase": "starting", "started_at": 1.0, "last_activity_at": 1.0,
            "timeout_s": 120, "idle_timeout_s": None, "retries": retries, "retry_count": 0,
            "result_style": "concise", "role": None, "provenance_class": "local", "attempt": 1, "team_id": None,
            "resume_session_id": None, "session_id": None,
        }
        (adir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        return adir

    def test_a_100_mb_single_line_is_cut_off_within_budget_and_the_result_says_so(self) -> None:
        adir = self._agent("agt_flood", retries=2)
        flood = [sys.executable, "-c", textwrap.dedent("""\
            import sys
            chunk = b"x" * (1 << 20)
            for _ in range(100):
                sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.write(b"\\n")
        """)]
        popen = subprocess.Popen
        launches = []

        def counting_popen(*args, **kwargs):
            launches.append(args[0])
            return popen(*args, **kwargs)

        started = time.monotonic()
        with patch.object(agents, "AGENTS_DIR", self.agents_root), \
                patch.object(agents, "_build_provider_command", return_value=flood), \
                patch.object(agents, "_provider_env", return_value=({"PATH": os.environ.get("PATH", "")}, None)), \
                patch.object(agents, "_provider_process_command", side_effect=lambda _a, _m, cmd: (cmd, None)), \
                patch.object(agents.subprocess, "Popen", side_effect=counting_popen):
            rc = agents._worker("agt_flood")
            meta = agents._read_meta("agt_flood")
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 60)
        self.assertEqual(1, rc)
        self.assertEqual(1, len(launches))  # cut off once: no retry prints the same flood again
        self.assertEqual("failed", meta["status"])
        self.assertEqual("output_limit", meta["failure_reason"])
        self.assertIn("64 MiB limit", meta["note"])
        result = (adir / "result.txt").read_text(encoding="utf-8")
        self.assertTrue(result.startswith("Mac MCP stopped this agent: its output passed the 64 MiB limit"))
        stdout = (adir / "stdout.log").read_bytes()
        # Within the budget, and a single line keeps at most the line limit however long it was.
        self.assertLessEqual(len(stdout), agents.AGENT_OUTPUT_LINE_LIMIT_BYTES + 4096)
        self.assertIn(b"[mac-mcp: line cut, ", stdout)
        self.assertIn(b"limit for one run", stdout)

    def test_capture_keeps_lines_parses_events_and_cuts_long_lines(self) -> None:
        events = []
        lines = [json.dumps({"type": "step", "n": n}) for n in range(3)]
        data = ("\n".join(lines[:2]) + "\n" + "y" * 300 + "\n" + lines[2] + "\n").encode()
        log = self.root / "out.log"
        with patch.object(agents, "_record_provider_event", side_effect=lambda _id, line: events.append(line)), \
                patch.object(agents, "AGENT_OUTPUT_LINE_LIMIT_BYTES", 100), \
                patch.object(agents, "_OUTPUT_READ_CHUNK_BYTES", 7):
            agents._capture_provider_stream("agt_x", io.BytesIO(data), log, True, agents._OutputBudget(10_000))
        self.assertEqual([json.loads(e) for e in (events[0], events[1], events[3])],
                         [{"type": "step", "n": 0}, {"type": "step", "n": 1}, {"type": "step", "n": 2}])
        self.assertTrue(events[2].startswith("y" * 100 + "\n[mac-mcp: line cut, 201 more bytes not kept]"))
        self.assertEqual("".join(events), log.read_text(encoding="utf-8"))

    def test_budget_is_shared_and_stops_reading(self) -> None:
        budget = agents._OutputBudget(1000)
        log = self.root / "out.log"
        with patch.object(agents, "_OUTPUT_READ_CHUNK_BYTES", 100):
            agents._capture_provider_stream("agt_x", io.BytesIO(b"a\n" * 800), log, False, budget)
            agents._capture_provider_stream("agt_x", io.BytesIO(b"b\n" * 400), self.root / "err.log", False, budget)
        self.assertTrue(budget.exceeded.is_set())
        self.assertEqual(1000, budget.used)
        self.assertEqual(500, log.read_text().count("a\n"))
        self.assertNotIn("b\n", (self.root / "err.log").read_text())
        self.assertIn("limit for one run", (self.root / "err.log").read_text())


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


class WorktreeSymlinkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mac-mcp-wt-link-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.repo = root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo, check=True)
        _git(self.repo, "config", "user.email", "test@example.com")
        _git(self.repo, "config", "user.name", "Test")
        (self.repo / "a.txt").write_text("a\n", encoding="utf-8")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-q", "-m", "base")
        self.outside = root / "outside"
        self.outside.mkdir()

    def assert_refused_before_git(self, agent_id: str) -> None:
        with self.assertRaises(AgentWorktreeError) as caught:
            prepare_worktree(agent_id=agent_id, cwd=self.repo, path_roots=[str(self.repo)], mode="required")
        self.assertEqual("git_worktree_path_unsafe", caught.exception.code)
        self.assertEqual([], [p.name for p in self.outside.iterdir()])
        self.assertEqual(1, _git(self.repo, "worktree", "list", "--porcelain").count("worktree "))
        self.assertEqual("", _git(self.repo, "branch", "--list", f"mac-mcp/agent/{agent_id}").strip())

    def test_a_symlinked_worktree_folder_is_refused_before_git_runs(self) -> None:
        (self.repo / ".mac-mcp-worktrees").symlink_to(self.outside, target_is_directory=True)
        self.assert_refused_before_git("agt_link1")

    def test_a_symlinked_agent_folder_is_refused_too(self) -> None:
        (self.repo / ".mac-mcp-worktrees").mkdir()
        (self.repo / ".mac-mcp-worktrees" / "agt_link2").symlink_to(self.outside / "missing", target_is_directory=True)
        self.assert_refused_before_git("agt_link2")

    def test_a_real_folder_still_works(self) -> None:
        state = prepare_worktree(agent_id="agt_plain", cwd=self.repo, path_roots=[str(self.repo)], mode="required")
        self.assertEqual(str(self.repo / ".mac-mcp-worktrees" / "agt_plain"), state["path"])
        self.assertTrue((self.repo / ".mac-mcp-worktrees" / "agt_plain" / "a.txt").is_file())


FLOOD_AGENT = textwrap.dedent('''\
    import json, sys
    N = int(sys.argv[1])
    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
    for i in range(N):
        send({"jsonrpc": "2.0", "id": i, "method": "session/request_permission", "params": {"n": i}})
    sys.stdout.write("z" * int(sys.argv[2]) + "\\n")  # one line over the event limit
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"after": "oversized"}})
    sys.stdout.flush()
    refused = 0
    for line in sys.stdin:
        message = json.loads(line)
        if (message.get("error") or {}).get("message") == "Too many open requests.":
            refused += 1
        if refused >= N - int(sys.argv[3]):
            break
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"refused": refused}})
    sys.stdout.flush()
    sys.stdin.read()
''')


class AcpFloodTests(unittest.TestCase):
    def test_request_flood_and_oversized_line_stay_bounded_and_quiet(self) -> None:
        async def scenario(script: Path):
            notes = []
            handled = []
            release = asyncio.Event()
            refused_seen = asyncio.Event()

            async def on_notification(method, params):
                notes.append(params)
                if "refused" in params:
                    refused_seen.set()

            async def on_request(method, params):
                handled.append(params["n"])
                await release.wait()  # the owner never answers during the flood
                return {"outcome": {"outcome": "cancelled"}}

            conn = acp_client.AcpConnection(on_notification, on_request)
            with tempfile.TemporaryDirectory() as temp:
                await conn.start([sys.executable, str(script), "200", str(3 * MIB), str(acp_client._MAX_OPEN_REQUESTS)],
                                 cwd=temp, env=dict(os.environ), stderr_path=Path(temp) / "err.log")
                await asyncio.wait_for(refused_seen.wait(), 30)
                open_tasks = len(conn._tasks)
                running = conn.running
                release.set()
                await asyncio.gather(*list(conn._tasks))
                await conn.close(grace_s=2)
            return notes, handled, open_tasks, running

        with tempfile.TemporaryDirectory() as temp, patch.object(acp_client, "_LINE_LIMIT", MIB):
            script = Path(temp) / "flood.py"
            script.write_text(FLOOD_AGENT, encoding="utf-8")
            notes, handled, open_tasks, running = asyncio.run(scenario(script))
        limit = acp_client._MAX_OPEN_REQUESTS
        self.assertEqual(list(range(limit)), handled)  # only the first ones reach a handler
        self.assertEqual(limit, open_tasks)
        self.assertTrue(running)  # the oversized line did not end the connection
        self.assertIn({"after": "oversized"}, notes)
        self.assertIn({"refused": 200 - limit}, notes)

    def test_chat_declines_permission_questions_over_the_limit_without_asking(self) -> None:
        chat = SimpleNamespace(permissions={n: None for n in range(chat_sessions.MAX_PENDING_PERMISSIONS)})
        host = chat_sessions.ChatHost()
        answer = asyncio.run(host._on_request(chat, "session/request_permission", {"toolCall": {"title": "x"}}))
        self.assertEqual({"outcome": {"outcome": "cancelled"}}, answer)
        self.assertEqual(chat_sessions.MAX_PENDING_PERMISSIONS, len(chat.permissions))  # no new question


class PromptIsNeverAnOptionTests(unittest.TestCase):
    PROMPT = "--help me fix the failing test"

    def test_codex_and_opencode_end_their_options_before_the_prompt(self) -> None:
        result = Path("/tmp/result.txt")
        base = {"binary": "x", "model": "m", "reasoning": "high", "cwd": "/w", "access_mode": "workspace_write",
                "scope": {"access_mode": "workspace_write", "path_roots": ["/w"]}}
        codex = agents._build_provider_command({**base, "provider": "codex"}, self.PROMPT, result)
        self.assertEqual(["--", self.PROMPT], codex[-2:])
        resumed = agents._build_provider_command({**base, "provider": "codex", "resume_session_id": "sid"},
                                                 self.PROMPT, result)
        self.assertEqual(["--", "sid", self.PROMPT], resumed[-3:])
        opencode = agents._build_provider_command({**base, "provider": "opencode", "resume_session_id": "ses"},
                                                  self.PROMPT, result)
        self.assertEqual(["--", self.PROMPT], opencode[-2:])
        for cmd in (codex, resumed, opencode):
            self.assertEqual(1, cmd.count("--"))

    def test_positional_harness_prompts_follow_the_separator(self) -> None:
        for provider in ("pi", "kilo", "cline"):
            cmd = extra.command({"provider": provider, "binary": provider, "cwd": "/w", "model": "a/b",
                                 "private_home": "/h", "resume_session_id": "s"}, self.PROMPT)
            with self.subTest(provider=provider):
                self.assertEqual(["--", self.PROMPT], cmd[-2:])

    def test_prompt_options_bind_the_prompt_as_their_value(self) -> None:
        goose = extra.command({"provider": "goose", "binary": "goose", "cwd": "/w"}, self.PROMPT)
        copilot = extra.command({"provider": "copilot", "binary": "copilot", "cwd": "/w"}, self.PROMPT)
        grok = harness.command({"provider": "grok", "binary": "grok", "cwd": "/w"}, self.PROMPT)
        for cmd, option in ((goose, "--text="), (copilot, "--prompt="), (grok, "--single=")):
            with self.subTest(option=option):
                self.assertIn(option + self.PROMPT, cmd)
                self.assertNotIn(self.PROMPT, cmd)

    def test_message_hook_arguments_stay_before_the_separator(self) -> None:
        cmd = ["pi", "--print", "--", self.PROMPT]
        self.assertEqual(["pi", "--print", "-e", "/ext", "--", self.PROMPT],
                         agent_message_bridge.apply_args("pi", cmd, ["-e", "/ext"]))
        self.assertEqual(["opencode", "run", "--", self.PROMPT],
                         agent_message_bridge.apply_args("opencode", ["opencode", "run", "--pure", "--", self.PROMPT],
                                                         [], ["--pure"]))


if __name__ == "__main__":
    unittest.main()
