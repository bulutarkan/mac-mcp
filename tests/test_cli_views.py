"""The CLI's themed screens."""
from __future__ import annotations

import argparse
import io
import re
import os
import unittest
from contextlib import redirect_stdout
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
        lines = [line for line in text.split("\n") if line.startswith("    ") and line.strip()]
        columns = {len(line) - len(line.lstrip(" ").split(" ", 1)[1].lstrip(" ")) for line in lines}
        self.assertEqual(1, len(columns), lines)  # connect-config does not push its text out
        self.assertIn("● Stopped", text.split("\n")[0])

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


if __name__ == "__main__":
    unittest.main()
