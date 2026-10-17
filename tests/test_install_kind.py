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

        # No test reaches GitHub; each one that needs a release answer sets it.
        self.fetch = patch.object(ik, "_fetch_latest_release", side_effect=OSError("offline in tests"))
        self.fetch.start()

    def tearDown(self) -> None:
        self.fetch.stop()
        self.env.stop()
        self.td.cleanup()

    def answer(self, tag: str, assets=("Mac-MCP-2.2.0-stable-2.2.0-r3-arm64.pkg",), installed="stable-2.2.0-r2"):
        release = {"tag_name": tag, "html_url": f"{ik.RELEASES_URL}/tag/{tag}",
                   "assets": [{"name": name} for name in assets]}
        return patch.object(ik, "_fetch_latest_release", return_value=release), \
            patch.object(ik, "installed_release_id", return_value=installed)

    def test_source_when_marker_missing_or_other(self) -> None:
        self.assertEqual("source", ik.install_kind())
        (self.state / "install-kind").write_text("something-else\n")
        self.assertEqual("source", ik.install_kind())

    def test_pkg_when_marker_says_pkg(self) -> None:
        (self.state / "install-kind").write_text("pkg\n")
        self.assertEqual("pkg", ik.install_kind())

    def test_payload_points_to_releases(self) -> None:
        with patch.object(ik, "installed_release_id", return_value=""):
            payload = ik.package_update_payload()
        self.assertEqual("pkg", payload["install_kind"])
        self.assertEqual(ik.RELEASES_URL, payload["download_url"])
        self.assertFalse(payload["update_available"])
        self.assertIn(".pkg", payload["message"])

    def test_a_newer_release_with_a_package_is_an_update(self) -> None:
        fetch, installed = self.answer("v2.2.0-r3")
        with fetch, installed:
            payload = ik.package_update_payload()
        self.assertTrue(payload["update_available"])
        self.assertEqual("v2.2.0-r3", payload["latest_release"])
        self.assertEqual(f"{ik.RELEASES_URL}/tag/v2.2.0-r3", payload["download_url"])
        self.assertIn("A new update is ready", payload["message"])

    def test_versions_compare_by_number_and_a_bare_tag_is_r1(self) -> None:
        for tag, installed, expected in [("v2.10.0", "stable-2.9.9-r12", True), ("v2.3.0", "stable-2.2.0-r9", True),
                                         ("v2.2.0", "stable-2.2.0-r1", False), ("v2.2.0-r2", "stable-2.2.0-r2", False),
                                         ("v2.2.0-r1", "stable-2.2.0-r2", False)]:
            fetch, current = self.answer(tag, installed=installed)
            with self.subTest(tag=tag, installed=installed), fetch, current:
                self.assertIs(expected, ik.package_update_payload()["update_available"])

    def test_a_release_without_a_package_or_an_odd_tag_is_not_an_update(self) -> None:
        for tag, assets in [("v2.3.0", ("source.tar.gz",)), ("nightly", ("x.pkg",)), ("v2.3.0-beta", ("x.pkg",))]:
            fetch, installed = self.answer(tag, assets=assets)
            with self.subTest(tag=tag), fetch, installed:
                payload = ik.package_update_payload()
            self.assertFalse(payload["update_available"])
            self.assertEqual(ik.RELEASES_URL, payload["download_url"])

    def test_only_this_repositorys_release_pages_are_opened(self) -> None:
        release = {"tag_name": "v9.0.0", "html_url": "https://example.com/evil", "assets": [{"name": "a.pkg"}]}
        with patch.object(ik, "_fetch_latest_release", return_value=release), \
                patch.object(ik, "installed_release_id", return_value="stable-2.2.0-r2"):
            payload = ik.package_update_payload()
        self.assertTrue(payload["update_available"])
        self.assertEqual(ik.RELEASES_URL, payload["download_url"])

    def test_no_network_is_said_not_hidden(self) -> None:
        with patch.object(ik, "installed_release_id", return_value="stable-2.2.0-r2"):
            payload = ik.package_update_payload()
        self.assertTrue(payload["check_failed"])
        self.assertFalse(payload["update_available"])

    def test_the_installed_release_comes_from_the_signed_manifest(self) -> None:
        manifest = json.loads((Path(ik.__file__).resolve().parents[1] / "release/stable-manifest.json").read_text())
        self.assertEqual(manifest["release_id"], ik.installed_release_id())

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
