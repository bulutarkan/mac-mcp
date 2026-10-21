"""#71: say a note out loud and keep it as text, a note or a reminder.

ask_user_voice speaks a question and records the answer; there was no way to dictate a note. voice_capture
uses the same switch, the same consent before every recording and the same Groq upload, cues the person
with a local sound instead of an online voice, and deletes the recording. Silence, skip words or a declined
consent save nothing; a saved note or reminder comes from mac_app (read back, undoable).
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp_server import tools_voice as tv
from mcp_server.observability import _omit_private_answers
from mcp_server.privacy_policy import PAUSED_TOOLS


class VoiceCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Path(self.temp.name) / "settings.json"
        self.settings.write_text(json.dumps({"experimental_tools": {"ask_user_voice": {"enabled": True}}}))
        env = patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(self.settings)})
        env.start()
        self.addCleanup(env.stop)
        self.ledger: list[dict] = []
        self.recordings: list[Path] = []
        self.saved: list[tuple] = []

    def record(self, timeout, mode, temp):
        audio = Path(temp) / "a.wav"
        audio.write_bytes(b"RIFF")
        self.recordings.append(audio)
        return {"ok": True, "audio_path": str(audio)}

    def save(self, target, text):
        self.saved.append((target, text))
        return {"ok": True, "created": True, "verified": True, "note": {"id": "n1", "title": text[:20]}}

    def capture(self, target="transcript", *, consent="record_once", transcript="Yarın saat onda bankayı ara",
                record=None, upload_error=None, **kwargs):
        upload = patch.object(tv, "_transcribe_audio", side_effect=upload_error) if upload_error else \
            patch.object(tv, "_transcribe_audio", return_value=transcript)
        with patch.object(tv, "_resolve_groq_api_key", return_value="test-key"), \
                patch.object(tv, "_ask_consent", return_value=consent) as ask, \
                patch.object(tv, "_play_start_sound") as sound, \
                patch.object(tv, "_speak_question") as speak, \
                patch.object(tv, "_record_answer", side_effect=record or self.record) as recorder, \
                upload as uploader:
            result = tv.voice_capture(SimpleNamespace(), target=target, on_egress=self.ledger.append,
                                      save=self.save, **kwargs)
        return result, ask, sound, speak, recorder, uploader

    def test_a_dictated_note_is_saved_and_the_recording_deleted(self) -> None:
        result, ask, sound, speak, _, _ = self.capture("notes")
        self.assertTrue(result["ok"] and result["captured"], result)
        self.assertEqual([("notes", "Yarın saat onda bankayı ara")], self.saved)
        self.assertEqual("capture", ask.call_args.kwargs["purpose"])
        sound.assert_called_once()
        speak.assert_not_called()  # nothing goes to the online voice
        self.assertFalse(self.recordings[0].exists())
        self.assertFalse(result["recording_kept"])
        self.assertEqual({"provider": "groq", "model": tv._GROQ_TRANSCRIPTION_MODEL, "tts": "none",
                          "consent": "record_once", "outcome": "transcribed"}, self.ledger[-1])
        self.assertNotIn("banka", json.dumps(self.ledger))

    def test_transcript_only_saves_nothing(self) -> None:
        result, *_ = self.capture("transcript", language="tr")
        self.assertEqual("Yarın saat onda bankayı ara", result["transcript"])
        self.assertIsNone(result["saved"])
        self.assertEqual([], self.saved)

    def test_declined_silent_skipped_or_timed_out_saves_nothing(self) -> None:
        declined, _, _, _, recorder, uploader = self.capture("notes", consent="declined")
        self.assertFalse(declined["captured"])
        recorder.assert_not_called()
        uploader.assert_not_called()
        skipped, *_ = self.capture("reminders", transcript="iptal")
        self.assertTrue(skipped["skipped"])
        silent, *_ = self.capture("notes", upload_error=RuntimeError("Groq transcription returned an empty response"))
        self.assertTrue(silent["no_speech"])
        late, *_ = self.capture("notes", record=lambda *_: {"ok": True, "timed_out": True})
        self.assertTrue(late["timed_out"])
        self.assertEqual([], self.saved)

    def test_voice_off_or_a_bad_target_does_nothing(self) -> None:
        bad, ask, *_ = self.capture("mail")
        self.assertIn("target must be", bad["error"])
        ask.assert_not_called()
        self.settings.write_text("{}")
        off, ask, *_ = self.capture("notes")
        self.assertEqual("experimental_tool_disabled", off["error"])
        ask.assert_not_called()

    def test_a_failed_save_still_returns_the_words(self) -> None:
        self.save = lambda target, text: {"ok": False, "reason_code": "NOTES_CREATE_FAILED"}
        result, *_ = self.capture("notes")
        self.assertFalse(result["ok"])
        self.assertEqual("Yarın saat onda bankayı ara", result["transcript"])

    def test_the_consent_names_groq_and_the_capture(self) -> None:
        text = tv._consent_text("Claude", "capture")
        self.assertIn("say a note out loud", text)
        self.assertIn("Groq", text)
        self.assertNotIn("text-to-speech", text)

    def test_transcripts_stay_out_of_telemetry_and_pause_stops_it(self) -> None:
        stored = _omit_private_answers("voice_capture", {}, {
            "ok": True, "transcript": "gizli metin", "saved": {"ok": True, "note": {"id": "n1", "title": "gizli metin"}}})
        self.assertNotIn("gizli", json.dumps(stored))
        self.assertEqual("n1", stored["saved"]["note"]["id"])
        self.assertIn("voice_capture", PAUSED_TOOLS)


if __name__ == "__main__":
    unittest.main()
