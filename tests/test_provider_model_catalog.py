from __future__ import annotations

import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

import mcp_server.tools_agents as agents


class ProviderModelCatalogTests(unittest.TestCase):
    def _paths(self, root: Path) -> tuple[Path, Path]:
        config = root / "config.toml"
        cache = root / "models_cache.json"
        return config, cache

    def _fake_binary(self, root: Path) -> str:
        binary = root / "codex"
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)
        return str(binary)

    def _write_cache(self, path: Path, *, fetched_at: str, models: list[dict]) -> None:
        path.write_text(json.dumps({
            "client_version": "test",
            "fetched_at": fetched_at,
            "models": models,
        }), encoding="utf-8")

    def _luna(self) -> dict:
        return {
            "slug": "gpt-6-luna",
            "display_name": "GPT-6-Luna",
            "visibility": "list",
            "priority": 4,
            "default_reasoning_level": "medium",
            "supported_reasoning_levels": [
                {"effort": "low", "description": "Fast"},
                {"effort": "medium", "description": "Balanced"},
                {"effort": "high", "description": "Deep"},
                {"effort": "xhigh", "description": "Extra deep"},
                {"effort": "max", "description": "Maximum"},
            ],
        }

    def test_codex_catalog_prefers_models_cache_and_exposes_luna_max(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config, cache = self._paths(root)
            config.write_text(
                'model = "gpt-6.1-sol"\nmodel_reasoning_effort = "high"\n'
                '[tui.model_availability_nux]\n"legacy-only" = true\n',
                encoding="utf-8",
            )
            self._write_cache(
                cache,
                fetched_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                models=[self._luna()],
            )
            with patch.object(agents, "_codex_config_path", return_value=config), \
                 patch.object(agents, "_codex_models_cache_path", return_value=cache):
                catalog = agents._codex_model_catalog()

            self.assertEqual(["gpt-6-luna"], catalog["models"])
            self.assertEqual("gpt-6.1-sol", catalog["default_model"])
            self.assertEqual("codex_models_cache", catalog["source"])
            self.assertEqual("fresh", catalog["freshness"])
            luna = catalog["model_items"][0]
            self.assertEqual("GPT-6-Luna", luna["display_name"])
            self.assertIn("max", luna["reasoning_values"])
            self.assertNotIn("legacy-only", catalog["models"])

    def test_codex_agent_catalog_returns_structured_reasoning_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config, cache = self._paths(root)
            config.write_text('model = "gpt-6-luna"\n', encoding="utf-8")
            self._write_cache(
                cache,
                fetched_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                models=[self._luna()],
            )
            with patch.object(agents, "_codex_config_path", return_value=config), \
                 patch.object(agents, "_codex_models_cache_path", return_value=cache), \
                 patch.object(agents, "provider_enabled", side_effect=lambda p: p == "codex"), \
                 patch.object(agents, "_find_binary", return_value="/tmp/codex"), \
                 patch.object(agents, "_version", return_value="codex-cli test"):
                provider = agents.agent_catalog(None, provider="codex")["providers"]["codex"]

            self.assertEqual(["gpt-6-luna"], provider["models"])
            self.assertEqual("fresh", provider["catalog_freshness"])
            self.assertEqual("codex_models_cache", provider["catalog_source"])
            self.assertIn("max", provider["reasoning_values"])
            self.assertIn("max", provider["model_items"][0]["reasoning_values"])

    def test_explicit_codex_model_requires_fresh_authoritative_cache(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config, cache = self._paths(root)
            config.write_text('model = "gpt-6-luna"\n', encoding="utf-8")
            stale = datetime.now(timezone.utc) - timedelta(days=2)
            self._write_cache(
                cache,
                fetched_at=stale.isoformat().replace("+00:00", "Z"),
                models=[self._luna()],
            )
            with patch.object(agents, "_codex_config_path", return_value=config), \
                 patch.object(agents, "_codex_models_cache_path", return_value=cache):
                with self.assertRaises(HTTPException) as ctx:
                    agents._validate_provider_model_selection(
                        "codex", self._fake_binary(root), "gpt-6-luna", "max"
                    )

            self.assertEqual(409, ctx.exception.status_code)
            self.assertIn("provider_catalog_stale", str(ctx.exception.detail))

    def test_codex_reasoning_is_validated_per_model(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config, cache = self._paths(root)
            config.write_text('model = "gpt-6-luna"\n', encoding="utf-8")
            self._write_cache(
                cache,
                fetched_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                models=[self._luna()],
            )
            with patch.object(agents, "_codex_config_path", return_value=config), \
                 patch.object(agents, "_codex_models_cache_path", return_value=cache):
                binary = self._fake_binary(root)
                valid = agents._validate_provider_model_selection(
                    "codex", binary, "gpt-6-luna", "max"
                )
                self.assertTrue(valid["validated"])
                with self.assertRaises(HTTPException) as ctx:
                    agents._validate_provider_model_selection(
                        "codex", binary, "gpt-6-luna", "ultra"
                    )

            self.assertEqual(400, ctx.exception.status_code)
            self.assertIn("unsupported_reasoning", str(ctx.exception.detail))

    def test_missing_or_malformed_cache_does_not_reinvent_config_nux_as_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config, cache = self._paths(root)
            config.write_text(
                'model = "gpt-6-luna"\n[tui.model_availability_nux]\n"old-model" = true\n',
                encoding="utf-8",
            )
            cache.write_text("{broken", encoding="utf-8")
            with patch.object(agents, "_codex_config_path", return_value=config), \
                 patch.object(agents, "_codex_models_cache_path", return_value=cache):
                catalog = agents._codex_model_catalog()

            self.assertEqual([], catalog["models"])
            self.assertEqual("gpt-6-luna", catalog["default_model"])
            self.assertEqual("config_default", catalog["source"])
            self.assertEqual("unavailable", catalog["freshness"])
            self.assertIn("models_cache_unreadable", catalog["error"])

    def test_codex_provider_event_records_effective_model_mismatch(self) -> None:
        meta = {
            "provider": "codex",
            "status": "running",
            "phase": "starting",
            "requested_model": "gpt-6-luna",
            "model": "gpt-6-luna",
            "model_mismatch": False,
            "started_at": time.time(),
            "last_activity_at": time.time(),
            "step_count": 0,
            "tool_call_count": 0,
        }
        agents._apply_provider_event(
            meta,
            {
                "type": "thread.started",
                "thread_id": "thread-1",
                "model": "gpt-6.1-sol",
                "reasoning_effort": "max",
            },
            time.time(),
        )
        self.assertEqual("gpt-6.1-sol", meta["effective_model"])
        self.assertEqual("max", meta["effective_reasoning"])
        self.assertTrue(meta["model_mismatch"])
        self.assertIn("requested gpt-6-luna", meta["note"])


if __name__ == "__main__":
    unittest.main()
