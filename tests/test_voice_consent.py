from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp_server import tools_voice as tv
from mcp_server.observability import TelemetryManager
from mcp_server.tools_interactive import _DIALOG_LOCK


class VoiceConsentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Path(self.temp.name) / "settings.json"
        self.write_settings(enabled=True)
        self.env = patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(self.settings)})
        self.env.start()
        self.ledger: list[dict] = []

    def tearDown(self) -> None:
        self.env.stop()
        self.temp.cleanup()

    def write_settings(self, *, enabled=None, consent=None) -> None:
        data: dict = {}
        if enabled is not None:
            data["experimental_tools"] = {"ask_user_voice": {"enabled": enabled}}
        if consent is not None:
            data["voice"] = {"transcription_consent": consent}
        self.settings.write_text(json.dumps(data), encoding="utf-8")

    def ask(self, *, dialog_output="record_once", transcript="merhaba"):
        lock_free_during_consent: list[bool] = []

        def fake_dialog(script, timeout):
            lock_free_during_consent.append(not _DIALOG_LOCK.locked())
            self.assertIn("Groq", script)
            self.assertIn("text-to-speech", script)
            return {"ok": True, "output": dialog_output}

        with patch.object(tv, "_resolve_groq_api_key", return_value="test-key"), \
             patch.object(tv, "_run_native_script", side_effect=fake_dialog) as dialog, \
             patch.object(tv, "_speak_question", return_value="edge_tts") as speak, \
             patch.object(tv, "_record_answer", side_effect=lambda timeout, mode, temp: (
                 (Path(temp) / "a.wav").write_bytes(b"RIFF") and {"ok": True, "audio_path": str(Path(temp) / "a.wav")})) as record, \
             patch.object(tv, "_transcribe_audio", return_value=transcript) as upload:
            result = tv.ask_user_voice(SimpleNamespace(), "Devam edeyim mi?", on_egress=self.ledger.append)
        return result, dialog, speak, record, upload, lock_free_during_consent

    def test_voice_is_off_until_the_person_enables_it(self) -> None:
        self.settings.write_text("{}", encoding="utf-8")
        result = tv.ask_user_voice(SimpleNamespace(), "Hi?")
        self.assertEqual("experimental_tool_disabled", result["error"])

    def test_declining_records_and_sends_nothing(self) -> None:
        for output in ("declined", "timed_out"):
            self.ledger.clear()
            with self.subTest(output=output):
                result, dialog, speak, record, upload, lock_free = self.ask(dialog_output=output)
                self.assertTrue(result["skipped"])
                self.assertEqual("ask_user", result["fallback_tool"])
                speak.assert_not_called()
                record.assert_not_called()
                upload.assert_not_called()
                self.assertEqual([True], lock_free, "the consent dialog must not be blocked by the recording lock")
                self.assertEqual("not_recorded", self.ledger[-1]["outcome"])

    def test_record_once_asks_every_time_and_logs_metadata_only(self) -> None:
        result, dialog, speak, record, upload, _ = self.ask()
        self.assertEqual("merhaba", result["response"])
        dialog.assert_called_once()
        upload.assert_called_once()
        self.assertEqual({"provider": "groq", "model": tv._GROQ_TRANSCRIPTION_MODEL, "tts": "edge_tts",
                          "consent": "record_once", "outcome": "transcribed"}, self.ledger[-1])
        self.assertNotIn("merhaba", json.dumps(self.ledger))
        self.assertFalse(_DIALOG_LOCK.locked())

    def test_always_allow_is_remembered_and_skips_the_dialog(self) -> None:
        self.ask(dialog_output="always")
        saved = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual("always", saved["voice"]["transcription_consent"])
        _, dialog, _, _, upload, _ = self.ask()
        dialog.assert_not_called()
        upload.assert_called_once()
        self.assertEqual("always", self.ledger[-1]["consent"])

    def test_missing_key_fails_before_asking(self) -> None:
        with patch.object(tv, "_resolve_groq_api_key", return_value=None), \
             patch.object(tv, "_run_native_script") as dialog:
            result = tv.ask_user_voice(SimpleNamespace(), "Hi?")
        self.assertFalse(result["ok"])
        dialog.assert_not_called()


class VoiceTelemetryTests(unittest.TestCase):
    def test_transcripts_never_reach_telemetry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            telemetry = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
            direct = telemetry.start_call("mcp", "ask_user_voice", {"question": "Q?"})
            telemetry.finish_call(direct, result={"ok": True, "response": "my secret answer"})
            nested = telemetry.start_call("mcp", "tool_invoke", {"tool_name": "ask_user_voice", "arguments": {}})
            telemetry.finish_call(nested, result={"ok": True, "tool": "ask_user_voice",
                                                  "result": {"ok": True, "response": "another secret"}})
            stored = json.dumps(telemetry.query_events(hours=1, limit=10))
        self.assertNotIn("my secret answer", stored)
        self.assertNotIn("another secret", stored)
        self.assertIn("voice transcript not stored", stored)


if __name__ == "__main__":
    unittest.main()
