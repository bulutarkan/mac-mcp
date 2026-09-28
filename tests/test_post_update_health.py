from __future__ import annotations

import asyncio
import importlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from starlette.testclient import TestClient

import mcp_server.post_update_health as health
from mcp_server.version import __version__


class PostUpdatePendingDetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        health._PENDING_CACHE = None
        health._GATE_TARGET = None
        health._GATE_TASK = None
        health._GATE_RESULT = None

    def test_starting_state_marks_update_pending(self) -> None:
        target = "b" * 40
        with patch.object(health, "_read_update_state", return_value={
            "status": "starting",
            "from_commit": "a" * 40,
            "to_commit": target,
        }), patch.object(health, "read_deployed_commit", return_value="a" * 40):
            context = health.pending_update_context(Path("/tmp/runtime"))
        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(target, context["target_commit"])
        self.assertEqual("update_state:starting", context["source"])

    def test_rolling_back_state_does_not_trigger_deep_gate(self) -> None:
        with patch.object(health, "_read_update_state", return_value={
            "status": "rolling_back",
            "from_commit": "a" * 40,
            "to_commit": "b" * 40,
        }), patch.object(health, "read_deployed_commit", return_value="a" * 40),                 patch.object(health, "_signed_repo_mismatch_context", return_value=None):
            self.assertIsNone(health.pending_update_context(Path("/tmp/runtime")))

    def test_signed_repo_mismatch_supports_direct_cli_first_transition(self) -> None:
        target = "c" * 40
        with tempfile.TemporaryDirectory(prefix="mac-mcp-health-pending-") as td:
            repo = Path(td) / "repo"
            (repo / ".git").mkdir(parents=True)
            verified = SimpleNamespace(
                release_id="stable-test",
                version=__version__,
                target_commit=target,
            )
            with patch.object(health, "_read_update_state", return_value={}),                     patch.object(health, "_repo_root", return_value=repo),                     patch.object(health, "read_deployed_commit", return_value="a" * 40),                     patch.object(health, "_git", return_value=target),                     patch.object(health.release_trust, "verify_release_commit", return_value=verified):
                context = health.pending_update_context(Path(td) / "runtime")
            self.assertIsNotNone(context)
            assert context is not None
            self.assertEqual("signed_repo_mismatch", context["source"])
            self.assertEqual(target, context["target_commit"])


class PostUpdateHealthGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        health._GATE_TARGET = None
        health._GATE_TASK = None
        health._GATE_RESULT = None

    def _fixture(self, root: Path) -> tuple[Path, Path]:
        app = root / "Applications" / "Mac MCP.app"
        (app / "Contents" / "MacOS").mkdir(parents=True)
        (app / "Contents" / "MacOS" / "MacMCPMenu").write_text("menu\n", encoding="utf-8")
        (app / "Contents" / "PlugIns" / "Safari.appex").mkdir(parents=True)
        runtime = root / "runtime"
        chrome = runtime / "menu_app" / "ChromeVisualCompanion"
        chrome.mkdir(parents=True)
        for name in ("manifest.json", "background.js", "bridge_config.js"):
            (chrome / name).write_text("{}\n", encoding="utf-8")
        return app, runtime

    async def _run_gate(
        self,
        root: Path,
        *,
        mcp_ok: bool = True,
        mcp_names: list[str] | None = None,
        public_mode: str = "none",
        public_ok: bool = True,
        codesign_ok: bool = True,
        menu_pids: list[int] | None = None,
        allow_shell: bool = True,
    ) -> dict:
        app, runtime = self._fixture(root)
        target = "d" * 40
        verified = SimpleNamespace(
            release_id="stable-test",
            version=__version__,
            target_commit=target,
        )
        settings = SimpleNamespace(
            allow_no_auth=False,
            api_key="test-key-not-secret",
            allow_shell=allow_shell,
        )
        names = mcp_names if mcp_names is not None else ["read_file", "run_command"]
        context = {
            "target_commit": target,
            "release_id": "stable-test",
            "release_version": __version__,
            "source": "test",
            "repo": str(root / "repo"),
        }
        public_url = None if public_mode == "none" else "https://example.test/health?probe=basic"
        resolver = SimpleNamespace(path="/usr/bin/true", source="test")
        with patch.object(health, "_git", return_value=target),                 patch.object(health.release_trust, "verify_release_commit", return_value=verified),                 patch.object(health, "_auth_smoke", new=AsyncMock(return_value=(True, 401, None))),                 patch.object(health, "_mcp_smoke", new=AsyncMock(return_value=(
                    mcp_ok,
                    {"protocol_version": "2025-11-25", "tool_count": len(names), "tool_names": names} if mcp_ok else {},
                    None if mcp_ok else "RuntimeError",
                ))),                 patch.object(health, "permission_profile_name", return_value="trusted"),                 patch.object(health, "tool_availability", return_value={"available": True}),                 patch.object(health, "_menu_app_path", return_value=app),                 patch.object(health, "_codesign_ok", return_value=codesign_ok),                 patch.object(health, "_menu_process_pids", return_value=[123] if menu_pids is None else menu_pids),                 patch.object(health, "_runtime_root", return_value=runtime),                 patch.object(health, "_public_basic_health_url", return_value=(public_url, public_mode, "test")),                 patch.object(health, "resolve_cloudflared_binary", return_value=resolver),                 patch.object(health, "resolve_ngrok_binary", return_value=resolver),                 patch.object(health, "_public_endpoint_smoke", new=AsyncMock(return_value=(
                    public_ok,
                    200 if public_ok else 503,
                    None if public_ok else "ConnectError",
                ))),                 patch.object(health, "_write_health_gate_report"):
            return await health.run_post_update_health_gate(settings, context, local_base_url="http://127.0.0.1:8877")

    async def test_healthy_gate_passes_all_critical_checks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mac-mcp-health-pass-") as td:
            report = await self._run_gate(Path(td))
        self.assertTrue(report["ok"])
        self.assertEqual([], report["critical_failures"])
        self.assertEqual("passed", report["status"])

    async def test_mcp_handshake_failure_is_critical(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mac-mcp-health-mcp-") as td:
            report = await self._run_gate(Path(td), mcp_ok=False, allow_shell=False)
        self.assertFalse(report["ok"])
        self.assertIn("mcp.initialize_tools", report["critical_failures"])

    async def test_public_endpoint_failure_is_critical_when_selected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mac-mcp-health-public-") as td:
            report = await self._run_gate(Path(td), public_mode="cloudflare", public_ok=False)
        self.assertFalse(report["ok"])
        self.assertIn("public.health", report["critical_failures"])

    async def test_menu_app_codesign_failure_is_critical(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mac-mcp-health-menu-") as td:
            report = await self._run_gate(Path(td), codesign_ok=False)
        self.assertFalse(report["ok"])
        self.assertIn("menu_app.codesign", report["critical_failures"])

    async def test_background_gate_returns_pending_before_completion(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        expected = {
            "ok": True,
            "status": "passed",
            "target_commit": "e" * 40,
            "critical_failures": [],
            "warnings": [],
        }

        async def fake_gate(*_args, **_kwargs):
            started.set()
            await release.wait()
            return expected

        context = {"target_commit": "e" * 40}
        settings = SimpleNamespace()
        with patch.object(health, "run_post_update_health_gate", side_effect=fake_gate):
            first = await health.get_or_start_post_update_health_gate(settings, context)
            self.assertIsNone(first)
            await started.wait()
            second = await health.get_or_start_post_update_health_gate(settings, context)
            self.assertIsNone(second)
            release.set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            final = await health.get_or_start_post_update_health_gate(settings, context)
        self.assertEqual(expected, final)


class UpdateHealthEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.main = importlib.import_module("mcp_server.main")
        self.client = TestClient(self.main.app)
        self.context = {"target_commit": "f" * 40, "source": "test"}

    def test_basic_probe_bypasses_deep_gate(self) -> None:
        gate = AsyncMock(return_value=None)
        with patch.object(self.main, "pending_update_context", return_value=self.context), \
                patch.object(self.main, "get_or_start_post_update_health_gate", new=gate):
            response = self.client.get("/health?probe=basic")
        self.assertEqual(200, response.status_code)
        self.assertTrue(response.json()["ok"])
        gate.assert_not_awaited()

    def test_pending_gate_returns_fast_503(self) -> None:
        with patch.object(self.main, "pending_update_context", return_value=self.context), \
                patch.object(
                    self.main,
                    "get_or_start_post_update_health_gate",
                    new=AsyncMock(return_value=None),
                ):
            response = self.client.get("/health")
        self.assertEqual(503, response.status_code)
        self.assertEqual("running", response.json()["update_gate"])

    def test_completed_gate_controls_health_status(self) -> None:
        passed = {"ok": True, "status": "passed", "target_commit": "f" * 40}
        failed = {"ok": False, "status": "failed", "target_commit": "f" * 40}
        with patch.object(self.main, "pending_update_context", return_value=self.context), \
                patch.object(
                    self.main,
                    "get_or_start_post_update_health_gate",
                    new=AsyncMock(return_value=passed),
                ):
            ok_response = self.client.get("/health")
        self.assertEqual(200, ok_response.status_code)
        self.assertTrue(ok_response.json()["ok"])

        with patch.object(self.main, "pending_update_context", return_value=self.context), \
                patch.object(
                    self.main,
                    "get_or_start_post_update_health_gate",
                    new=AsyncMock(return_value=failed),
                ):
            fail_response = self.client.get("/health")
        self.assertEqual(503, fail_response.status_code)
        self.assertFalse(fail_response.json()["ok"])
        self.assertEqual("failed", fail_response.json()["update_gate"])


if __name__ == "__main__":
    unittest.main()
