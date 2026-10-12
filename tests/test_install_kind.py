from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp_server import install_kind as ik


class InstallKindTests(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory(prefix="mac-mcp-install-kind-")
        self.state = Path(self.td.name)
        self.env = patch.dict(os.environ, {"MAC_MCP_STATE_DIR": str(self.state)})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.td.cleanup()

    def test_source_when_marker_missing_or_other(self) -> None:
        self.assertEqual("source", ik.install_kind())
        (self.state / "install-kind").write_text("something-else\n")
        self.assertEqual("source", ik.install_kind())

    def test_pkg_when_marker_says_pkg(self) -> None:
        (self.state / "install-kind").write_text("pkg\n")
        self.assertEqual("pkg", ik.install_kind())

    def test_payload_points_to_releases(self) -> None:
        payload = ik.package_update_payload()
        self.assertEqual("pkg", payload["install_kind"])
        self.assertEqual(ik.RELEASES_URL, payload["download_url"])
        self.assertFalse(payload["update_available"])
        self.assertIn(".pkg", payload["message"])

    def test_cli_update_check_json_for_pkg_install(self) -> None:
        (self.state / "install-kind").write_text("pkg\n")
        from mcp_server import cli
        args = SimpleNamespace(check=True, json=True, repo=None, runtime=None, branch="main", remote="origin",
                               skip_restart=False, skip_deps=False)
        out = io.StringIO()
        with patch("mcp_server.cli.check_update") as check, redirect_stdout(out):
            rc = cli.update(args)
        check.assert_not_called()
        self.assertEqual(0, rc)
        self.assertEqual("pkg", json.loads(out.getvalue())["install_kind"])

    def test_cli_update_install_for_pkg_install_is_refused_with_guidance(self) -> None:
        (self.state / "install-kind").write_text("pkg\n")
        from mcp_server import cli
        args = SimpleNamespace(check=False, json=False, repo=None, runtime=None, branch="main", remote="origin",
                               skip_restart=False, skip_deps=False)
        out = io.StringIO()
        with patch("mcp_server.cli.launch_detached_update") as launch, redirect_stdout(out):
            rc = cli.update(args)
        launch.assert_not_called()
        self.assertEqual(3, rc)
        self.assertIn(ik.RELEASES_URL, out.getvalue())

    def test_update_tool_for_pkg_install(self) -> None:
        (self.state / "install-kind").write_text("pkg\n")
        from mcp_server import tools_update
        with patch("mcp_server.tools_update.check_update") as check:
            payload = tools_update.mac_mcp_update(check_only=False)
        check.assert_not_called()
        self.assertEqual("pkg", payload["install_kind"])
        self.assertFalse(payload["check_only"])

    def test_doctor_reports_install_kind(self) -> None:
        (self.state / "install-kind").write_text("pkg\n")
        from mcp_server import diagnostics
        row = diagnostics._check_cli_installation().to_dict()
        self.assertEqual("pkg", row["details"]["install_kind"])


if __name__ == "__main__":
    unittest.main()
