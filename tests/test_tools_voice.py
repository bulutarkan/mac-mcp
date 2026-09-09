import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import tools_voice


class VoiceToolTests(unittest.TestCase):
    def setUp(self):
        self.tool_enabled_patcher = patch.object(tools_voice, "tool_enabled", return_value=True)
        self.tool_enabled_patcher.start()
        self.addCleanup(self.tool_enabled_patcher.stop)

    def tearDown(self):
        if tools_voice._DIALOG_LOCK.locked():
            try:
                tools_voice._DIALOG_LOCK.release()
            except RuntimeError:
                pass

    def test_voice_answer_returns_transcript(self):
        with tempfile.TemporaryDirectory() as td:
            audio = Path(td) / "answer.wav"
            audio.write_bytes(b"RIFFfake")
            with patch.object(tools_voice, "_resolve_groq_api_key", return_value="secret"), \
                 patch.object(tools_voice, "_speak_question", return_value="edge_tts") as speak, \
                 patch.object(
                     tools_voice,
                     "_record_answer",
                     return_value={
                         "ok": True,
                         "timed_out": False,
                         "audio_path": str(audio),
                         "input_device": "MacBook Air Microphone",
                     },
                 ), \
                 patch.object(tools_voice, "_transcribe_audio", return_value="Evet, devam et."):
                # Keep TemporaryDirectory inside ask_user_voice from deleting our mocked path
                # by returning a path that exists independently for the duration of the call.
                with patch.object(tools_voice.tempfile, "TemporaryDirectory") as temp_factory:
                    temp_factory.return_value.__enter__.return_value = td
                    temp_factory.return_value.__exit__.return_value = False
                    result = tools_voice.ask_user_voice(None, "Devam edeyim mi?", sender="Ti'Cloud")

        self.assertTrue(result["ok"])
        self.assertEqual("Evet, devam et.", result["response"])
        self.assertFalse(result["timed_out"])
        self.assertFalse(result["skipped"])
        self.assertEqual("edge_tts", result["tts_backend"])
        self.assertEqual("MacBook Air Microphone", result["input_device"])
        speak.assert_called_once()

    def test_voice_skip_phrase_returns_null_response(self):
        with tempfile.TemporaryDirectory() as td:
            audio = Path(td) / "answer.wav"
            audio.write_bytes(b"RIFFfake")
            with patch.object(tools_voice, "_resolve_groq_api_key", return_value="secret"), \
                 patch.object(tools_voice, "_speak_question", return_value="edge_tts"), \
                 patch.object(
                     tools_voice,
                     "_record_answer",
                     return_value={
                         "ok": True,
                         "timed_out": False,
                         "audio_path": str(audio),
                         "input_device": "MacBook Air Microphone",
                     },
                 ), \
                 patch.object(tools_voice, "_transcribe_audio", return_value="Boşver."), \
                 patch.object(tools_voice.tempfile, "TemporaryDirectory") as temp_factory:
                temp_factory.return_value.__enter__.return_value = td
                temp_factory.return_value.__exit__.return_value = False
                result = tools_voice.ask_user_voice(None, "Bir şey soracağım")

        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertIsNone(result["response"])

    def test_voice_timeout_preserves_response_shape(self):
        with patch.object(tools_voice, "_resolve_groq_api_key", return_value="secret"), \
             patch.object(tools_voice, "_speak_question", return_value="edge_tts"), \
             patch.object(
                 tools_voice,
                 "_record_answer",
                 return_value={
                     "ok": True,
                     "timed_out": True,
                     "input_device": "MacBook Air Microphone",
                 },
             ):
            result = tools_voice.ask_user_voice(None, "Orada mısın?", timeout_s=2)

        self.assertTrue(result["ok"])
        self.assertIsNone(result["response"])
        self.assertTrue(result["timed_out"])
        self.assertFalse(result["skipped"])

    def test_missing_groq_key_fails_before_audio(self):
        with patch.object(tools_voice, "_resolve_groq_api_key", return_value=None), \
             patch.object(tools_voice, "_speak_question") as speak:
            result = tools_voice.ask_user_voice(None, "Cevap verir misin?")

        self.assertFalse(result["ok"])
        self.assertIn("Groq API key", result["error"])
        speak.assert_not_called()

    def test_voice_prompt_shares_interactive_lock(self):
        self.assertTrue(tools_voice._DIALOG_LOCK.acquire(blocking=False))
        with patch.object(tools_voice, "_resolve_groq_api_key", return_value="secret"), \
             patch.object(tools_voice, "_speak_question") as speak:
            result = tools_voice.ask_user_voice(None, "Cevap verir misin?")
        tools_voice._DIALOG_LOCK.release()

        self.assertEqual("prompt_busy", result["error"])
        speak.assert_not_called()

    def test_disabled_voice_tool_returns_fallback_before_key_or_audio(self):
        with patch.object(tools_voice, "tool_enabled", return_value=False), \
             patch.object(tools_voice, "_resolve_groq_api_key") as key, \
             patch.object(tools_voice, "_speak_question") as speak:
            result = tools_voice.ask_user_voice(None, "Cevap verir misin?")
        self.assertFalse(result["ok"])
        self.assertEqual("experimental_tool_disabled", result["error"])
        self.assertEqual("ask_user", result["fallback_tool"])
        key.assert_not_called(); speak.assert_not_called()

    def test_keychain_groq_key_is_used_after_environment(self):
        with patch.dict(tools_voice.os.environ, {"MAC_MCP_VOICE_GROQ_API_KEY": "", "GROQ_API_KEY": ""}, clear=False), \
             patch.object(tools_voice, "keychain_password", return_value="keychain-secret"):
            self.assertEqual("keychain-secret", tools_voice._resolve_groq_api_key())

    def test_environment_groq_key_keeps_precedence_over_keychain(self):
        with patch.dict(tools_voice.os.environ, {"MAC_MCP_VOICE_GROQ_API_KEY": "env-secret"}, clear=False), \
             patch.object(tools_voice, "keychain_password") as keychain:
            self.assertEqual("env-secret", tools_voice._resolve_groq_api_key())
        keychain.assert_not_called()


if __name__ == "__main__":
    unittest.main()
