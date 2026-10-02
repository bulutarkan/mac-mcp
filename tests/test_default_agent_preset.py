from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

import mcp_server.tools_agents as agents


class DefaultAgentPresetTests(unittest.TestCase):
    def _settings_path(
        self,
        root: Path,
        *,
        default: dict | None = None,
        codex_enabled: bool = True,
        chatgpt_enabled: bool = True,
        opencode_enabled: bool = True,
    ) -> Path:
        path = root / "settings.json"
        subagents: dict = {
            "providers": {
                "codex": {"enabled": codex_enabled},
                "chatgpt": {"enabled": chatgpt_enabled},
                "opencode": {"enabled": opencode_enabled},
            }
        }
        if default is not None:
            subagents["default"] = default
        path.write_text(json.dumps({"subagents": subagents}), encoding="utf-8")
        return path

    def _env(self, path: Path):
        return patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(path)}, clear=False)

    def test_omitted_provider_uses_saved_tuple(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = self._settings_path(
                Path(td),
                default={"provider": "codex", "model": "gpt-6-luna", "reasoning": "max"},
            )
            with self._env(path),                  patch.object(agents, "_find_binary", return_value="/usr/bin/true"),                  patch.object(
                     agents,
                     "_validate_provider_model_selection",
                     return_value={"validated": True, "catalog_source": "test"},
                 ) as validate:
                resolved = agents._resolve_agent_selection(None, None, None)

        self.assertEqual("codex", resolved["provider"])
        self.assertEqual("gpt-6-luna", resolved["model"])
        self.assertEqual("max", resolved["reasoning"])
        self.assertEqual("default", resolved["provider_source"])
        self.assertEqual("default", resolved["model_source"])
        self.assertEqual("default", resolved["reasoning_source"])
        validate.assert_called_once_with("codex", "/usr/bin/true", "gpt-6-luna", "max")

    def test_explicit_same_provider_model_overrides_but_reasoning_can_inherit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = self._settings_path(
                Path(td),
                default={"provider": "codex", "model": "gpt-6-luna", "reasoning": "max"},
            )
            with self._env(path),                  patch.object(agents, "_find_binary", return_value="/usr/bin/true"),                  patch.object(
                     agents,
                     "_validate_provider_model_selection",
                     return_value={"validated": True},
                 ) as validate:
                resolved = agents._resolve_agent_selection("codex", "gpt-6-sol", None)

        self.assertEqual("codex", resolved["provider"])
        self.assertEqual("gpt-6-sol", resolved["model"])
        self.assertEqual("max", resolved["reasoning"])
        self.assertEqual("explicit", resolved["model_source"])
        self.assertEqual("default", resolved["reasoning_source"])
        validate.assert_called_once_with("codex", "/usr/bin/true", "gpt-6-sol", "max")

    def test_explicit_different_provider_never_inherits_saved_codex_model_or_reasoning(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = self._settings_path(
                Path(td),
                default={"provider": "codex", "model": "gpt-6-luna", "reasoning": "max"},
            )
            with self._env(path):
                resolved = agents._resolve_agent_selection("chatgpt", None, None)

        self.assertEqual("chatgpt", resolved["provider"])
        self.assertIsNone(resolved["model"])
        self.assertIsNone(resolved["reasoning"])
        self.assertEqual("provider_default", resolved["model_source"])
        self.assertEqual("provider_default", resolved["reasoning_source"])

    def test_explicit_reasoning_wins_over_saved_reasoning(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = self._settings_path(
                Path(td),
                default={"provider": "codex", "model": "gpt-6-luna", "reasoning": "max"},
            )
            with self._env(path),                  patch.object(agents, "_find_binary", return_value="/usr/bin/true"),                  patch.object(
                     agents,
                     "_validate_provider_model_selection",
                     return_value={"validated": True},
                 ) as validate:
                resolved = agents._resolve_agent_selection(None, None, "high")

        self.assertEqual("codex", resolved["provider"])
        self.assertEqual("gpt-6-luna", resolved["model"])
        self.assertEqual("high", resolved["reasoning"])
        self.assertEqual("explicit", resolved["reasoning_source"])
        validate.assert_called_once_with("codex", "/usr/bin/true", "gpt-6-luna", "high")

    def test_no_default_requires_explicit_provider(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = self._settings_path(Path(td), default=None)
            with self._env(path):
                explicit = agents._resolve_agent_selection("codex", None, None)
                with self.assertRaises(HTTPException) as ctx:
                    agents._resolve_agent_selection(None, None, None)

        self.assertEqual("codex", explicit["provider"])
        self.assertEqual(400, ctx.exception.status_code)
        self.assertIn("default_agent_not_configured", str(ctx.exception.detail))

    def test_disabled_saved_provider_fails_closed_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = self._settings_path(
                Path(td),
                default={"provider": "codex", "model": "gpt-6-luna", "reasoning": "max"},
                codex_enabled=False,
            )
            with self._env(path), self.assertRaises(HTTPException) as ctx:
                agents._resolve_agent_selection(None, None, None)

        self.assertEqual(409, ctx.exception.status_code)
        self.assertIn("default_agent_unavailable", str(ctx.exception.detail))
        self.assertIn("disabled", str(ctx.exception.detail))

    def test_missing_saved_provider_binary_fails_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = self._settings_path(
                Path(td),
                default={"provider": "codex", "model": "gpt-6-luna", "reasoning": "max"},
            )
            with self._env(path),                  patch.object(agents, "_find_binary", return_value=None),                  self.assertRaises(HTTPException) as ctx:
                agents._resolve_agent_selection(None, None, None)

        self.assertEqual(503, ctx.exception.status_code)
        self.assertIn("default_agent_unavailable", str(ctx.exception.detail))
        self.assertIn("CLI is unavailable", str(ctx.exception.detail))

    def test_invalid_saved_model_or_reasoning_is_reported_as_default_agent_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = self._settings_path(
                Path(td),
                default={"provider": "codex", "model": "removed-model", "reasoning": "max"},
            )
            with self._env(path),                  patch.object(agents, "_find_binary", return_value="/usr/bin/true"),                  patch.object(
                     agents,
                     "_validate_provider_model_selection",
                     side_effect=HTTPException(400, "unsupported_model: removed-model"),
                 ),                  self.assertRaises(HTTPException) as ctx:
                agents._resolve_agent_selection(None, None, None)

        self.assertEqual(409, ctx.exception.status_code)
        self.assertIn("default_agent_invalid", str(ctx.exception.detail))
        self.assertIn("unsupported_model", str(ctx.exception.detail))

    def test_corrupt_settings_make_implicit_default_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            path.write_text("{broken", encoding="utf-8")
            with self._env(path), self.assertRaises(HTTPException) as ctx:
                agents._resolve_agent_selection(None, None, None)

        self.assertEqual(409, ctx.exception.status_code)
        self.assertIn("default_agent_unavailable", str(ctx.exception.detail))
        self.assertIn("invalid_json", str(ctx.exception.detail))

    def test_single_spawn_consumes_resolved_default_without_touching_access_mode(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = self._settings_path(
                root,
                default={"provider": "codex", "model": "gpt-6-luna", "reasoning": "max"},
            )
            with self._env(path),                  patch.object(agents, "_find_binary", return_value="/usr/bin/true"),                  patch.object(
                     agents,
                     "_validate_provider_model_selection",
                     return_value={"validated": True},
                 ),                  patch.object(agents, "_spawn_internal", return_value={"ok": True}) as spawn:
                result = agents.spawn_agent(
                    None,
                    provider=None,
                    prompt="test",
                    cwd=str(root),
                    access_mode="full",
                    git_isolation="off",
                )

        self.assertTrue(result["ok"])
        args = spawn.call_args.args
        self.assertEqual("codex", args[1])
        self.assertEqual("gpt-6-luna", args[3])
        self.assertEqual("max", args[4])
        self.assertEqual("full", args[9])

    def test_team_resolves_saved_tuple_once_at_team_level(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = self._settings_path(
                root,
                default={"provider": "codex", "model": "gpt-6-luna", "reasoning": "max"},
            )
            teams = root / "teams"
            agents_dir = root / "agents"
            teams.mkdir()
            agents_dir.mkdir()

            def no_spawn_tick(team_id: str):
                return agents._read_team(team_id)

            with self._env(path),                  patch.object(agents, "TEAMS_DIR", teams),                  patch.object(agents, "AGENTS_DIR", agents_dir),                  patch.object(agents, "_find_binary", return_value="/usr/bin/true"),                  patch.object(
                     agents,
                     "_validate_provider_model_selection",
                     return_value={"validated": True},
                 ) as validate,                  patch.object(agents, "_team_tick", side_effect=no_spawn_tick):
                result = agents.spawn_agents(
                    None,
                    tasks=[{"id": "one", "prompt": "test"}],
                    provider=None,
                    cwd=str(root),
                    access_mode="full",
                    git_isolation="off",
                    retries=0,
                )

        self.assertEqual("codex", result["provider"])
        self.assertEqual("gpt-6-luna", result["model"])
        self.assertEqual("max", result["reasoning"])
        self.assertEqual(1, validate.call_count)


if __name__ == "__main__":
    unittest.main()
