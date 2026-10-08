from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

from mcp_server import decision_engine
from mcp_server.decision_engine import DecisionCandidate, DecisionConfig
from mcp_server.tools_browser_agent import _browser_act_locked

ENABLED = DecisionConfig(enabled=True, scope="browser")
CANDIDATES = [
    DecisionCandidate("c1", label="Reply", role="button", tag="button"),
    DecisionCandidate("c2", label="Reply All", role="button", tag="button"),
    DecisionCandidate("c3", label="Share", role="button", tag="button"),
]


def _client_factory(handler):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return lambda: client


def _answer(choice, confidence):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "answers": [{"type": "choice", "name": "target", "choice": choice, "confidence": confidence}],
        })
    return handler


def _reset_key_state(value=None, *, loaded=True):
    decision_engine._key_state.update({
        "value": value,
        "loaded_at": decision_engine.time.monotonic() if loaded else None,
        "refreshing": False,
        "invalid_fingerprint": None,
        "verified_fingerprint": None,
    })


class DecisionEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        env = patch.dict(os.environ, {"MAC_MCP_DECISIONS_OPENAI_API_KEY": "sk-test-decisions-key-123456"})
        env.start()
        self.addCleanup(env.stop)
        keychain = patch.object(decision_engine, "keychain_password", return_value=None)
        keychain.start()
        self.addCleanup(keychain.stop)
        self.warmup = patch.object(decision_engine, "_schedule_warmup").start()
        self.addCleanup(patch.stopall)
        _reset_key_state("sk-test-decisions-key-123456")
        self.addCleanup(_reset_key_state, None, loaded=False)

    def _resolve(self, handler, *, config=ENABLED, candidates=CANDIDATES, deterministic_id="c1"):
        with patch.object(decision_engine, "_client", _client_factory(handler)):
            return decision_engine.resolve_ambiguity(
                "Browser click target. query: Reply all", candidates,
                surface="browser", deterministic_id=deterministic_id, config=config,
            )

    def test_assess_ambiguity_matches_recovery_thresholds(self) -> None:
        self.assertTrue(decision_engine.assess_ambiguity([0.98, 0.95])["ambiguous"])
        self.assertFalse(decision_engine.assess_ambiguity([0.98, 0.70])["ambiguous"])
        self.assertFalse(decision_engine.assess_ambiguity([0.98])["ambiguous"])
        self.assertFalse(decision_engine.assess_ambiguity([0.55, 0.54])["ambiguous"])

    def test_config_is_disabled_by_default_and_fails_closed_on_invalid_values(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            with patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(path)}):
                self.assertFalse(decision_engine.load_decision_config().allows("browser"))
                path.write_text(json.dumps({"decision_acceleration": {"enabled": False}}))
                self.assertFalse(decision_engine.load_decision_config().allows("browser"))
                path.write_text(json.dumps({"decision_acceleration": {"enabled": True}}))
                config = decision_engine.load_decision_config()
                self.assertTrue(config.allows("browser"))
                self.assertTrue(config.allows("native"))
                path.write_text(json.dumps({"decision_acceleration": {"enabled": True, "scope": "browser"}}))
                self.assertFalse(decision_engine.load_decision_config().allows("native"))
                for bad in (
                    {"enabled": "yes", "scope": "browser"},
                    {"enabled": True, "scope": "everything"},
                    {"enabled": True, "scope": "both", "timeout_ms": 999999},
                    {"enabled": True, "scope": "both", "accept_threshold": 0.5, "agree_threshold": 0.8},
                ):
                    path.write_text(json.dumps({"decision_acceleration": bad}))
                    self.assertFalse(decision_engine.load_decision_config().enabled, bad)

    def test_disabled_config_never_calls_provider(self) -> None:
        handler = MagicMock(side_effect=AssertionError("network used"))
        result = self._resolve(handler, config=DecisionConfig())
        self.assertEqual("disabled", result.outcome)
        self.assertFalse(result.attempted)
        handler.assert_not_called()

    def test_missing_key_skips_provider(self) -> None:
        handler = MagicMock(side_effect=AssertionError("network used"))
        _reset_key_state(None)
        self.assertEqual("no_key", self._resolve(handler).outcome)
        handler.assert_not_called()

    def test_unloaded_key_never_blocks_and_falls_back(self) -> None:
        handler = MagicMock(side_effect=AssertionError("network used"))
        _reset_key_state(None, loaded=False)
        with patch.object(decision_engine.threading, "Thread") as thread:
            result = self._resolve(handler)
        self.assertEqual("key_pending", result.outcome)
        thread.return_value.start.assert_called_once()
        handler.assert_not_called()

    def test_settings_keychain_key_wins_over_env_and_generic_openai_key_is_ignored(self) -> None:
        with patch.object(decision_engine, "keychain_password", return_value="sk-from-settings"):
            self.assertEqual("sk-from-settings", decision_engine._read_api_key())
        with patch.dict(os.environ, {"MAC_MCP_DECISIONS_OPENAI_API_KEY": "", "OPENAI_API_KEY": "sk-unrelated"}):
            self.assertIsNone(decision_engine._read_api_key())

    def test_rejected_key_is_not_retried_until_it_changes(self) -> None:
        calls = []

        def unauthorized(request):
            calls.append(request)
            return httpx.Response(401, json={"error": "invalid_api_key"})

        self.assertEqual("invalid_key", self._resolve(unauthorized).outcome)
        self.assertEqual("invalid_key", self._resolve(unauthorized).outcome)
        self.assertEqual(1, len(calls))
        decision_engine._store_key("sk-new-key-after-fix")
        self.assertEqual("accepted", self._resolve(_answer("c2", 0.95)).outcome)

    def test_verify_reports_valid_invalid_and_missing_keys(self) -> None:
        with patch.object(decision_engine, "_client", _client_factory(_answer("c1", 0.99))):
            valid = decision_engine.verify_api_key()
        self.assertEqual(("valid", True), (valid["status"], valid["ok"]))
        self.assertEqual("valid", decision_engine.decision_status()["key_status"])
        with patch.object(decision_engine, "_client",
                          _client_factory(lambda r: httpx.Response(401, json={}))):
            invalid = decision_engine.verify_api_key()
        self.assertEqual("invalid", invalid["status"])
        self.assertEqual("invalid", decision_engine.decision_status()["key_status"])
        self.assertNotIn("sk-test", json.dumps(invalid))
        with patch.dict(os.environ, {"MAC_MCP_DECISIONS_OPENAI_API_KEY": ""}):
            self.assertEqual("missing", decision_engine.verify_api_key()["status"])

    def test_high_confidence_choice_is_accepted(self) -> None:
        result = self._resolve(_answer("c2", 0.95))
        self.assertEqual("accepted", result.outcome)
        self.assertEqual("c2", result.selected_id)

    def test_mid_confidence_requires_agreement_with_deterministic_rank(self) -> None:
        self.assertEqual("accepted", self._resolve(_answer("c2", 0.80)).outcome)
        self.assertEqual("low_confidence", self._resolve(_answer("c2", 0.79)).outcome)
        agreed = self._resolve(_answer("c1", 0.70))
        self.assertEqual("accepted_agreement", agreed.outcome)
        self.assertEqual("c1", agreed.selected_id)
        self.assertEqual("low_confidence", self._resolve(_answer("c1", 0.30)).outcome)

    def test_choice_outside_candidate_set_or_none_is_rejected(self) -> None:
        self.assertEqual("malformed", self._resolve(_answer("e_injected", 0.99)).outcome)
        self.assertEqual("no_match", self._resolve(_answer("none", 0.99)).outcome)

    def test_provider_failures_fall_back(self) -> None:
        def status(code):
            return lambda request: httpx.Response(code, json={"error": "x"})

        def timeout(request):
            raise httpx.ReadTimeout("slow", request=request)

        def bad_json(request):
            return httpx.Response(200, content=b"not-json")

        def refusal(request):
            return httpx.Response(200, json={"answers": [{"type": "refusal", "name": "target"}]})

        cases = {
            "rate_limited": status(429), "http_error": status(500), "timeout": timeout,
            "malformed": refusal,
        }
        for outcome, handler in cases.items():
            result = self._resolve(handler)
            self.assertEqual(outcome, result.outcome)
            self.assertIsNone(result.selected_id)
        self.warmup.assert_called_once()
        self.assertEqual("http_error", self._resolve(status(503)).outcome)
        self.assertEqual("http_error", self._resolve(bad_json).outcome)

    def test_risky_alternative_is_never_selected_over_deterministic_choice(self) -> None:
        candidates = [
            DecisionCandidate("c1", label="Cancel", role="button"),
            DecisionCandidate("c2", label="Delete account", role="button", risky=True),
        ]
        result = self._resolve(_answer("c2", 0.99), candidates=candidates)
        self.assertEqual("skipped_risky", result.outcome)
        self.assertIsNone(result.selected_id)

    def test_outbound_payload_is_minimal_and_redacted(self) -> None:
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = request.content.decode()
            seen["auth"] = request.headers.get("authorization")
            return _answer("c1", 0.99)(request)

        candidates = [
            DecisionCandidate("c1", label="Token ghp_abcdefghijklmnopqrstuvwxyz0123456789", role="textbox"),
            DecisionCandidate("c2", label="Name", role="textbox", context="x" * 500),
        ]
        self._resolve(handler, candidates=candidates)
        body = json.loads(seen["body"])
        self.assertNotIn("ghp_abcdefghijklmnopqrstuvwxyz0123456789", seen["body"])
        self.assertNotIn("sk-test-decisions-key", seen["body"])
        self.assertEqual("Bearer sk-test-decisions-key-123456", seen["auth"])
        values = [choice["value"] for choice in body["questions"][0]["choices"]]
        self.assertEqual(["c1", "c2", "none"], values)
        self.assertLess(len(body["questions"][0]["choices"][1]["description"]), 140)


class BrowserActDecisionTests(unittest.TestCase):
    def _run(self, matches, *, resolve_result=None):
        verified_calls = []

        def fake_verified(*args, **kwargs):
            action = args[2]
            verified_calls.append(action["element_id"])
            return {"ok": True, "type": action["type"], "element_id": action["element_id"], "_js_calls": 1}

        resolver = MagicMock(return_value=resolve_result or decision_engine.DecisionResult(outcome="disabled"))
        with patch("mcp_server.tools_browser_agent._resolve_tab_target", return_value=(1, 1)), \
                patch("mcp_server.tools_browser_agent.browser_find",
                      return_value={"ok": True, "best_match": matches[0], "matches": matches}) as find, \
                patch("mcp_server.tools_browser_agent._verified_dom_action", side_effect=fake_verified), \
                patch("mcp_server.tools_browser_agent._run_json_js", return_value={"ok": True}), \
                patch.object(decision_engine, "resolve_ambiguity", resolver), \
                patch.object(decision_engine, "is_risky_label", return_value=False):
            result = _browser_act_locked(
                MagicMock(), "Safari",
                [{"type": "click", "query": "Reply", "role": "button"}, {"type": "click", "query": "Next"}],
                window_index=1, tab_index=1, tab_handle="tab-1", return_state="none",
            )
        return result, verified_calls, resolver, find

    def test_unique_target_makes_no_decision_request(self) -> None:
        matches = [
            {"element_id": "e1", "text": "Reply", "role": "button", "tag": "button", "confidence": 0.99},
            {"element_id": "e2", "text": "Reply later", "role": "button", "tag": "button", "confidence": 0.70},
        ]
        result, verified, resolver, find = self._run(matches)
        self.assertTrue(result["ok"])
        self.assertEqual(["e1", "e1"], verified)
        resolver.assert_not_called()
        self.assertNotIn("decision", result["actions"][0]["resolved_target"])

    def test_ambiguous_target_keeps_deterministic_choice_when_decision_not_accepted(self) -> None:
        matches = [
            {"element_id": "e1", "text": "Reply", "role": "button", "tag": "button", "confidence": 0.98},
            {"element_id": "e2", "text": "Reply", "role": "button", "tag": "a", "confidence": 0.95},
        ]
        result, verified, resolver, _ = self._run(matches)
        self.assertEqual(["e1", "e1"], verified)
        self.assertEqual(2, resolver.call_count)
        decision = result["actions"][0]["resolved_target"]["decision"]
        self.assertTrue(decision["ambiguity"]["ambiguous"])
        self.assertEqual("disabled", decision["outcome"])

    def test_accepted_decision_executes_inside_same_batch(self) -> None:
        matches = [
            {"element_id": "e1", "text": "Reply", "role": "button", "tag": "button", "confidence": 0.98},
            {"element_id": "e2", "text": "Reply All", "role": "button", "tag": "button", "confidence": 0.95},
        ]
        accepted = decision_engine.DecisionResult(outcome="accepted", attempted=True, selected_id="c2", confidence=0.97)
        result, verified, resolver, find = self._run(matches, resolve_result=accepted)
        self.assertTrue(result["ok"])
        self.assertEqual(["e2", "e2"], verified)
        self.assertEqual(2, find.call_count)
        offered = [c.candidate_id for c in resolver.call_args_list[0].args[1]]
        self.assertEqual(["c1", "c2"], offered)
        self.assertEqual("e2", result["actions"][0]["resolved_target"]["element_id"])


class BrowserIntentHintTests(unittest.TestCase):
    MATCHES = [
        {"element_id": "e1", "text": "Continue", "role": "button", "tag": "button", "confidence": 0.98,
         "context": "Shipping"},
        {"element_id": "e2", "text": "Continue", "role": "button", "tag": "button", "confidence": 0.98,
         "context": "Billing"},
    ]

    def _decide(self, action, matches=None):
        resolver = MagicMock(return_value=decision_engine.DecisionResult(outcome="disabled"))
        with patch.object(decision_engine, "resolve_ambiguity", resolver), \
                patch.object(decision_engine, "is_risky_label", return_value=False):
            from mcp_server.tools_browser_agent import _decide_browser_target
            _decide_browser_target(action, "Continue", "button", None, matches or self.MATCHES)
        return resolver

    def test_without_intent_the_request_is_unchanged(self) -> None:
        resolver = self._decide({"type": "click", "query": "Continue"})
        self.assertEqual(
            "Browser click target. query: Continue; role: button; text: ", resolver.call_args.args[0],
        )

    def test_intent_is_appended_to_the_decision_input(self) -> None:
        resolver = self._decide({"type": "click", "query": "Continue", "intent": "Continue in the  Billing\nsection"})
        self.assertTrue(resolver.call_args.args[0].endswith("; intent: Continue in the Billing section"))

    def test_intent_is_redacted_capped_and_never_carries_typed_values(self) -> None:
        from mcp_server.tools_browser_agent import _action_intent_hint
        action = {
            "type": "type", "text": "4111 1111 1111 1111",
            "intent": "Card field 4111 1111 1111 1111 near token ghp_abcdefghijklmnopqrstuvwxyz0123456789 " + "x" * 300,
        }
        hint = _action_intent_hint(action)
        self.assertNotIn("4111", hint)
        self.assertIn("[typed value]", hint)
        self.assertNotIn("ghp_abcdefghijklmnopqrstuvwxyz0123456789", hint)
        self.assertLessEqual(len(hint), 120)
        self.assertEqual("", _action_intent_hint({"intent": {"not": "text"}}))

    def test_intent_never_triggers_a_request_without_ambiguity(self) -> None:
        unique = [dict(self.MATCHES[0]), {**self.MATCHES[1], "confidence": 0.60}]
        resolver = self._decide({"type": "click", "query": "Continue", "intent": "Billing"}, unique)
        resolver.assert_not_called()

    def test_disabled_layer_makes_no_outbound_call_even_with_intent(self) -> None:
        with patch.object(decision_engine, "_client") as client:
            result = decision_engine.resolve_ambiguity(
                "Browser click target. query: Continue; intent: Billing", [
                    DecisionCandidate("c1", label="Continue"), DecisionCandidate("c2", label="Continue"),
                ], surface="browser", deterministic_id="c1", config=DecisionConfig(),
            )
        self.assertEqual("disabled", result.outcome)
        client.assert_not_called()


class DecisionDashboardRouteTests(unittest.TestCase):
    def test_status_and_verify_routes_require_dashboard_auth_and_never_return_key(self) -> None:
        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        from mcp_server.dashboard_routes import create_dashboard_routes
        from mcp_server.observability import TelemetryManager
        from mcp_server.security import load_settings

        token = "decision-dashboard-test-token-0123456789"
        auth = {"authorization": f"Bearer {token}"}
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            settings_path = root / "settings.json"
            settings_path.write_text(json.dumps({"decision_acceleration": {"enabled": True}}))
            with patch.dict(os.environ, {
                "MAC_MCP_SETTINGS_PATH": str(settings_path),
                "MAC_MCP_DECISIONS_OPENAI_API_KEY": "sk-dashboard-test-key-123456",
            }), patch.object(decision_engine, "keychain_password", return_value=None), \
                    patch.object(decision_engine, "_client", _client_factory(_answer("c1", 0.99))):
                _reset_key_state(None, loaded=False)
                self.addCleanup(_reset_key_state, None, loaded=False)
                app = Starlette(routes=create_dashboard_routes(
                    TelemetryManager(db_path=root / "telemetry.sqlite3", max_events=10), load_settings(), token,
                ))
                client = TestClient(app)
                self.assertEqual(401, client.get("/dashboard/api/decision-acceleration").status_code)
                self.assertEqual(401, client.post("/dashboard/api/decision-acceleration/verify").status_code)

                status = client.get("/dashboard/api/decision-acceleration?reload=1", headers=auth).json()
                self.assertEqual((True, "both", "unverified"), (status["enabled"], status["scope"], status["key_status"]))
                verified = client.post("/dashboard/api/decision-acceleration/verify", headers=auth)
                self.assertEqual(200, verified.status_code)
                self.assertEqual(("valid", "valid"), (verified.json()["status"], verified.json()["key_status"]))
                self.assertNotIn("sk-dashboard-test-key", verified.text + json.dumps(status))


class NativeRecoveryDecisionTests(unittest.TestCase):
    def _run(self, *, config, resolve_result):
        import asyncio

        from mcp_server.computer_plan import execute_computer_plan

        button = {"parent_id": "w1", "role": "AXButton", "title": "Continue", "description": "Continue", "identifier": ""}
        observe_calls = 0
        acted = []

        async def caller(tool: str, args: dict):
            nonlocal observe_calls
            if tool == "mac_observe":
                observe_calls += 1
                nodes = [{"element_id": "w1", "role": "AXWindow", "title": "Demo"}, {"element_id": "w1/2", **button}]
                if observe_calls > 1:
                    nodes.append({"element_id": "w1/3", **button})
                return {"ok": True, "observation_id": f"obs{observe_calls}", "active_app": "DemoApp",
                        "window_index": 1, "nodes": nodes}
            if tool == "mac_act":
                element_id = args["actions"][0]["element_id"]
                acted.append(element_id)
                if len(acted) == 1:
                    return {"ok": False, "reason_code": "STALE_ELEMENT_PATH", "retryable": True, "observe_again": True,
                            "actions": [{"ok": False, "reason_code": "STALE_ELEMENT_PATH", "element_id": element_id}]}
                return {"ok": True, "actions": [{"ok": True, "element_id": element_id}]}
            raise AssertionError(tool)

        steps = [
            {"id": "obs", "tool": "mac_observe", "arguments": {"app": "DemoApp", "include_screenshot": False}},
            {"id": "act", "tool": "mac_act", "arguments": {
                "app": "DemoApp", "observation_id": {"$ref": "obs.observation_id"},
                "actions": [{"type": "click", "element_id": "w1/2"}], "state_mode": "none",
            }},
        ]
        resolver = MagicMock(return_value=resolve_result)
        with patch.object(decision_engine, "load_decision_config", return_value=config), \
                patch.object(decision_engine, "resolve_ambiguity", resolver):
            result = asyncio.run(execute_computer_plan(caller, plan_version=2, steps=steps))
        return result, acted, resolver

    def test_disabled_layer_keeps_fail_closed_native_ambiguity(self) -> None:
        result, acted, resolver = self._run(
            config=DecisionConfig(), resolve_result=decision_engine.DecisionResult(outcome="accepted", selected_id="c2"),
        )
        self.assertEqual("RECOVERY_AMBIGUOUS_TARGET", result["reason_code"])
        self.assertEqual(["w1/2"], acted)
        resolver.assert_not_called()

    def test_enabled_layer_rebinds_ambiguous_native_target_with_accepted_choice(self) -> None:
        accepted = decision_engine.DecisionResult(outcome="accepted", attempted=True, selected_id="c2", confidence=0.97)
        result, acted, resolver = self._run(config=DecisionConfig(enabled=True, scope="native"), resolve_result=accepted)
        self.assertTrue(result["ok"], result)
        self.assertEqual(["w1/2", "w1/3"], acted)
        self.assertEqual("native", resolver.call_args.kwargs["surface"])

    def test_enabled_layer_without_accepted_choice_still_fails_closed(self) -> None:
        rejected = decision_engine.DecisionResult(outcome="low_confidence", attempted=True, confidence=0.4)
        result, acted, _ = self._run(config=DecisionConfig(enabled=True, scope="both"), resolve_result=rejected)
        self.assertEqual("RECOVERY_AMBIGUOUS_TARGET", result["reason_code"])
        self.assertEqual(["w1/2"], acted)


if __name__ == "__main__":
    unittest.main()
