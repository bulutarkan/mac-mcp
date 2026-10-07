from __future__ import annotations

import io
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from mcp_server import cli, diagnostics, update_helper
from mcp_server.cli_bootstrap import ensure_cli_launcher, launcher_kind


class CliErgonomicsTests(unittest.TestCase):
    def _fake_runtime(self, root: Path, label: str = "runtime") -> Path:
        runtime = root / label
        entry = runtime / ".venv" / "bin" / "mac-mcp"
        entry.parent.mkdir(parents=True)
        entry.write_text("#!/bin/sh\nprintf 'delegated:%s\\n' \"$*\"\n", encoding="utf-8")
        os.chmod(entry, 0o755)
        return runtime

    def test_launcher_is_regular_executable_and_works_without_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mac-mcp-cli-launcher-") as td:
            root = Path(td)
            runtime = self._fake_runtime(root)
            launcher = root / "home" / ".local" / "bin" / "mac-mcp"
            created = ensure_cli_launcher(runtime, launcher, strict=True)
            self.assertEqual(launcher, created)
            self.assertFalse(launcher.is_symlink())
            self.assertEqual("file", launcher_kind(launcher))
            self.assertTrue(os.access(launcher, os.X_OK))
            proc = subprocess.run(
                [str(launcher), "--version"],
                env={"HOME": str(root / "home"), "PATH": "/usr/bin:/bin"},
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(0, proc.returncode)
            self.assertEqual("delegated:--version", proc.stdout.strip())

    def test_launcher_runtime_override_survives_runtime_move(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mac-mcp-cli-move-") as td:
            root = Path(td)
            old_runtime = self._fake_runtime(root, "old-runtime")
            new_runtime = self._fake_runtime(root, "new-runtime")
            launcher = root / "bin" / "mac-mcp"
            ensure_cli_launcher(old_runtime, launcher, strict=True)
            subprocess.run(["/bin/rm", "-rf", str(old_runtime)], check=True)
            proc = subprocess.run(
                [str(launcher), "status"],
                env={"HOME": str(root), "PATH": "/usr/bin:/bin", "MAC_MCP_RUNTIME_DIR": str(new_runtime)},
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(0, proc.returncode)
            self.assertEqual("delegated:status", proc.stdout.strip())

    def test_cli_version_flag_is_script_friendly(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            cli.main(["--version"])
        self.assertEqual(0, ctx.exception.code)
        self.assertEqual("mac-mcp 2.1.8", out.getvalue().strip())

    def test_update_wording_matches_verified_stable_checkpoint_model(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            cli.main(["--help"])
        self.assertEqual(0, ctx.exception.code)
        top_help = " ".join(out.getvalue().split())
        self.assertIn(
            "Update Mac MCP to the latest verified stable release checkpoint.",
            top_help,
        )
        self.assertNotIn("latest commit on a Git branch", top_help)

        helper_help = " ".join(update_helper._build_parser().format_help().split())
        self.assertIn(
            "Update Mac MCP to the latest verified stable release checkpoint.",
            helper_help,
        )
        self.assertNotIn("latest commit on a Git branch", helper_help)

        info = update_helper.UpdateInfo(
            repo="/tmp/repo",
            runtime="/tmp/runtime",
            branch="main",
            remote="origin",
            deployed_commit="a" * 40,
            repo_commit="b" * 40,
            target_commit="c" * 40,
            behind_by=2,
            update_available=True,
            dirty=False,
            release_verified=True,
            release_id="stable-test",
            release_version="2.1.8",
        )
        status_text = update_helper.format_check(info)
        self.assertIn("Verified stable commit: cccccccc", status_text)
        self.assertNotIn("Latest commit:", status_text)

        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
        self.assertIn("newest cryptographically verified stable release checkpoint", readme)
        self.assertIn("not arbitrary repository HEAD", readme)

    def test_doctor_reports_absolute_invocation_when_cli_not_on_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mac-mcp-cli-doctor-") as td:
            root = Path(td)
            runtime = self._fake_runtime(root)
            launcher = root / "home" / ".local" / "bin" / "mac-mcp"
            ensure_cli_launcher(runtime, launcher, strict=True)
            with patch.object(diagnostics, "default_cli_path", return_value=launcher), \
                    patch.object(diagnostics, "runtime_entrypoint", return_value=runtime / ".venv" / "bin" / "mac-mcp"), \
                    patch.object(diagnostics.shutil, "which", return_value=None), \
                    patch.object(diagnostics, "read_deployed_commit", return_value="a" * 40):
                row = diagnostics._check_cli_installation().to_dict()
        self.assertEqual("warn", row["status"])
        self.assertEqual("CLI_NOT_ON_PATH", row["reason_code"])
        self.assertEqual(str(launcher), row["details"]["absolute_invocation"])
        self.assertIn(str(launcher), row["remediation"])

    def test_doctor_fails_broken_cli_symlink(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mac-mcp-cli-broken-") as td:
            root = Path(td)
            launcher = root / "bin" / "mac-mcp"
            launcher.parent.mkdir(parents=True)
            launcher.symlink_to(root / "missing" / "mac-mcp")
            runtime = self._fake_runtime(root)
            with patch.object(diagnostics, "default_cli_path", return_value=launcher), \
                    patch.object(diagnostics, "runtime_entrypoint", return_value=runtime / ".venv" / "bin" / "mac-mcp"), \
                    patch.object(diagnostics.shutil, "which", return_value=None):
                row = diagnostics._check_cli_installation().to_dict()
        self.assertEqual("fail", row["status"])
        self.assertEqual("CLI_BROKEN_SYMLINK", row["reason_code"])

    def test_fastmcp_forward_reference_warning_is_resolved_before_app_creation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mac-mcp-warning-") as td:
            env = dict(os.environ)
            env.update({
                "HOME": td,
                "MAC_MCP_HOME": td,
                "MAC_MCP_STATE_DIR": str(Path(td) / ".mac-mcp"),
                "MAC_MCP_SKIP_MENU_APP_INSTALL": "1",
                "MAC_MCP_SKIP_MENU_APP": "1",
                "MCP_ALLOW_NO_AUTH": "true",
                "MAC_MCP_HOST": "127.0.0.1",
                "MAC_MCP_PUBLIC_ENDPOINT_MODE": "none",
            })
            code = (
                "import warnings; "
                "from pydantic_settings.exceptions import IncompleteFieldDefinitionWarning; "
                "warnings.simplefilter('error', IncompleteFieldDefinitionWarning); "
                "import mcp_server.main; "
                "print('OK')"
            )
            proc = subprocess.run(
                [sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                env=env, capture_output=True, text=True, check=False,
            )
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual("OK", proc.stdout.strip())
        self.assertNotIn("IncompleteFieldDefinitionWarning", proc.stderr)

    def test_installer_uses_launcher_bootstrap_and_documents_absolute_path(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "install.sh").read_text(encoding="utf-8")
        self.assertIn("from mcp_server.cli_bootstrap import ensure_cli_launcher", source)
        self.assertNotIn('/bin/ln -s "$RUNTIME_DIR/.venv/bin/mac-mcp" "$CLI_PATH"', source)
        self.assertIn("Noninteractive shells can always use the absolute CLI path", source)
        self.assertIn('"$CLI_PATH" --version', source)


if __name__ == "__main__":
    unittest.main()
