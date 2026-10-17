"""The CLI's themed screens."""
from __future__ import annotations

import argparse
import io
import re
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from mcp_server import cli, cli_views
from mcp_server.cli_style import Style, strip_ansi
from mcp_server.managed_process import ProcessValidation


class FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


TTY_ENV = {"TERM": "xterm-256color", "COLORTERM": "truecolor", "COLUMNS": "80"}


def visible(text: str) -> str:
    return "\n".join(line.rsplit("\r", 1)[-1] for line in strip_ansi(text).split("\n"))


class ThemedTestCase(unittest.TestCase):
    def setUp(self) -> None:
        env = patch.dict(os.environ, TTY_ENV)
        env.start()
        self.addCleanup(env.stop)
        for name in ("NO_COLOR", "MAC_MCP_PLAIN"):
            os.environ.pop(name, None)

    def run_main(self, argv):
        out = FakeTTY()
        code = None
        with redirect_stdout(out):
            try:
                code = cli.main(argv)
            except SystemExit as stop:
                code = stop.code
        return code, visible(out.getvalue())


class HomeAndHelp(ThemedTestCase):
    def test_groups_list_every_command_but_stdio_once(self) -> None:
        listed = [name for _, names in cli_views.GROUPS for name in names] + cli_views.ADVANCED
        parser_commands = set(cli.command_names())
        self.assertEqual(sorted(parser_commands - {"stdio"}), sorted(listed))
        self.assertEqual(len(listed), len(set(listed)))
        self.assertTrue(set(cli_views.SHORT_HELP) >= set(listed))

    def test_bare_command_shows_home_with_status(self) -> None:
        with patch.object(cli, "_home_running", return_value=True):
            code, text = self.run_main([])
        self.assertEqual(0, code)
        lines = text.split("\n")
        self.assertTrue(lines[0].startswith("  ◆ Mac MCP · ") and lines[0].endswith("● Running"), lines[0])
        self.assertIn("  Everyday", text)
        self.assertIn("    status          How Mac MCP is doing", text)
        self.assertIn("  Advanced  ", text)
        self.assertIn("mac-mcp <command> --help for more.", text)
        self.assertNotIn("stdio", text)

    def test_help_has_no_status_line_and_no_server_call(self) -> None:
        with patch.object(cli, "_home_running", side_effect=AssertionError("no server call")):
            code, text = self.run_main(["--help"])
        self.assertEqual(0, code)
        self.assertNotIn("Running", text.split("\n")[0])
        self.assertIn("  Connect", text)

    def test_command_help_is_themed(self) -> None:
        code, text = self.run_main(["status", "--help"])
        self.assertEqual(0, code)
        self.assertTrue(text.startswith("  ◆ Mac MCP · status\n"), text[:80])
        self.assertIn("--local-only", text)
        self.assertIn("--json", text)
        self.assertIn("Example", text)
        self.assertIn("mac-mcp status --local-only", text)

    def test_home_lines_command_names_up_in_one_column(self) -> None:
        with patch.object(cli, "_home_running", return_value=False):
            _, text = self.run_main([])
        # Command rows are indented four spaces; the Advanced line's continuation is deeper.
        lines = [line for line in text.split("\n") if line.startswith("    ") and not line.startswith("     ")]
        columns = {len(line) - len(line.lstrip(" ").split(" ", 1)[1].lstrip(" ")) for line in lines}
        self.assertEqual(1, len(columns), lines)  # connect-config does not push its text out
        self.assertIn("● Stopped", text.split("\n")[0])

    def test_the_advanced_line_wraps_under_itself(self) -> None:
        out = FakeTTY()
        cli_views.render_home(Style.for_stream(out, env={**TTY_ENV, "COLUMNS": "60"}), "2.2.0", None)
        lines = visible(out.getvalue()).split("\n")
        start = next(i for i, line in enumerate(lines) if line.startswith("  Advanced  "))
        block = lines[start:next(i for i in range(start, len(lines)) if not lines[i].strip())]
        self.assertGreater(len(block), 1)
        self.assertTrue(all(len(line) <= 60 for line in block), block)
        self.assertTrue(all(line.startswith(" " * 12) for line in block[1:]), block)
        self.assertIn("uninstall", " ".join(block))

    def test_command_help_says_what_the_command_does(self) -> None:
        _, text = self.run_main(["status", "--help"])
        self.assertIn("  Show server and public endpoint status.", text)
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            cli.main(["status", "--help"])  # not a terminal: argparse's own help, no description added
        self.assertNotIn("Show server and public endpoint status", out.getvalue())

    def test_nested_command_help_is_themed_too(self) -> None:
        code, text = self.run_main(["recipe", "--help"])
        self.assertEqual(0, code)
        self.assertTrue(text.startswith("  ◆ Mac MCP · recipe\n"), text[:80])


class StatusScreen(ThemedTestCase):
    def report(self, **changes):
        report = {
            "ok": True, "state": "healthy", "exit_code": 0,
            "server": {"running": True, "pid": 4321, "identity": "pid_record", "port": 8765, "health": "ok",
                       "log": "/Users/x/.mac-mcp/mac-mcp.log", "port_owners": []},
            "public_endpoint": {"mode": "cloudflare", "url": "https://mac.example.com/mcp", "tunnel_running": True,
                                "tunnel_pid": 77, "route": "ok", "error": None},
            "stray_processes": [], "privacy": {"paused": False, "private_mode": False},
            "supervisor": {"enabled": True, "loaded": True, "intent": "running"}, "remediation": [],
        }
        for key, value in changes.items():
            report[key] = {**report[key], **value} if isinstance(value, dict) and isinstance(report.get(key), dict) else value
        return report

    def render(self, report) -> str:
        out = FakeTTY()
        style = Style.for_stream(out, env=TTY_ENV)
        cli_views.render_status(style, report)
        return visible(out.getvalue())

    def test_healthy(self) -> None:
        text = self.render(self.report())
        self.assertTrue(text.startswith("  ◆ Mac MCP · status\n\n"))
        self.assertIn("  ● Running         pid 4321 · port 8765", text)
        self.assertIn("  ● Public address  mac.example.com · Cloudflare", text)
        self.assertIn("  ● Crash guard     watching", text)
        self.assertIn("\n  Logs  ~/", text.replace("/Users/x", "~"))
        self.assertTrue(text.rstrip("\n").endswith("✓ All good — AI apps can reach this Mac."), text)

    def test_stopped_names_the_next_command(self) -> None:
        text = self.render(self.report(ok=False, state="stopped", exit_code=1,
                                       server={"running": False, "pid": None, "health": None},
                                       remediation=["Start it with: mac-mcp start"]))
        self.assertIn("● Stopped", text)
        self.assertTrue(text.rstrip("\n").endswith("✗ Mac MCP is not running — try: mac-mcp start"), text)

    def test_tunnel_down_ends_with_the_remediation(self) -> None:
        text = self.render(self.report(ok=False, state="degraded", exit_code=2,
                                       public_endpoint={"tunnel_running": False, "route": None},
                                       remediation=["Run mac-mcp restart to start the cloudflare tunnel, or use --local-only to ignore it."]))
        self.assertIn("● Public address  mac.example.com · Cloudflare · tunnel not running", text)
        self.assertIn("⚠ Run mac-mcp restart to start the cloudflare tunnel", text)

    def test_local_only_and_privacy_paused(self) -> None:
        text = self.render(self.report(public_endpoint={"mode": "none", "url": None, "tunnel_running": None, "route": None},
                                       privacy={"paused": True, "private_mode": False}))
        self.assertIn("Public address  local only", text)
        self.assertIn("● Privacy", text)
        self.assertIn("observation paused", text)
        self.assertIn("AI apps on this Mac can reach it.", text)

    def test_status_command_uses_the_screen_and_keeps_its_exit_code(self) -> None:
        missing = ProcessValidation("missing", None, "server", None, "record_missing")
        public = type("Public", (), {"mode": "cloudflare", "endpoint_url": "https://mac.example.com/mcp"})()
        out = FakeTTY()
        with patch.object(cli, "_resolve_server_identity", return_value=(4321, "pid_record")), \
             patch.object(cli, "_validate_managed_pid", return_value=missing), \
             patch.object(cli, "_launchctl_pid", return_value=None), \
             patch.object(cli, "_server_health", return_value="ok"), \
             patch.object(cli, "CLOUDFLARE_PID_FILE", cli.Path("/nonexistent/cloudflared.pid")), \
             patch.object(cli, "resolve_public_endpoint", return_value=public), \
             redirect_stdout(out):
            code = cli.status(argparse.Namespace(json=False, local_only=False))
        self.assertEqual(2, code)
        self.assertIn("tunnel not running", visible(out.getvalue()))
        self.assertNotIn("overall: degraded", out.getvalue())


class StartStop(ThemedTestCase):
    def start(self, server_code=0, server_print="mac-mcp started (pid 4321)."):
        out = FakeTTY()

        def fake_server(args):
            print(server_print)
            return server_code

        public = type("Public", (), {"mode": "cloudflare", "endpoint_url": "https://mac.example.com/mcp"})()
        with patch.object(cli, "_public_endpoint_config", return_value=public), \
             patch.object(cli, "validate_bootstrap_security"), patch.object(cli, "load_settings"), \
             patch.object(cli, "_record_intent"), patch.object(cli, "_start_server", side_effect=fake_server), \
             patch.object(cli, "_stop_unselected_public_processes"), \
             patch.object(cli, "_start_cloudflare", side_effect=lambda args, public: print("cloudflared started") or 0), \
             patch.object(cli, "_resolve_server_identity", return_value=(4321, "pid_record")), \
             redirect_stdout(out):
            code = cli.start(argparse.Namespace(host="127.0.0.1", port=8765))
        return code, re.sub(r"\d+\.\d+s", "0.0s", visible(out.getvalue()))

    def test_start_shows_steps_and_a_summary(self) -> None:
        code, text = self.start()
        self.assertEqual(0, code)
        self.assertTrue(text.startswith("  ◆ Mac MCP · start\n\n"))
        self.assertIn("✓ Started the server", text)
        self.assertIn("✓ Connected the Cloudflare tunnel", text)
        self.assertNotIn("cloudflared started", text)  # a step's own prints show only when it fails
        self.assertTrue(text.rstrip("\n").endswith("✓ Started — pid 4321 · mac.example.com"), text)

    def test_a_failed_server_step_shows_its_reason(self) -> None:
        code, text = self.start(server_code=1, server_print="configured port 8765 is used by Python (pid 99).")
        self.assertEqual(1, code)
        self.assertIn("✗ Start the server", text)
        self.assertIn("      configured port 8765 is used by Python (pid 99).", text)
        self.assertTrue(text.rstrip("\n").endswith("✗ Mac MCP did not start — try: mac-mcp doctor"), text)

    def test_uninstall_stops_quietly_without_a_header(self) -> None:
        # uninstall prints its own report; a themed stop would put a second header in it.
        out = FakeTTY()
        with patch.object(cli, "_stop_plain", return_value=0) as plain, patch.object(cli, "stop") as themed, \
             patch("mcp_server.update_helper._stop_menu_app"), redirect_stdout(out):
            cli._stop_for_uninstall(cli.Path("/nonexistent/Mac MCP.app"))
        plain.assert_called_once()
        themed.assert_not_called()

    def test_stop(self) -> None:
        out = FakeTTY()
        with patch.object(cli, "_record_intent"), \
             patch.object(cli, "_resolve_server_identity", return_value=(4321, "pid_record")), \
             patch.object(cli, "_stop_pid", return_value=True), patch.object(cli, "_stop_cloudflare", return_value=True), \
             patch.object(cli, "_server_job_plist_path", return_value=cli.Path("/nonexistent/job.plist")), \
             redirect_stdout(out):
            code = cli.stop(argparse.Namespace(timeout=5.0, force=False, keep_intent=False))
        text = visible(out.getvalue())
        self.assertEqual(0, code)
        self.assertIn("✓ Stopped the server", text)
        self.assertIn("✓ Stopped the public tunnel", text)
        self.assertTrue(text.rstrip("\n").endswith("✓ Stopped — AI apps can no longer reach this Mac."), text)


class RestartScreen(ThemedTestCase):
    def test_stages_become_steps_and_the_outcome_the_summary(self) -> None:
        payloads = iter([
            {"state": "running", "stage": "stopping", "updated_at": 2e9},
            {"state": "running", "stage": "starting", "updated_at": 2e9},
            {"state": "running", "stage": "verifying", "updated_at": 2e9},
            {"state": "succeeded", "server_pid": 4321, "updated_at": 2e9},
        ])
        out = FakeTTY()
        with patch.dict(os.environ, {cli.RESTART_HANDOFF_ENV: ""}), \
             patch.object(cli, "_spawn_detached_restart", side_effect=lambda args: print("handed off") or 0) as spawn, \
             patch.object(cli, "_read_restart_status", side_effect=lambda: next(payloads)), \
             patch.object(cli.time, "sleep"), \
             redirect_stdout(out):
            code = cli.restart(argparse.Namespace(wait=False, timeout=5.0))
        text = re.sub(r"\d+\.\d+s", "0.0s", visible(out.getvalue()))
        self.assertEqual(0, code)
        self.assertFalse(spawn.call_args[0][0].wait)  # the themed path waits itself
        for label in ("✓ Handed the restart to launchd", "✓ Stopped the server", "✓ Started the server", "✓ Checked that it answers"):
            self.assertIn(label, text)
        self.assertTrue(text.rstrip("\n").endswith("✓ Restarted — pid 4321"), text)

    def test_the_worker_does_not_wait_for_a_watching_terminal(self) -> None:
        # The themed restart stays alive to show the stages, like --wait: the worker must not
        # wait for it to exit first (live run: requester_wait failure, no restart).
        payloads = iter([{"state": "succeeded", "server_pid": 1, "updated_at": 2e9}])
        with patch.dict(os.environ, {cli.RESTART_HANDOFF_ENV: ""}), \
             patch.object(cli, "_spawn_detached_restart", return_value=0) as spawn, \
             patch.object(cli, "_read_restart_status", side_effect=lambda: next(payloads)), \
             patch.object(cli.time, "sleep"), redirect_stdout(FakeTTY()):
            cli.restart(argparse.Namespace(wait=False, timeout=5.0))
        handed = spawn.call_args[0][0]
        self.assertFalse(handed.wait)
        self.assertTrue(handed.requester_waits)
        import plistlib
        import tempfile
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(cli, "STATE_DIR", cli.Path(temp)), patch.object(cli, "LOG_FILE", cli.Path(temp) / "log"), \
             patch.object(cli, "_restart_handoff_plist_path", return_value=cli.Path(temp) / "job.plist"), \
             patch.object(cli, "_restart_handoff_command", return_value=["mac-mcp", "restart"]):
            for waits, expected in ((True, "0"), (False, str(os.getpid()))):
                path = cli._write_restart_handoff_plist(argparse.Namespace(wait=False, requester_waits=waits))
                arguments = plistlib.loads(path.read_bytes())["ProgramArguments"]
                self.assertIn(f"{cli.RESTART_REQUESTER_ENV}={expected}", arguments)

    def test_a_failure_before_any_stage_says_where_it_stopped(self) -> None:
        payloads = iter([{"state": "failed", "stage": "requester_wait", "updated_at": 2e9}])
        out = FakeTTY()
        with patch.dict(os.environ, {cli.RESTART_HANDOFF_ENV: ""}), \
             patch.object(cli, "_spawn_detached_restart", return_value=0), \
             patch.object(cli, "_read_restart_status", side_effect=lambda: next(payloads)), \
             patch.object(cli.time, "sleep"), redirect_stdout(out):
            code = cli.restart(argparse.Namespace(wait=False, timeout=5.0))
        self.assertEqual(1, code)
        self.assertIn("✗ Restart failed (requester wait) — try: mac-mcp doctor", visible(out.getvalue()))

    def test_a_failed_restart_names_the_stage_and_the_repair(self) -> None:
        payloads = iter([
            {"state": "running", "stage": "starting", "updated_at": 2e9},
            {"state": "failed", "stage": "health", "repair_command": "mac-mcp doctor", "updated_at": 2e9},
        ])
        out = FakeTTY()
        with patch.dict(os.environ, {cli.RESTART_HANDOFF_ENV: ""}), \
             patch.object(cli, "_spawn_detached_restart", return_value=0), \
             patch.object(cli, "_read_restart_status", side_effect=lambda: next(payloads)), \
             patch.object(cli.time, "sleep"), redirect_stdout(out):
            code = cli.restart(argparse.Namespace(wait=False, timeout=5.0))
        text = visible(out.getvalue())
        self.assertEqual(1, code)
        self.assertIn("✗ Start the server", text)
        self.assertTrue(text.rstrip("\n").endswith("✗ Restart failed — try: mac-mcp doctor"), text)


class DoctorScreen(ThemedTestCase):
    def report(self, *checks, health="healthy", ok=True):
        return {"ok": ok, "health": health, "counts": {}, "checks": [
            {"check_id": check_id, "category": check_id.split(".")[0], "status": status, "summary": summary,
             **({"remediation": fix} if fix else {})}
            for check_id, status, summary, fix in checks]}

    def render(self, report, verbose=False) -> str:
        out = FakeTTY()
        cli_views.render_doctor(Style.for_stream(out, env=TTY_ENV), report, verbose, None)
        return visible(out.getvalue())

    def test_passes_fold_per_group_and_notes_are_written_out(self) -> None:
        text = self.render(self.report(
            ("runtime.python", "pass", "Python 3.14.7 on arm64", None),
            ("runtime.venv", "pass", "Runtime Python is present.", None),
            ("permissions.accessibility", "pass", "Accessibility is allowed.", None),
            ("permissions.microphone", "info", "Microphone is asked the first time you use voice.", None),
            ("dependencies.cloudflared", "pass", "cloudflared is available.", None),
        ))
        self.assertTrue(text.startswith("  ◆ Mac MCP · doctor\n\n"))
        self.assertIn("Runtime       ✓ 2 checks passed", text)
        self.assertIn("Permissions   ✓ Accessibility", text)
        self.assertIn("ℹ Microphone is asked the first time you use voice.", text)
        self.assertIn("Dependencies  ✓ cloudflared", text)
        self.assertTrue(text.rstrip("\n").endswith("✓ Everything Mac MCP needs is in place.   (--verbose: every check)"), text)

    def test_warnings_and_failures_show_their_fix(self) -> None:
        text = self.render(self.report(
            ("server.health", "fail", "The server does not answer /health.", "Run mac-mcp restart."),
            ("agents.worktrees", "warn", "2 agent worktrees hold unreviewed changes.", "Review them in Settings › Agents."),
            ok=False))
        self.assertIn("✗ The server does not answer /health.", text)
        self.assertIn("Fix: Run mac-mcp restart.", text)
        self.assertIn("⚠ 2 agent worktrees hold unreviewed changes.", text)
        self.assertTrue(text.rstrip("\n").endswith("✗ 1 problem and 1 warning — fix them, then run: mac-mcp doctor"), text)

    def test_long_notes_and_fixes_wrap_inside_the_terminal(self) -> None:
        long_fix = "Port 8765 is used by Python (pid 50849). Mac MCP never stops programs it did not start: quit that program."
        out = FakeTTY()
        style = Style.for_stream(out, env={**TTY_ENV, "COLUMNS": "60"})
        cli_views.render_doctor(style, self.report(
            ("server.port", "fail", "Configured Mac MCP port 8765 is used by Python and cannot be used now.", long_fix), ok=False),
            False, None)
        lines = visible(out.getvalue()).rstrip("\n").split("\n")
        self.assertTrue(all(len(line) <= 60 for line in lines), [line for line in lines if len(line) > 60])
        fix_lines = [line for line in lines if "Fix:" in line or "never stops" in line or "quit that" in line]
        self.assertGreater(len(fix_lines), 1)
        column = lines[[i for i, line in enumerate(lines) if "Fix:" in line][0]].index("Fix:")
        self.assertTrue(all(line[:column].strip() == "" for line in fix_lines), fix_lines)

    def test_an_unknown_category_lands_in_other(self) -> None:
        text = self.render(self.report(("brandnew.check", "warn", "Something new to look at.", None), ok=True))
        self.assertIn("Other", text)
        self.assertIn("⚠ Something new to look at.", text)

    def test_verbose_lists_every_check(self) -> None:
        text = self.render(self.report(("runtime.python", "pass", "Python 3.14.7 on arm64", None)), verbose=True)
        self.assertIn("✓ Python 3.14.7 on arm64", text)

    def test_doctor_command_json_and_plain_are_untouched(self) -> None:
        from mcp_server import diagnostics
        report = self.report(("runtime.python", "pass", "Python 3.14.7 on arm64", None))
        out = FakeTTY()
        with patch.object(diagnostics, "run_doctor", return_value=report), redirect_stdout(out):
            code = cli.doctor(argparse.Namespace(json=True, support_bundle=None, local_only=False, verbose=False))
        self.assertEqual(0, code)
        self.assertTrue(out.getvalue().lstrip().startswith("{"))


class ReviewFindings(ThemedTestCase):
    """Fixes from the whole-branch review of the CLI theme."""

    def test_interrupting_the_restart_watch_fixes_the_line_and_shows_the_cursor(self) -> None:
        def payloads():
            yield {"state": "running", "stage": "stopping", "updated_at": 2e9}
            raise KeyboardInterrupt
        feed = payloads()
        out = FakeTTY()
        with patch.dict(os.environ, {cli.RESTART_HANDOFF_ENV: ""}), \
             patch.object(cli, "_spawn_detached_restart", return_value=0), \
             patch.object(cli, "_read_restart_status", side_effect=lambda: next(feed)), \
             patch.object(cli.time, "sleep"), redirect_stdout(out), self.assertRaises(KeyboardInterrupt):
            cli.restart(argparse.Namespace(wait=False, timeout=5.0))
        raw = out.getvalue()
        self.assertIn("✗ Stop the server", visible(raw))
        self.assertIn("\x1b[?25h", raw.rsplit("Stop the server", 1)[1])

    def test_themed_stop_stops_cloudflared_even_when_ngrok_fails(self) -> None:
        out = FakeTTY()
        with patch.object(cli, "_record_intent"), \
             patch.object(cli, "_resolve_server_identity", return_value=(4321, "pid_record")), \
             patch.object(cli, "_stop_pid", side_effect=lambda path, *a: path != cli.NGROK_PID_FILE), \
             patch.object(cli, "_stop_cloudflare", return_value=True) as cloudflare, \
             patch.object(cli, "_server_job_plist_path", return_value=cli.Path("/nonexistent/job.plist")), \
             redirect_stdout(out):
            code = cli.stop(argparse.Namespace(timeout=5.0, force=False, keep_intent=False))
        self.assertEqual(1, code)
        cloudflare.assert_called_once()

    def test_themed_stop_when_nothing_was_running(self) -> None:
        out = FakeTTY()
        with patch.object(cli, "_record_intent"), \
             patch.object(cli, "_resolve_server_identity", return_value=(None, "not_running")), \
             patch.object(cli, "PID_FILE", cli.Path("/nonexistent/server.pid")), \
             patch.object(cli, "_stop_pid", return_value=True), patch.object(cli, "_stop_cloudflare", return_value=True), \
             patch.object(cli, "_server_job_plist_path", return_value=cli.Path("/nonexistent/job.plist")), \
             redirect_stdout(out):
            cli.stop(argparse.Namespace(timeout=5.0, force=False, keep_intent=False))
        self.assertTrue(visible(out.getvalue()).rstrip("\n").endswith("✓ Mac MCP was not running."), visible(out.getvalue()))

    def test_themed_stop_with_unverifiable_ownership_points_to_status(self) -> None:
        out = FakeTTY()
        with patch.object(cli, "_record_intent"), \
             patch.object(cli, "_resolve_server_identity", return_value=(None, "foreign_listener")), \
             patch.object(cli, "_stop_pid", return_value=True), patch.object(cli, "_stop_cloudflare", return_value=True), \
             patch.object(cli, "_server_job_plist_path", return_value=cli.Path("/nonexistent/job.plist")), \
             redirect_stdout(out):
            code = cli.stop(argparse.Namespace(timeout=5.0, force=False, keep_intent=False))
        self.assertEqual(1, code)
        self.assertIn("try: mac-mcp status", visible(out.getvalue()))

    def test_only_the_home_screen_asks_for_a_style_in_main(self) -> None:
        with patch.object(cli.Style, "for_stream", side_effect=AssertionError("no style for --version")), \
             redirect_stdout(FakeTTY()), self.assertRaises(SystemExit):
            cli.main(["--version"])

    def test_a_successful_start_keeps_its_warnings_and_says_already_running(self) -> None:
        out = FakeTTY()
        public = type("Public", (), {"mode": "none", "endpoint_url": None})()

        def server(args):
            print("mac-mcp is already running (pid 4321).")
            print("Warning: crash supervisor could not be loaded (launchctl failed).")
            return 0

        with patch.object(cli, "_public_endpoint_config", return_value=public), \
             patch.object(cli, "validate_bootstrap_security"), patch.object(cli, "load_settings"), \
             patch.object(cli, "_record_intent"), patch.object(cli, "_start_server", side_effect=server), \
             patch.object(cli, "_stop_unselected_public_processes"), \
             patch.object(cli, "_resolve_server_identity", return_value=(4321, "pid_record")), redirect_stdout(out):
            cli.start(argparse.Namespace(host="127.0.0.1", port=8765))
        text = visible(out.getvalue())
        self.assertIn("✓ The server was already running", text)
        self.assertIn("⚠ Warning: crash supervisor could not be loaded (launchctl failed).", text)

    def test_restart_steps_agree_with_the_outcome(self) -> None:
        def run(stages):
            feed = iter(stages)
            out = FakeTTY()
            with patch.dict(os.environ, {cli.RESTART_HANDOFF_ENV: ""}), \
                 patch.object(cli, "_spawn_detached_restart", return_value=0), \
                 patch.object(cli, "_read_restart_status", side_effect=lambda: next(feed)), \
                 patch.object(cli.time, "sleep"), redirect_stdout(out):
                cli.restart(argparse.Namespace(wait=False, timeout=5.0))
            return visible(out.getvalue())
        recovered = run([{"state": "running", "stage": "starting", "updated_at": 2e9},
                         {"state": "running", "stage": "recovering", "updated_at": 2e9},
                         {"state": "succeeded", "server_pid": 7, "recovered_after_retry": True, "updated_at": 2e9}])
        self.assertIn("✗ Start the server", recovered)
        self.assertIn("✓ Recovered after a failed start", recovered)
        degraded = run([{"state": "running", "stage": "starting", "updated_at": 2e9},
                        {"state": "running", "stage": "public_endpoint", "updated_at": 2e9},
                        {"state": "degraded", "updated_at": 2e9}])
        self.assertIn("✓ Started the server", degraded)
        self.assertIn("✗ Connect the public tunnel", degraded)

    def test_local_only_degraded_status_does_not_suggest_local_only(self) -> None:
        report = StatusScreen.report(self, ok=True, state="degraded", exit_code=0,
                                     public_endpoint={"tunnel_running": False, "route": None},
                                     remediation=["Run mac-mcp restart to start the cloudflare tunnel, or use --local-only to ignore it."])
        out = FakeTTY()
        cli_views.render_status(Style.for_stream(out, env=TTY_ENV), report)
        summary = " ".join(visible(out.getvalue()).rstrip("\n").split("\n\n")[-1].split())
        self.assertNotIn("--local-only", summary)
        self.assertIn("public address is not available", summary)
        self.assertTrue(summary.startswith("✓ Running here"), summary)

    def test_everything_fits_an_80_column_terminal(self) -> None:
        for argv in (["doctor", "--help"], ["start", "--help"], ["restart", "--help"], ["status", "--help"]):
            with self.subTest(argv=argv):
                _, text = self.run_main(argv)
                self.assertTrue(all(len(line) <= 80 for line in text.split("\n")), [l for l in text.split("\n") if len(l) > 80])

    def test_home_fits_40_columns(self) -> None:
        out = FakeTTY()
        cli_views.render_home(Style.for_stream(out, env={**TTY_ENV, "COLUMNS": "40"}), "2.2.0", True)
        lines = visible(out.getvalue()).split("\n")
        self.assertTrue(all(len(line) <= 40 for line in lines), [l for l in lines if len(l) > 40])

    def test_doctor_hint_stays_whole_when_the_summary_wraps(self) -> None:
        out = FakeTTY()
        report = DoctorScreen.report(self, ("runtime.python", "pass", "ok", None))
        cli_views.render_doctor(Style.for_stream(out, env={**TTY_ENV, "COLUMNS": "50"}), report, False, None)
        text = visible(out.getvalue())
        self.assertIn("(--verbose: every check)", text)
        self.assertTrue(all(len(line) <= 50 for line in text.split("\n")), text)


PHASE2_APPS = [
    {"id": "codex", "name": "Codex", "installed": True, "added": True, "keyless": True},
    {"id": "claude-desktop", "name": "Claude Desktop", "installed": True, "added": True, "keyless": False},
    {"id": "kimi", "name": "Kimi Code", "installed": True, "added": False, "keyless": None},
    {"id": "goose", "name": "Goose", "installed": False, "added": False, "keyless": None},
    {"id": "pi", "name": "pi", "installed": False, "added": False, "keyless": None},
]


class ClientsScreen(ThemedTestCase):
    def run_clients(self, apps):
        from mcp_server import client_setup
        out = FakeTTY()
        with patch.object(client_setup, "apps_status", return_value=apps), redirect_stdout(out):
            code = cli.clients(argparse.Namespace(json=False))
        return code, visible(out.getvalue())

    def test_added_old_way_installed_and_missing(self) -> None:
        code, text = self.run_clients(PHASE2_APPS)
        self.assertEqual(0, code)
        self.assertTrue(text.startswith("  ◆ Mac MCP · clients\n\n"), text)
        self.assertIn("● Codex           added", text)
        self.assertIn("● Claude Desktop  added the old way · key in its settings", text)
        self.assertIn("● Kimi Code       on this Mac · not added", text)
        self.assertIn("Not on this Mac: Goose · pi", text)
        # An old setup is updated first; the command sits in the summary, where it never breaks.
        self.assertIn("⚠ Claude Desktop holds Mac MCP's key in its own settings — try: mac-mcp connect --client claude-desktop",
                      " ".join(text.split()))

    def test_without_an_old_setup_the_summary_suggests_the_next_app(self) -> None:
        apps = [app for app in PHASE2_APPS if app["id"] != "claude-desktop"]
        _, text = self.run_clients(apps)
        self.assertIn("✓ Mac MCP is added to 1 of 2 apps on this Mac — try: mac-mcp connect --client kimi", " ".join(text.split()))

    def test_no_app_on_this_mac(self) -> None:
        code, text = self.run_clients([a for a in PHASE2_APPS if not a["installed"]])
        self.assertEqual(0, code)
        self.assertIn("ℹ No AI app Mac MCP knows is on this Mac.", text)
        self.assertNotIn("try:", text)

    def test_json_stays_json(self) -> None:
        from mcp_server import client_setup
        out = FakeTTY()
        with patch.object(client_setup, "apps_status", return_value=PHASE2_APPS), redirect_stdout(out):
            cli.clients(argparse.Namespace(json=True))
        self.assertTrue(out.getvalue().startswith('{"ok": true'))


class ConnectScreen(ThemedTestCase):
    def connect(self, client, **mock):
        from mcp_server import client_setup
        out, err = FakeTTY(), io.StringIO()
        with patch.object(client_setup, "connect", **mock), redirect_stdout(out), redirect_stderr(err):
            code = cli.connect_app(argparse.Namespace(client=client, json=False))
        return code, re.sub(r"\d+\.\d+s", "0.0s", visible(out.getvalue())), err.getvalue()

    def test_success(self) -> None:
        code, text, _ = self.connect("codex", return_value={"ok": True, "name": "Codex", "next": "Start a new Codex session, then ask it something about your Mac."})
        self.assertEqual(0, code)
        self.assertTrue(text.startswith("  ◆ Mac MCP · connect\n\n"), text)
        self.assertIn("✓ Added Mac MCP to Codex", text)
        self.assertIn("✓ Start a new Codex session, then ask it something about your Mac.", " ".join(text.split()))

    def test_failure_shows_the_reason_and_keeps_the_code(self) -> None:
        from mcp_server import client_setup
        code, text, err = self.connect("kimi", side_effect=client_setup.SetupError("Kimi Code is not installed on this Mac."))
        self.assertEqual(1, code)
        self.assertEqual("", err)
        self.assertIn("✗ Add Mac MCP to Kimi Code", text)
        self.assertIn("Kimi Code is not installed on this Mac.", text)
        self.assertIn("✗ Mac MCP was not added — try: mac-mcp clients", " ".join(text.split()))

    def test_chatgpt_never_prints_the_address(self) -> None:
        out = FakeTTY()
        result = {"ok": True, "client": "chatgpt", "name": "ChatGPT", "copied": True, "next": "The address is copied."}
        with patch.object(cli, "_copy_chatgpt_address", return_value=result), redirect_stdout(out):
            code = cli.connect_app(argparse.Namespace(client="chatgpt", json=False))
        self.assertEqual(0, code)
        text = visible(out.getvalue())
        self.assertIn("✓ Copied ChatGPT's address", text)
        self.assertNotIn("ApiKey", text)


class ConnectConfigScreen(ThemedTestCase):
    def run_config(self, client="codex", endpoint=("http://127.0.0.1:8765/mcp", "local")):
        settings = type("S", (), {"allow_no_auth": False, "api_key": "never-printed"})()
        out, err = FakeTTY(), io.StringIO()
        with patch.object(cli, "_connect_config_endpoint", return_value=endpoint), \
             patch.object(cli, "load_settings", return_value=settings), \
             patch.object(cli, "_public_endpoint_check", return_value="unreachable"), \
             redirect_stdout(out), redirect_stderr(err):
            code = cli.connect_config(argparse.Namespace(client=client, endpoint="auto", name="mac-mcp", auth_env="MAC_MCP_API_KEY"))
        return code, out.getvalue(), err.getvalue()

    def test_the_snippet_is_copyable_byte_for_byte(self) -> None:
        from mcp_server.connection_config import render_connection_config
        code, raw, _ = self.run_config()
        self.assertEqual(0, code)
        text = visible(raw)
        self.assertTrue(text.startswith("  ◆ Mac MCP · connect-config\n\n"), text[:60])
        self.assertIn("Client", text)
        self.assertIn("Endpoint", text)
        snippet = render_connection_config(client="codex", endpoint_url="http://127.0.0.1:8765/mcp", auth_required=True,
                                           server_name="mac-mcp", auth_env="MAC_MCP_API_KEY").snippet
        self.assertIn("\n" + snippet + "\n", raw)  # raw: no colour codes, no indent inside the snippet
        self.assertIn("Copy into", text)
        self.assertIn("Secret setup", text)
        self.assertNotIn("never-printed", raw)
        self.assertTrue(" ".join(text.split()).endswith("✓ Snippet ready — no secret is printed."), text[-120:])

    def test_a_public_endpoint_that_does_not_answer_is_a_warning(self) -> None:
        code, raw, _ = self.run_config(client="chatgpt", endpoint=("https://mac.example.com/mcp", "public"))
        text = " ".join(visible(raw).split())
        self.assertEqual(0, code)
        self.assertIn("not reachable right now", text)
        self.assertIn("⚠", text.rsplit("\n", 1)[-1])

    def test_an_error_is_a_themed_line(self) -> None:
        settings = type("S", (), {"allow_no_auth": False, "api_key": ""})()
        out, err = FakeTTY(), io.StringIO()
        with patch.object(cli, "_connect_config_endpoint", return_value=("http://127.0.0.1:8765/mcp", "local")), \
             patch.object(cli, "load_settings", return_value=settings), redirect_stdout(out), redirect_stderr(err):
            code = cli.connect_config(argparse.Namespace(client="codex", endpoint="auto", name="mac-mcp", auth_env="MAC_MCP_API_KEY"))
        self.assertEqual(1, code)
        self.assertIn("✗ authentication is required but MCP_API_KEY is not configured", " ".join(visible(out.getvalue()).split()))
        self.assertNotIn("\n\n\n", visible(out.getvalue()))  # one blank line under the header, not two


def _update_info(**changes):
    from mcp_server.update_helper import UpdateInfo
    base = dict(repo="/r", runtime="/rt", branch="main", remote="origin", deployed_commit="05b6e68c1111",
                repo_commit="aaaa", target_commit="05b6e68c1111", behind_by=0, update_available=False, dirty=False)
    base.update(changes)
    return UpdateInfo(**base)


class UpdateScreen(ThemedTestCase):
    def check(self, info):
        out = FakeTTY()
        with patch.object(cli, "install_kind", return_value="git"), patch.object(cli, "validate_update_state"), \
             patch.object(cli, "check_update", return_value=info), redirect_stdout(out):
            code = cli.update(argparse.Namespace(check=True, json=False, repo=None, runtime=None, branch="main", remote="origin"))
        return code, " ".join(visible(out.getvalue()).split())

    def test_check_states(self) -> None:
        code, text = self.check(_update_info())
        self.assertEqual(0, code)
        self.assertTrue(text.startswith("◆ Mac MCP · update"), text)
        self.assertIn("Installed 05b6e68c", text)
        self.assertTrue(text.endswith("✓ Mac MCP is up to date."), text)
        code, text = self.check(_update_info(update_available=True, behind_by=2, target_commit="bbbbbbbb2222",
                                             release_id="stable-2.2.1-r1", release_version="2.2.1"))
        self.assertEqual(0, code)
        self.assertIn("Verified stable bbbbbbbb · stable-2.2.1-r1 (v2.2.1)", text)
        self.assertTrue(text.endswith("✓ A verified update is ready — try: mac-mcp update"), text)
        code, text = self.check(_update_info(unverified_ahead=49))
        self.assertIn("49 development commits ahead", text)
        self.assertTrue(text.endswith("✓ No newer verified stable release."), text)
        code, text = self.check(_update_info(dirty=True))
        self.assertEqual(2, code)
        self.assertTrue(text.endswith("✗ Update blocked: the repository has local changes"), text)

    def test_an_error_before_any_screen_still_has_a_header(self) -> None:
        out = FakeTTY()
        with patch.object(cli, "install_kind", return_value="git"), \
             patch.object(cli, "validate_update_state", side_effect=cli.UpdateError("The last update did not finish.")), \
             redirect_stdout(out):
            code = cli.update(argparse.Namespace(check=True, json=False, repo=None, runtime=None, branch="main", remote="origin"))
        text = visible(out.getvalue())
        self.assertEqual(1, code)
        self.assertTrue(text.startswith("  ◆ Mac MCP · update\n\n  ✗ Update failed: The last update did not finish."), text)

    def test_pkg_install(self) -> None:
        out = FakeTTY()
        with patch.object(cli, "install_kind", return_value="pkg"), redirect_stdout(out), \
                patch("mcp_server.install_kind._fetch_latest_release", side_effect=OSError("offline in tests")):
            code = cli.update(argparse.Namespace(check=True, json=False))
        text = " ".join(visible(out.getvalue()).split())
        self.assertEqual(0, code)
        self.assertIn("Download", text)
        self.assertIn("ℹ", text)

    def apply(self, info, return_code=0, log="step one\nstep two\n", raise_exc=None):
        import tempfile
        from mcp_server import update_helper
        out, err = FakeTTY(), io.StringIO()
        with tempfile.TemporaryDirectory() as temp:
            log_path = cli.Path(temp) / "upd_1.log"
            log_path.write_text(log)
            proc = type("P", (), {"wait": lambda self: return_code})()
            launch = patch.object(cli, "launch_detached_update",
                                  side_effect=raise_exc or (lambda *a, **k: ({"updater_pid": 9, "log_path": str(log_path)}, proc)))
            with patch.object(cli, "install_kind", return_value="git"), patch.object(cli, "validate_update_state"), \
                 patch.object(cli, "resolve_update_paths", return_value=(cli.Path("/r"), cli.Path("/rt"))), \
                 patch.object(cli, "check_update", return_value=info), \
                 patch.object(cli, "secure_bootstrap_update_blocker", return_value=None), launch, \
                 redirect_stdout(out), redirect_stderr(err):
                code = cli.update(argparse.Namespace(check=False, json=False, repo=None, runtime=None, branch="main",
                                                     remote="origin", skip_restart=False, skip_deps=False))
        return code, re.sub(r"\d+\.\d+s", "0.0s", visible(out.getvalue())), err.getvalue()

    def test_apply_success_and_up_to_date(self) -> None:
        ready = _update_info(update_available=True, behind_by=1, target_commit="bbbbbbbb2222", release_id="stable-2.2.1-r1")
        code, text, _ = self.apply(ready)
        self.assertEqual(0, code)
        self.assertIn("✓ Checked for a verified release", text)
        self.assertIn("✓ Installed the update", text)
        self.assertIn("✓ Updated to stable-2.2.1-r1", " ".join(text.split()))
        self.assertNotIn("step one", text)
        code, text, _ = self.apply(_update_info())
        self.assertEqual(0, code)
        self.assertIn("✓ Mac MCP is already up to date.", " ".join(text.split()))

    def test_apply_failure_shows_the_end_of_the_log(self) -> None:
        ready = _update_info(update_available=True, behind_by=1, target_commit="bbbbbbbb2222")
        log = "".join(f"line {n}\n" for n in range(40)) + "ERROR: signature check failed\n"
        code, text, _ = self.apply(ready, return_code=1, log=log)
        self.assertEqual(1, code)
        self.assertIn("✗ Install the update", text)
        self.assertIn("ERROR: signature check failed", text)
        self.assertNotIn("line 10\n", text)
        self.assertIn("✗ The update did not finish — try: mac-mcp logs update", " ".join(text.split()))

    def test_an_update_already_running(self) -> None:
        ready = _update_info(update_available=True, behind_by=1, target_commit="bbbbbbbb2222")
        code, text, err = self.apply(ready, raise_exc=cli.UpdateInProgress({"update_id": "upd_1", "log_path": "/l/upd.log"}))
        self.assertEqual(1, code)
        self.assertEqual("", err)
        self.assertIn("already in progress", " ".join(text.split()))
        self.assertIn("/l/upd.log", text)


class LogsScreen(ThemedTestCase):
    def run_logs(self, files: dict, **args):
        import tempfile
        out, err = FakeTTY(), io.StringIO()
        with tempfile.TemporaryDirectory() as temp:
            paths = {}
            for name, text in files.items():
                path = cli.Path(temp) / f"{name}.log"
                if text is not None:
                    path.write_text(text)
                paths[name] = path
            updates = cli.Path(temp) / "update"
            updates.mkdir()
            with patch.object(cli, "managed_logs", return_value=paths), patch.object(cli, "update_logs_dir", return_value=updates), \
                 patch.object(cli, "log_limits", return_value={"max_bytes": 10 * 1024 * 1024, "backups": 3, "update_logs_kept": 20}), \
                 redirect_stdout(out), redirect_stderr(err):
                code = cli.logs(argparse.Namespace(**{"component": "server", "lines": 3, "list": False, **args}))
        return code, out.getvalue(), err.getvalue()

    def test_lines_are_written_raw(self) -> None:
        long_line = "INFO: " + "x" * 300
        code, raw, _ = self.run_logs({"server": "first\n" + long_line + "\nlast line\n"})
        self.assertEqual(0, code)
        self.assertTrue(visible(raw).startswith("  ◆ Mac MCP · logs\n\n"), raw[:80])
        self.assertIn("\n" + long_line + "\nlast line\n", raw)  # whole, uncoloured, unindented
        self.assertIn("secrets redacted", visible(raw))
        self.assertIn("✓ 3 lines", visible(raw))

    def test_a_missing_log(self) -> None:
        code, raw, err = self.run_logs({"server": None})
        self.assertEqual((1, ""), (code, err))
        self.assertIn("✗ No server log yet", visible(raw))

    def test_the_list(self) -> None:
        code, raw, _ = self.run_logs({"server": "a\n", "cloudflared": None}, list=True)
        text = visible(raw)
        self.assertEqual(0, code)
        self.assertIn("server", text)
        self.assertIn("cloudflared", text)
        self.assertIn("10 MB × 4 files per log", text)
        self.assertIn("✓ 3 logs", text)


class PrivacyDeleteScreen(ThemedTestCase):
    PLAN = [{"key": "memory", "name": "Memory", "exists": True, "bytes": 2_097_152, "paths": ["/s/memory"]},
            {"key": "lessons", "name": "Lessons", "exists": False, "bytes": 0, "paths": ["/s/lessons"]}]

    def run_delete(self, *, yes, running=False, result=None, stores=("memory", "lessons")):
        from mcp_server import privacy_inventory
        out, err = FakeTTY(), io.StringIO()
        result = result or {"moved": [{"path": "/s/memory", "to": "/T/memory"}], "failed": []}
        with patch.object(privacy_inventory, "delete_plan", return_value=self.PLAN), \
             patch.object(privacy_inventory, "trash_stores", return_value=result) as trash, \
             patch.object(cli, "_resolve_server_identity", return_value=(4321, "pid_record") if running else (None, "not_running")), \
             patch.object(cli, "STATE_DIR", cli.DEFAULT_STATE_DIR), \
             patch.object(cli, "_stop_plain", return_value=0) as stop, \
             patch.object(cli, "_start_again_as_before", return_value=True) as start, \
             redirect_stdout(out), redirect_stderr(err):
            code = cli.privacy_delete(argparse.Namespace(all=False, store=list(stores) if stores else None, yes=yes, json=False))
        return code, re.sub(r"\d+\.\d+s", "0.0s", visible(out.getvalue())), err.getvalue(), trash, stop, start

    def test_preview_changes_nothing_and_names_the_command(self) -> None:
        code, text, _, trash, stop, start = self.run_delete(yes=False)
        self.assertEqual(0, code)
        self.assertTrue(text.startswith("  ◆ Mac MCP · privacy delete\n\n"), text)
        self.assertIn("Memory   2.0 MB · /s/memory", text)
        self.assertIn("Lessons  empty · /s/lessons", text)
        flat = " ".join(text.split())
        self.assertIn("⚠ Nothing was changed — try: mac-mcp privacy delete --store memory --store lessons --yes", flat)
        trash.assert_not_called()
        stop.assert_not_called()

    def test_no_store_chosen(self) -> None:
        code, text, err, *_ = self.run_delete(yes=False, stores=None)
        self.assertEqual((2, ""), (code, err))
        self.assertIn("✗ Choose --all or --store from:", text)

    def test_yes_while_running_stops_moves_and_starts(self) -> None:
        code, text, _, trash, stop, start = self.run_delete(yes=True, running=True)
        self.assertEqual(0, code)
        self.assertIn("✓ Stopped Mac MCP", text)
        self.assertIn("✓ Moved 1 store to the Trash", text)
        self.assertIn("✓ Started Mac MCP again", text)
        self.assertIn("✓ Moved to the Trash: /s/memory", " ".join(text.split()))
        stop.assert_called_once()
        self.assertTrue(stop.call_args[0][0].keep_intent)
        start.assert_called_once()

    def test_yes_while_not_running_leaves_the_server_alone(self) -> None:
        code, text, _, trash, stop, start = self.run_delete(yes=True, running=False)
        self.assertEqual(0, code)
        self.assertNotIn("Stopped Mac MCP", text)
        stop.assert_not_called()
        start.assert_not_called()

    def test_a_failed_move(self) -> None:
        result = {"moved": [], "failed": [{"path": "/s/memory", "error": "Operation not permitted"}]}
        code, text, *_ = self.run_delete(yes=True, result=result)
        self.assertEqual(1, code)
        self.assertIn("✗ Move the stores to the Trash", text)
        self.assertIn("/s/memory: Operation not permitted", text)
        self.assertIn("try: mac-mcp privacy delete --store memory --store lessons --yes", " ".join(text.split()))


class Phase2ReviewFindings(ThemedTestCase):
    """Fixes from the phase 2 whole-branch review."""

    def lines_with(self, text, needle):
        return [line for line in text.split("\n") if needle in line]

    def test_a_long_suggested_command_never_breaks_mid_word(self) -> None:
        for width in (40, 80):
            out = FakeTTY()
            style = Style.for_stream(out, env={**TTY_ENV, "COLUMNS": str(width)})
            command = "mac-mcp privacy delete --store tool_activity --store run_evidence --store file_history --yes"
            style.summary("warn", "Nothing was changed", next_command=command)
            text = visible(out.getvalue())
            self.assertEqual(1, len(self.lines_with(text, command)), text)  # whole, on one line

    def test_clients_old_way_row_keeps_no_command_and_the_summary_names_it(self) -> None:
        from mcp_server import client_setup
        out = FakeTTY()
        with patch.object(client_setup, "apps_status", return_value=PHASE2_APPS), redirect_stdout(out):
            cli.clients(argparse.Namespace(json=False))
        text = visible(out.getvalue())
        self.assertEqual(1, len(self.lines_with(text, "mac-mcp connect --client claude-desktop")), text)
        self.assertIn("try:", self.lines_with(text, "mac-mcp connect --client claude-desktop")[0] + "try:")

    def test_ctrl_c_while_the_updater_runs_says_it_keeps_running(self) -> None:
        import tempfile
        ready = _update_info(update_available=True, behind_by=1, target_commit="bbbbbbbb2222")
        out = FakeTTY()

        class Proc:
            def wait(self):
                raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as temp:
            log_path = cli.Path(temp) / "upd_1.log"
            with patch.object(cli, "install_kind", return_value="git"), patch.object(cli, "validate_update_state"), \
                 patch.object(cli, "resolve_update_paths", return_value=(cli.Path("/r"), cli.Path("/rt"))), \
                 patch.object(cli, "check_update", return_value=ready), \
                 patch.object(cli, "secure_bootstrap_update_blocker", return_value=None), \
                 patch.object(cli, "launch_detached_update", return_value=({"updater_pid": 77, "log_path": str(log_path)}, Proc())), \
                 redirect_stdout(out):
                code = cli.update(argparse.Namespace(check=False, json=False, repo=None, runtime=None, branch="main",
                                                     remote="origin", skip_restart=False, skip_deps=False))
        text = " ".join(visible(out.getvalue()).split())
        self.assertEqual(130, code)
        self.assertIn("The update keeps running in the background (pid 77)", text)
        self.assertIn("try: mac-mcp logs update", text)
        self.assertNotIn("Traceback", text)

    def privacy(self, *, running, result, start_ok=True, stores=("memory", "backups")):
        from mcp_server import privacy_inventory
        plan = [{"key": "memory", "name": "Memories", "exists": True, "bytes": 1, "paths": ["/s/memory"]},
                {"key": "backups", "name": "Backups", "exists": True, "bytes": 1, "paths": ["/s/backups"]}]
        out = FakeTTY()
        with patch.object(privacy_inventory, "delete_plan", return_value=plan), \
             patch.object(privacy_inventory, "trash_stores", return_value=result), \
             patch.object(cli, "_resolve_server_identity", return_value=(4321, "pid_record") if running else (None, "not_running")), \
             patch.object(cli, "STATE_DIR", cli.DEFAULT_STATE_DIR), patch.object(cli, "_stop_plain", return_value=0), \
             patch.object(cli, "_start_again_as_before", return_value=start_ok), redirect_stdout(out):
            code = cli.privacy_delete(argparse.Namespace(all=False, store=list(stores), yes=True, json=False))
        return code, re.sub(r"\d+\.\d+s", "0.0s", visible(out.getvalue()))

    def test_privacy_counts_stores_and_names_them_with_the_trash_folder(self) -> None:
        moved = [{"store": "memory", "path": "/s/memory", "to": "/T/m"}] + [
            {"store": "backups", "path": f"/s/backups/b{n}", "to": f"/T/b{n}"} for n in range(6)]
        code, text = self.privacy(running=False, result={"folder": "/Users/x/.Trash/Mac MCP data 20261016", "moved": moved, "failed": []})
        flat = " ".join(text.split())
        self.assertEqual(0, code)
        self.assertIn("✓ Moved 2 stores to the Trash", text)
        self.assertIn("✓ Moved to the Trash: Memories, Backups", flat)
        self.assertIn("Mac MCP data 20261016", flat)
        self.assertNotIn("/s/backups/b3", flat)

    def test_privacy_restart_failure_is_a_warning_with_the_next_command(self) -> None:
        moved = [{"store": "memory", "path": "/s/memory", "to": "/T/m"}]
        code, text = self.privacy(running=True, result={"folder": "/T", "moved": moved, "failed": []}, start_ok=False)
        flat = " ".join(text.split())
        self.assertEqual(0, code)  # as the plain path: the move worked
        self.assertIn("✗ Start Mac MCP again", text)
        self.assertIn("⚠ Moved to the Trash, but Mac MCP did not start again — try: mac-mcp start", flat)

    def test_privacy_not_running_summary_says_what_moved(self) -> None:
        moved = [{"store": "memory", "path": "/s/memory", "to": "/T/m"}]
        code, text = self.privacy(running=False, result={"folder": "/T", "moved": moved, "failed": []}, stores=("memory",))
        self.assertIn("✓ Moved to the Trash: Memories", " ".join(text.split()))

    def test_an_empty_log_says_so(self) -> None:
        import tempfile
        out = FakeTTY()
        with tempfile.TemporaryDirectory() as temp:
            log = cli.Path(temp) / "s.log"
            log.write_text("")
            with patch.object(cli, "managed_logs", return_value={"server": log}), redirect_stdout(out):
                code = cli.logs(argparse.Namespace(component="server", lines=5, list=False))
        text = visible(out.getvalue())
        self.assertEqual(0, code)
        self.assertNotIn("\n\n\n", text)
        self.assertIn("✓ The server log is empty.", text)

    def test_logs_and_connect_config_lines_fit_40_columns(self) -> None:
        import tempfile
        out = FakeTTY()
        with patch.dict(os.environ, {"COLUMNS": "40"}), tempfile.TemporaryDirectory() as temp:
            log = cli.Path(temp) / "s.log"
            log.write_text("a\n")
            with patch.object(cli, "managed_logs", return_value={"server": log}), patch.object(cli, "update_logs_dir", return_value=cli.Path(temp)), \
                 patch.object(cli, "log_limits", return_value={"max_bytes": 10 * 1024 * 1024, "backups": 3, "update_logs_kept": 20}), \
                 redirect_stdout(out):
                cli.logs(argparse.Namespace(component="server", lines=5, list=True))
        long = [line for line in visible(out.getvalue()).split("\n") if len(line) > 40 and "Kept" in line]
        self.assertEqual([], long)
        from mcp_server.connection_config import render_connection_config
        rendered = render_connection_config(client="claude-code", endpoint_url="http://127.0.0.1:8765/mcp", auth_required=True,
                                            server_name="mac-mcp", auth_env="MAC_MCP_API_KEY")
        out = FakeTTY()
        cli_views.render_connect_config(Style.for_stream(out, env={**TTY_ENV, "COLUMNS": "60"}), rendered, "local",
                                        "http://127.0.0.1:8765/mcp", None, True)
        copy = [line for line in visible(out.getvalue()).split("\n") if "Copy into" in line or line.startswith("    ") and "config" in line]
        self.assertTrue(all(len(line) <= 60 for line in copy), copy)

    def test_a_failed_updates_log_keeps_its_indentation(self) -> None:
        from mcp_server.cli_steps import Steps
        out = FakeTTY()
        steps = Steps(Style.for_stream(out, env=TTY_ENV))
        with steps.step("Installed", failed_label="Install") as state:
            print("Traceback (most recent call last):\n  File \"x.py\", line 1\nValueError: bad")
            state.ok = False
        text = visible(out.getvalue())
        self.assertIn("      Traceback (most recent call last):", text)
        self.assertIn("        File \"x.py\", line 1", text)


if __name__ == "__main__":
    unittest.main()
