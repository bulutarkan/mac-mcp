from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp_server.dashboard_routes import create_dashboard_routes
from mcp_server import runtime_settings
from mcp_server.observability import TelemetryManager
from mcp_server.security import load_settings

import mcp_server.provider_usage as provider_usage
import mcp_server.tools_agents as agents


# Usage metering reads ~/.mac-mcp/settings.json; never let the owner's real
# privacy settings decide these results.
_SETTINGS_DIR = tempfile.TemporaryDirectory(prefix="mac-mcp-usage-settings-")
_SETTINGS_ENV = patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(Path(_SETTINGS_DIR.name) / "settings.json")})


def setUpModule() -> None:
    _SETTINGS_ENV.start()
    runtime_settings._usage_privacy_cache.update({"at": -1.0, "value": None})


def tearDownModule() -> None:
    _SETTINGS_ENV.stop()
    runtime_settings._usage_privacy_cache.update({"at": -1.0, "value": None})
    _SETTINGS_DIR.cleanup()


class ProviderUsageNormalizationTests(unittest.TestCase):
    def test_codex_native_breakdown_maps_without_double_counting_subsets(self) -> None:
        row = provider_usage.normalize_codex_usage({
            "input_tokens": 100,
            "cached_input_tokens": 40,
            "output_tokens": 12,
            "reasoning_output_tokens": 3,
            "cache_write_input_tokens": 7,
        })
        self.assertTrue(row.recognized)
        self.assertEqual(100, row.input_tokens)
        self.assertEqual(40, row.cache_read_tokens)
        self.assertEqual(7, row.cache_write_tokens)
        self.assertEqual(12, row.output_tokens)
        self.assertEqual(3, row.reasoning_tokens)
        self.assertEqual(112, row.total_tokens)

    def test_opencode_118_full_schema_is_lossless(self) -> None:
        row = provider_usage.normalize_opencode_usage({
            "input": 1257,
            "output": 129,
            "reasoning": 21,
            "cache": {"read": 125440, "write": 0},
            "total": 126847,
        })
        self.assertTrue(row.recognized)
        self.assertFalse(row.total_only)
        self.assertEqual(1257, row.input_tokens)
        self.assertEqual(129, row.output_tokens)
        self.assertEqual(21, row.reasoning_tokens)
        self.assertEqual(125440, row.cache_read_tokens)
        self.assertEqual(0, row.cache_write_tokens)
        self.assertEqual(126847, row.total_tokens)

    def test_opencode_total_only_does_not_invent_breakdown(self) -> None:
        row = provider_usage.normalize_opencode_usage({"total": 20})
        self.assertTrue(row.recognized)
        self.assertTrue(row.total_only)
        self.assertEqual(20, row.total_tokens)
        self.assertIsNone(row.input_tokens)
        self.assertIsNone(row.output_tokens)
        self.assertIsNone(row.reasoning_tokens)
        self.assertIsNone(row.cache_read_tokens)
        self.assertIsNone(row.cache_write_tokens)

    def test_unknown_schema_fails_closed(self) -> None:
        row = provider_usage.normalize_opencode_usage({"future_usage": 999})
        self.assertFalse(row.recognized)
        self.assertIsNone(row.total_tokens)


class ProviderUsageStoreTests(unittest.TestCase):
    def record(
        self,
        *,
        key: str,
        provider: str = "codex",
        session: str = "session-1",
        event_id: str = "turn:1",
        agent_id: str | None = "agt_1",
        model: str | None = None,
        verified: bool = False,
        timestamp: float | None = None,
        input_tokens: int | None = 100,
        output_tokens: int | None = 12,
        reasoning_tokens: int | None = 3,
        cache_read_tokens: int | None = 40,
        cache_write_tokens: int | None = 0,
        total_tokens: int | None = 112,
    ) -> provider_usage.UsageRecord:
        return provider_usage.UsageRecord(
            event_key=key,
            provider=provider,
            session_id=session,
            event_id=event_id,
            timestamp=time.time() if timestamp is None else timestamp,
            agent_id=agent_id,
            model=model,
            model_verified=verified,
            source=provider_usage.SOURCE_REPORT,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            total_tokens=total_tokens,
            requested_model=model if verified else None,
            effective_model=None,
        )

    def test_legacy_schema_adds_requested_and_effective_model_columns(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "provider.sqlite3"
            with sqlite3.connect(path) as conn:
                conn.execute(
                    """
                    CREATE TABLE provider_usage_events (
                        event_key TEXT PRIMARY KEY,
                        provider TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        event_id TEXT NOT NULL,
                        timestamp REAL NOT NULL,
                        local_date TEXT NOT NULL,
                        timezone TEXT NOT NULL,
                        agent_id TEXT,
                        model TEXT,
                        model_verified INTEGER NOT NULL DEFAULT 0,
                        source TEXT NOT NULL,
                        schema_version INTEGER NOT NULL,
                        provider_version TEXT,
                        input_tokens INTEGER,
                        output_tokens INTEGER,
                        reasoning_tokens INTEGER,
                        cache_read_tokens INTEGER,
                        cache_write_tokens INTEGER,
                        total_tokens INTEGER
                    )
                    """
                )
                conn.commit()
            provider_usage.ProviderUsageStore(path)
            with sqlite3.connect(path) as conn:
                columns = [
                    row[1]
                    for row in conn.execute(
                        "PRAGMA table_info(provider_usage_events)"
                    )
                ]
            self.assertIn("requested_model", columns)
            self.assertIn("effective_model", columns)

    def test_duplicate_event_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = provider_usage.ProviderUsageStore(Path(td) / "provider.sqlite3")
            row = self.record(key="codex:s:turn:1")
            self.assertTrue(store.ingest(row))
            self.assertFalse(store.ingest(row))
            codex = store.summary(days=1)["providers"]["codex"]
            self.assertEqual(1, codex["turns"])
            self.assertEqual(112, codex["total_tokens"])
            self.assertEqual(1, store.summary(days=1)["diagnostics"]["duplicate_events"])

    def test_total_only_makes_partial_breakdown_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = provider_usage.ProviderUsageStore(Path(td) / "provider.sqlite3")
            self.assertTrue(store.ingest(self.record(
                key="opencode:s:p1", provider="opencode", event_id="p1",
                input_tokens=5, output_tokens=3, reasoning_tokens=1,
                cache_read_tokens=10, cache_write_tokens=0, total_tokens=19,
            )))
            self.assertTrue(store.ingest(self.record(
                key="opencode:s:p2", provider="opencode", event_id="p2",
                input_tokens=None, output_tokens=None, reasoning_tokens=None,
                cache_read_tokens=None, cache_write_tokens=None, total_tokens=20,
            )))
            row = store.summary(days=1)["providers"]["opencode"]
            self.assertEqual(2, row["turns"])
            self.assertEqual(39, row["total_tokens"])
            self.assertIsNone(row["input_tokens"])
            self.assertEqual(1, row["input_known_turns"])

    def test_unverified_model_is_not_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "provider.sqlite3"
            store = provider_usage.ProviderUsageStore(path)
            store.ingest(self.record(
                key="codex:s:t1", model="unattested", verified=False,
            ))
            self.assertEqual([], store.summary(days=1)["providers"]["codex"]["models"])
            with sqlite3.connect(path) as conn:
                self.assertEqual(
                    (None, 0),
                    conn.execute(
                        "SELECT model, model_verified FROM provider_usage_events"
                    ).fetchone(),
                )
                self.assertEqual(
                    (None, None),
                    conn.execute(
                        "SELECT requested_model, effective_model FROM provider_usage_events"
                    ).fetchone(),
                )

    def test_verified_model_drilldown_is_exact(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = provider_usage.ProviderUsageStore(Path(td) / "provider.sqlite3")
            store.ingest(self.record(
                key="codex:s:t1", model="gpt-6-luna", verified=True,
            ))
            model = store.summary(days=1)["providers"]["codex"]["models"][0]
            self.assertEqual("gpt-6-luna", model["model"])
            self.assertEqual(112, model["total_tokens"])

    def test_period_filter_and_aggregate_survive_agent_meta_prune(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = provider_usage.ProviderUsageStore(Path(td) / "provider.sqlite3")
            now = time.time()
            store.ingest(self.record(
                key="codex:s:t1", timestamp=now - 10 * 86400, total_tokens=1000,
                input_tokens=900, output_tokens=100,
            ))
            store.ingest(self.record(
                key="codex:s:t2", event_id="turn:2",
                timestamp=now - 100 * 86400, total_tokens=2000,
                input_tokens=1800, output_tokens=200,
            ))
            self.assertEqual(
                1000, store.summary(days=30)["providers"]["codex"]["total_tokens"]
            )
            self.assertEqual(
                3000, store.summary(days=365)["providers"]["codex"]["total_tokens"]
            )
            agent_dir = Path(td) / "agents" / "agt_1"
            agent_dir.mkdir(parents=True)
            (agent_dir / "meta.json").write_text('{"provider":"codex"}')
            (agent_dir / "stdout.log").write_text("provider raw fixture")
            for child in agent_dir.iterdir():
                child.unlink()
            agent_dir.rmdir()
            # Aggregate is independent of agent meta/stdout retention after ingest.
            self.assertEqual(2, store.summary(days=365)["providers"]["codex"]["turns"])

    def test_retention_prunes_usage_older_than_four_hundred_days(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "provider.sqlite3"
            store = provider_usage.ProviderUsageStore(path)
            store.ingest(self.record(
                key="codex:old:t1",
                timestamp=time.time() - 450 * 86400,
            ))
            store.ingest(self.record(
                key="codex:recent:t2",
                event_id="turn:2",
                timestamp=time.time() - 10 * 86400,
            ))
            with sqlite3.connect(path) as conn:
                self.assertEqual(
                    1,
                    conn.execute("SELECT COUNT(*) FROM provider_usage_events").fetchone()[0],
                )
                self.assertEqual(
                    1,
                    conn.execute("SELECT SUM(turns) FROM provider_usage_daily").fetchone()[0],
                )

    def test_mixed_provider_parallel_ingest_is_exact(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = provider_usage.ProviderUsageStore(Path(td) / "provider.sqlite3")
            results: list[bool] = []
            result_lock = threading.Lock()

            def worker(provider: str, start: int) -> None:
                local: list[bool] = []
                for index in range(start, start + 50):
                    local.append(store.ingest(self.record(
                        key=f"{provider}:s:{index}",
                        provider=provider,
                        session="s",
                        event_id=str(index),
                        total_tokens=10,
                    )))
                with result_lock:
                    results.extend(local)

            threads = [
                threading.Thread(target=worker, args=("codex", 0)),
                threading.Thread(target=worker, args=("opencode", 100)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(100, sum(1 for value in results if value))
            summary = store.summary(days=1)
            self.assertEqual(50, summary["providers"]["codex"]["turns"])
            self.assertEqual(500, summary["providers"]["codex"]["total_tokens"])
            self.assertEqual(50, summary["providers"]["opencode"]["turns"])
            self.assertEqual(500, summary["providers"]["opencode"]["total_tokens"])

    def test_daily_aggregate_tracks_unique_agents(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "provider.sqlite3"
            store = provider_usage.ProviderUsageStore(path)
            store.ingest(self.record(key="codex:s:t1", agent_id="agt_a"))
            store.ingest(self.record(
                key="codex:s:t2", event_id="turn:2", agent_id="agt_a"
            ))
            store.ingest(self.record(
                key="codex:s:t3", event_id="turn:3", agent_id="agt_b"
            ))
            with sqlite3.connect(path) as conn:
                agents, turns = conn.execute(
                    "SELECT agents, turns FROM provider_usage_daily WHERE provider='codex'"
                ).fetchone()
            self.assertEqual(2, agents)
            self.assertEqual(3, turns)
            self.assertEqual(2, store.summary(days=1)["providers"]["codex"]["agents"])

    def test_db_has_no_prompt_result_or_stdout_fields(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "provider.sqlite3"
            store = provider_usage.ProviderUsageStore(path)
            store.ingest(self.record(key="codex:s:t1"))
            with sqlite3.connect(path) as conn:
                columns = [
                    row[1] for row in conn.execute(
                        "PRAGMA table_info(provider_usage_events)"
                    )
                ]
            forbidden = [
                column for column in columns
                if any(marker in column for marker in (
                    "prompt", "result", "content", "stdout", "stderr", "secret"
                ))
            ]
            self.assertEqual([], forbidden)

    def test_chatgpt_is_unavailable_not_estimated(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            summary = provider_usage.ProviderUsageStore(
                Path(td) / "provider.sqlite3"
            ).summary(days=1)
            row = summary["providers"]["chatgpt"]
            self.assertFalse(row["available"])
            self.assertIsNone(row["total_tokens"])
            self.assertIsNone(row["source"])
            self.assertIn("No reliable provider-native tokenizer", summary["fallback_policy"])


class ProviderUsageAgentIntegrationTests(unittest.TestCase):
    def meta(self, provider: str, *, session: str | None = None) -> dict:
        return {
            "provider": provider,
            "status": "running",
            "phase": "provider_starting",
            "started_at": time.time(),
            "last_activity_at": time.time(),
            "step_count": 0,
            "tool_call_count": 0,
            "session_id": session,
            "resume_session_id": None,
            "requested_model": "gpt-6-luna",
            "model": "gpt-6-luna",
            "effective_model": None,
            "model_selection_verified": False,
            "attempt": 1,
            "retry_count": 0,
        }

    def write_agent(self, root: Path, agent_id: str, meta: dict) -> None:
        path = root / agent_id
        path.mkdir(parents=True)
        (path / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

    def summary(self, state_root: Path, provider: str) -> dict:
        return provider_usage.ProviderUsageStore(
            state_root / "state" / "provider_usage.sqlite3"
        ).summary(days=1)["providers"][provider]

    def test_codex_full_event_replay_does_not_double_count(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agents_root = root / "agents"
            state_root = root / "state"
            agent_id = "agt_codex_replay"
            self.write_agent(agents_root, agent_id, self.meta("codex"))
            completed = {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 40,
                    "output_tokens": 12,
                    "reasoning_output_tokens": 3,
                    "cache_write_input_tokens": 7,
                },
            }
            with patch.object(agents, "AGENTS_DIR", agents_root), patch.dict(
                os.environ, {"MAC_MCP_STATE_DIR": str(state_root)}
            ):
                sequence = [
                    {"type": "thread.started", "thread_id": "thread-replay"},
                    {"type": "turn.started"},
                    completed,
                ]
                for event in sequence:
                    agents._record_provider_event(agent_id, json.dumps(event))
                # Replay the same provider attempt's turn-start/completion.
                agents._record_provider_event(
                    agent_id, json.dumps({"type": "turn.started"})
                )
                agents._record_provider_event(agent_id, json.dumps(completed))
                row = self.summary(state_root, "codex")
                self.assertEqual(1, row["turns"])
                self.assertEqual(112, row["total_tokens"])

    def test_codex_new_retry_attempt_counts_real_new_consumption(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agents_root = root / "agents"
            state_root = root / "state"
            agent_id = "agt_codex_retry"
            self.write_agent(agents_root, agent_id, self.meta("codex"))
            completed = {
                "type": "turn.completed",
                "usage": {"input_tokens": 10, "output_tokens": 2},
            }
            with patch.object(agents, "AGENTS_DIR", agents_root), patch.dict(
                os.environ, {"MAC_MCP_STATE_DIR": str(state_root)}
            ):
                agents._record_provider_event(
                    agent_id,
                    json.dumps({"type": "thread.started", "thread_id": "thread-retry"}),
                )
                agents._record_provider_event(agent_id, json.dumps({"type": "turn.started"}))
                agents._record_provider_event(agent_id, json.dumps(completed))

                def prepare_retry(meta: dict) -> None:
                    agents._prepare_codex_usage_attempt(meta, agent_id, 1)
                    meta["retry_count"] = 1

                agents._update_meta(agent_id, prepare_retry)
                agents._record_provider_event(agent_id, json.dumps({"type": "turn.started"}))
                agents._record_provider_event(agent_id, json.dumps(completed))
                row = self.summary(state_root, "codex")
                self.assertEqual(2, row["turns"])
                self.assertEqual(24, row["total_tokens"])

    def test_codex_resume_same_session_gets_new_ordinal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agents_root = root / "agents"
            state_root = root / "state"
            parent = "agt_parent"
            child = "agt_resume"
            self.write_agent(agents_root, parent, self.meta("codex"))
            child_meta = self.meta("codex")
            child_meta["resume_session_id"] = "thread-resume"
            self.write_agent(agents_root, child, child_meta)
            completed = {
                "type": "turn.completed",
                "usage": {"input_tokens": 10, "output_tokens": 2},
            }
            with patch.object(agents, "AGENTS_DIR", agents_root), patch.dict(
                os.environ, {"MAC_MCP_STATE_DIR": str(state_root)}
            ):
                for agent_id in (parent, child):
                    agents._record_provider_event(
                        agent_id,
                        json.dumps({
                            "type": "thread.started",
                            "thread_id": "thread-resume",
                        }),
                    )
                    agents._record_provider_event(
                        agent_id, json.dumps({"type": "turn.started"})
                    )
                    agents._record_provider_event(agent_id, json.dumps(completed))
                row = self.summary(state_root, "codex")
                self.assertEqual(2, row["turns"])
                self.assertEqual(24, row["total_tokens"])

    def test_provider_effective_model_is_recorded_as_attested(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agents_root = root / "agents"
            state_root = root / "state"
            agent_id = "agt_effective_model"
            meta = self.meta("opencode")
            meta["effective_model"] = "provider-attested-model"
            self.write_agent(agents_root, agent_id, meta)
            event = {
                "type": "step_finish",
                "sessionID": "ses_effective",
                "part": {
                    "id": "prt_effective",
                    "sessionID": "ses_effective",
                    "tokens": {
                        "input": 1, "output": 1, "reasoning": 0,
                        "cache": {"read": 0, "write": 0}, "total": 2,
                    },
                },
            }
            with patch.object(agents, "AGENTS_DIR", agents_root), patch.dict(
                os.environ, {"MAC_MCP_STATE_DIR": str(state_root)}
            ):
                agents._record_provider_event(agent_id, json.dumps(event))
                db = state_root / "state" / "provider_usage.sqlite3"
                with sqlite3.connect(db) as conn:
                    row = conn.execute(
                        "SELECT requested_model, effective_model, model, model_verified "
                        "FROM provider_usage_events"
                    ).fetchone()
                self.assertEqual(
                    (None, "provider-attested-model", "provider-attested-model", 1),
                    row,
                )

    def test_opencode_part_identity_dedupes_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agents_root = root / "agents"
            state_root = root / "state"
            agent_id = "agt_opencode"
            self.write_agent(agents_root, agent_id, self.meta("opencode"))
            event = {
                "type": "step_finish",
                "sessionID": "ses_open",
                "timestamp": int(time.time() * 1000),
                "part": {
                    "id": "prt_1",
                    "messageID": "msg_1",
                    "sessionID": "ses_open",
                    "reason": "tool-calls",
                    "tokens": {
                        "input": 5,
                        "output": 3,
                        "reasoning": 1,
                        "cache": {"read": 10, "write": 0},
                        "total": 19,
                    },
                },
            }
            second = json.loads(json.dumps(event))
            second["part"]["id"] = "prt_2"
            second["part"]["messageID"] = "msg_2"
            second["part"]["tokens"]["input"] = 6
            second["part"]["tokens"]["total"] = 20
            with patch.object(agents, "AGENTS_DIR", agents_root), patch.dict(
                os.environ, {"MAC_MCP_STATE_DIR": str(state_root)}
            ):
                agents._record_provider_event(agent_id, json.dumps(event))
                agents._record_provider_event(agent_id, json.dumps(event))
                agents._record_provider_event(agent_id, json.dumps(second))
                row = self.summary(state_root, "opencode")
                self.assertEqual(2, row["turns"])
                self.assertEqual(11, row["input_tokens"])
                self.assertEqual(39, row["total_tokens"])

    def test_unknown_codex_schema_is_diagnostic_and_closes_turn(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agents_root = root / "agents"
            state_root = root / "state"
            agent_id = "agt_unknown_codex"
            self.write_agent(agents_root, agent_id, self.meta("codex"))
            with patch.object(agents, "AGENTS_DIR", agents_root), patch.dict(
                os.environ, {"MAC_MCP_STATE_DIR": str(state_root)}
            ):
                agents._record_provider_event(
                    agent_id,
                    json.dumps({"type": "thread.started", "thread_id": "thread-future"}),
                )
                agents._record_provider_event(agent_id, json.dumps({"type": "turn.started"}))
                agents._record_provider_event(
                    agent_id,
                    json.dumps({
                        "type": "turn.completed",
                        "usage": {"future_usage_metric": 999},
                    }),
                )
                summary = provider_usage.ProviderUsageStore(
                    state_root / "state" / "provider_usage.sqlite3"
                ).summary(days=1)
                self.assertNotIn("codex", summary["providers"])
                self.assertEqual(
                    1, summary["diagnostics"]["unknown_usage_schema_codex"]
                )
                meta = agents._read_meta(agent_id)
                self.assertFalse(meta["provider_usage_turn_open"])

    def test_unknown_opencode_schema_increments_diagnostic_without_fake_total(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agents_root = root / "agents"
            state_root = root / "state"
            agent_id = "agt_unknown"
            self.write_agent(agents_root, agent_id, self.meta("opencode"))
            event = {
                "type": "step_finish",
                "sessionID": "ses_unknown",
                "part": {
                    "id": "prt_future",
                    "sessionID": "ses_unknown",
                    "tokens": {"future_metric": 999},
                },
            }
            with patch.object(agents, "AGENTS_DIR", agents_root), patch.dict(
                os.environ, {"MAC_MCP_STATE_DIR": str(state_root)}
            ):
                agents._record_provider_event(agent_id, json.dumps(event))
                summary = provider_usage.ProviderUsageStore(
                    state_root / "state" / "provider_usage.sqlite3"
                ).summary(days=1)
                self.assertNotIn("opencode", summary["providers"])
                self.assertEqual(
                    1, summary["diagnostics"]["unknown_usage_schema_opencode"]
                )

    def test_attested_requested_model_is_used_but_unattested_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agents_root = root / "agents"
            state_root = root / "state"
            verified = "agt_verified"
            unknown = "agt_unknown_model"
            verified_meta = self.meta("opencode")
            verified_meta["model_selection_verified"] = True
            unknown_meta = self.meta("opencode")
            self.write_agent(agents_root, verified, verified_meta)
            self.write_agent(agents_root, unknown, unknown_meta)

            def event(part_id: str) -> dict:
                return {
                    "type": "step_finish",
                    "sessionID": "ses_model",
                    "part": {
                        "id": part_id,
                        "sessionID": "ses_model",
                        "tokens": {
                            "input": 1, "output": 1, "reasoning": 0,
                            "cache": {"read": 0, "write": 0}, "total": 2,
                        },
                    },
                }

            with patch.object(agents, "AGENTS_DIR", agents_root), patch.dict(
                os.environ, {"MAC_MCP_STATE_DIR": str(state_root)}
            ):
                agents._record_provider_event(verified, json.dumps(event("p1")))
                agents._record_provider_event(unknown, json.dumps(event("p2")))
                models = self.summary(state_root, "opencode")["models"]
                self.assertEqual(["gpt-6-luna"], [row["model"] for row in models])


class ProviderUsageDashboardTests(unittest.TestCase):
    def test_endpoint_requires_auth_and_honors_period(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            telemetry = TelemetryManager(
                db_path=root / "telemetry.sqlite3",
                usage_enabled=False,
            )
            store = provider_usage.ProviderUsageStore(root / "provider.sqlite3")
            now = time.time()
            store.ingest(provider_usage.UsageRecord(
                event_key="codex:route:recent",
                provider="codex",
                session_id="route",
                event_id="recent",
                timestamp=now - 10 * 86400,
                agent_id="agt_route",
                model=None,
                model_verified=False,
                source=provider_usage.SOURCE_REPORT,
                input_tokens=900,
                output_tokens=100,
                reasoning_tokens=20,
                cache_read_tokens=400,
                cache_write_tokens=0,
                total_tokens=1000,
            ))
            store.ingest(provider_usage.UsageRecord(
                event_key="codex:route:old",
                provider="codex",
                session_id="route",
                event_id="old",
                timestamp=now - 100 * 86400,
                agent_id="agt_route",
                model=None,
                model_verified=False,
                source=provider_usage.SOURCE_REPORT,
                input_tokens=1800,
                output_tokens=200,
                reasoning_tokens=30,
                cache_read_tokens=800,
                cache_write_tokens=0,
                total_tokens=2000,
            ))
            token = "provider-usage-dashboard-token-0123456789"
            with patch(
                "mcp_server.dashboard_routes.provider_usage_summary",
                side_effect=lambda **kwargs: store.summary(**kwargs),
            ):
                app = Starlette(routes=create_dashboard_routes(
                    telemetry, load_settings(), token
                ))
                client = TestClient(app)
                self.assertEqual(
                    401, client.get("/dashboard/api/provider-usage").status_code
                )
                recent = client.get(
                    "/dashboard/api/provider-usage?days=30",
                    headers={"authorization": "Bearer " + token},
                )
                annual = client.get(
                    "/dashboard/api/provider-usage?days=365",
                    headers={"authorization": "Bearer " + token},
                )
                self.assertEqual(200, recent.status_code)
                self.assertEqual(1000, recent.json()["providers"]["codex"]["total_tokens"])
                self.assertEqual(3000, annual.json()["providers"]["codex"]["total_tokens"])
                self.assertNotIn("prompt", json.dumps(annual.json()).lower())


if __name__ == "__main__":
    unittest.main()
