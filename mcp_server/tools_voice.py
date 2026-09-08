from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from .security import Settings
from .tools_interactive import _DIALOG_LOCK, _normalize_timeout, _validate_question_and_sender


_DEFAULT_VOICE = "tr-TR-AhmetNeural"
_DEFAULT_LANGUAGE = "auto"
_DEFAULT_INPUT_MODE = "auto"
_DEFAULT_OUTPUT_MODE = "system"
_DEFAULT_RATE = "-5%"
_GROQ_TRANSCRIPTION_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
_GROQ_TRANSCRIPTION_MODEL = "whisper-large-v3-turbo"
_SKIP_WORDS = {
    "atla",
    "iptal",
    "boşver",
    "bosver",
    "vazgeç",
    "vazgec",
}

_HELPER_BUILD_LOCK = threading.Lock()


def _cache_dir() -> Path:
    path = Path(os.getenv("MAC_MCP_VOICE_CACHE_DIR", "~/.mac-mcp/cache/voice")).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _source_path(name: str) -> Path:
    return Path(__file__).resolve().with_name(name)


def _source_fingerprint(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _write_voice_info_plist(path: Path) -> None:
    payload = {
        "CFBundleExecutable": "MacMCPVoiceHelper",
        "CFBundleIdentifier": "dev.macmcp.voicehelper",
        "CFBundleName": "Mac MCP Voice Helper",
        "CFBundlePackageType": "APPL",
        "CFBundleVersion": "1",
        "CFBundleShortVersionString": "1.0",
        "LSUIElement": True,
        "NSMicrophoneUsageDescription": (
            "Mac MCP needs microphone access only while waiting for your spoken answer."
        ),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        plistlib.dump(payload, handle)


def _run_checked(command: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=True,
    )


def _ensure_helpers() -> tuple[Path, Path]:
    voice_source = _source_path("voice_helper.swift")
    route_source = _source_path("audio_route.swift")
    fingerprint = _source_fingerprint([voice_source, route_source])
    cache = _cache_dir()
    marker = cache / "build.sha256"
    app = cache / "MacMCPVoiceHelper.app"
    app_executable = app / "Contents" / "MacOS" / "MacMCPVoiceHelper"
    route_executable = cache / "audio_route"

    if (
        marker.exists()
        and marker.read_text(encoding="utf-8").strip() == fingerprint
        and app_executable.exists()
        and route_executable.exists()
    ):
        return app, route_executable

    with _HELPER_BUILD_LOCK:
        if (
            marker.exists()
            and marker.read_text(encoding="utf-8").strip() == fingerprint
            and app_executable.exists()
            and route_executable.exists()
        ):
            return app, route_executable

        swiftc = shutil.which("swiftc") or "/usr/bin/swiftc"
        codesign = shutil.which("codesign") or "/usr/bin/codesign"
        if not Path(swiftc).exists():
            raise RuntimeError("swiftc is unavailable; install the Xcode Command Line Tools")

        app_executable.parent.mkdir(parents=True, exist_ok=True)
        _write_voice_info_plist(app / "Contents" / "Info.plist")

        _run_checked(
            [
                swiftc,
                "-O",
                "-framework",
                "AVFoundation",
                "-framework",
                "Foundation",
                "-framework",
                "CoreAudio",
                "-framework",
                "AudioToolbox",
                str(voice_source),
                "-o",
                str(app_executable),
            ],
            timeout=90,
        )
        _run_checked(
            [
                swiftc,
                "-O",
                "-framework",
                "CoreAudio",
                str(route_source),
                "-o",
                str(route_executable),
            ],
            timeout=90,
        )
        route_executable.chmod(0o755)
        app_executable.chmod(0o755)
        if Path(codesign).exists():
            _run_checked([codesign, "--force", "--deep", "--sign", "-", str(app)], timeout=30)
        marker.write_text(fingerprint + "\n", encoding="utf-8")

    return app, route_executable


def _resolve_groq_api_key() -> Optional[str]:
    direct = (
        os.getenv("MAC_MCP_VOICE_GROQ_API_KEY", "").strip()
        or os.getenv("GROQ_API_KEY", "").strip()
    )
    if direct:
        return direct

    domain = os.getenv("MAC_MCP_VOICE_GROQ_DEFAULTS_DOMAIN", "").strip()
    if not domain:
        return None
    key_name = os.getenv("MAC_MCP_VOICE_GROQ_DEFAULTS_KEY", "groqAPIKey").strip() or "groqAPIKey"
    try:
        result = subprocess.run(
            ["defaults", "read", domain, key_name],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = (result.stdout or "").strip()
    return value or None


def _parse_route_state(stdout: str) -> Dict[str, str]:
    state: Dict[str, str] = {}
    for line in stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        state[key.strip()] = value.strip()
    return state


def _route_uid(value: str) -> Optional[str]:
    if "|" not in value:
        return None
    _name, uid = value.rsplit("|", 1)
    uid = uid.strip()
    return uid if uid and uid != "unknown" else None


def _prepare_output_route(route_executable: Path, output_mode: str) -> tuple[Optional[str], Optional[str]]:
    if output_mode in {"system", "default", "current"}:
        return None, None

    current = _run_checked([str(route_executable), "get"], timeout=5)
    state = _parse_route_state(current.stdout)
    previous_output_uid = _route_uid(state.get("output", ""))
    previous_system_uid = _route_uid(state.get("system", ""))

    if output_mode == "built-in":
        _run_checked([str(route_executable), "set-builtin"], timeout=5)
    elif output_mode.startswith("name:"):
        name = output_mode.split(":", 1)[1].strip()
        if not name:
            raise RuntimeError("MAC_MCP_VOICE_OUTPUT_DEVICE name is empty")
        _run_checked([str(route_executable), "set-name", name], timeout=5)
    else:
        raise RuntimeError("MAC_MCP_VOICE_OUTPUT_DEVICE must be built-in, system, or name:<device>")

    return previous_output_uid, previous_system_uid


def _restore_output_route(
    route_executable: Path,
    previous_output_uid: Optional[str],
    previous_system_uid: Optional[str],
) -> None:
    # Only the normal media output is changed; system-alert routing stays untouched.
    uid = previous_output_uid or previous_system_uid
    if not uid:
        return
    try:
        _run_checked([str(route_executable), "set-uid", uid], timeout=5)
    except Exception:
        pass


def _speak_question(question: str, voice: str, rate: str, output_mode: str, temp_dir: Path) -> str:
    _app, route_executable = _ensure_helpers()
    audio_path = temp_dir / "question.mp3"
    edge_command = [
        sys.executable,
        "-m",
        "edge_tts",
        "--voice",
        voice,
        "--rate",
        rate,
        "--text",
        question,
        "--write-media",
        str(audio_path),
    ]
    _run_checked(edge_command, timeout=30)

    previous_output_uid: Optional[str] = None
    previous_system_uid: Optional[str] = None
    try:
        previous_output_uid, previous_system_uid = _prepare_output_route(route_executable, output_mode)
        _run_checked(["/usr/bin/afplay", str(audio_path)], timeout=60)
    finally:
        _restore_output_route(route_executable, previous_output_uid, previous_system_uid)
    return "edge_tts"


def _record_answer(timeout_s: int, input_mode: str, temp_dir: Path) -> Dict[str, Any]:
    app, _route = _ensure_helpers()
    result_path = temp_dir / "record-result.json"
    audio_path = temp_dir / "answer.wav"
    command = [
        "/usr/bin/open",
        "-W",
        "-n",
        str(app),
        "--args",
        "--result",
        str(result_path),
        "--audio",
        str(audio_path),
        "--timeout",
        str(timeout_s),
        "--input",
        input_mode,
    ]
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_s + 40,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Voice helper timed out"}

    if not result_path.exists():
        detail = (completed.stderr or completed.stdout or "").strip()
        return {"ok": False, "error": detail or "Voice helper did not return a result"}

    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"Could not read voice helper result: {exc}"}

    if result.get("audio_path"):
        result["audio_path"] = str(audio_path)
    return result


def _transcribe_audio(audio_path: Path, language: str, api_key: str) -> str:
    timeout = httpx.Timeout(35.0, connect=10.0)
    with audio_path.open("rb") as audio_handle, httpx.Client(timeout=timeout) as client:
        data = {
            "model": _GROQ_TRANSCRIPTION_MODEL,
            "response_format": "text",
        }
        if language and language.lower() != "auto":
            data["language"] = language
        response = client.post(
            _GROQ_TRANSCRIPTION_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            data=data,
            files={"file": ("answer.wav", audio_handle, "audio/wav")},
        )
    if response.status_code == 401:
        raise RuntimeError("Groq API key is invalid or expired")
    if response.status_code < 200 or response.status_code >= 300:
        detail = response.text.strip()
        if len(detail) > 500:
            detail = detail[:500] + "…"
        raise RuntimeError(f"Groq transcription failed (HTTP {response.status_code}): {detail}")
    text = response.text.strip()
    if not text:
        raise RuntimeError("Groq transcription returned an empty response")
    return text


def _is_skip_response(response: str) -> bool:
    normalized = response.strip().lower().strip(".!?,;:\"' ")
    return normalized in _SKIP_WORDS


def ask_user_voice(
    settings: Settings,
    question: str,
    sender: str = "AI",
    timeout_s: int = 45,
    voice: str = _DEFAULT_VOICE,
) -> Dict[str, Any]:
    """Speak a short question, record the local user's answer, and return its transcript."""
    total_timeout = _normalize_timeout(timeout_s)
    if total_timeout is None:
        return {"ok": False, "error": "timeout_s must be a positive integer"}

    common = _validate_question_and_sender(question, sender)
    if isinstance(common, dict):
        return common
    question_display, sender_display = common

    if not isinstance(voice, str) or not voice.strip():
        return {"ok": False, "error": "voice must be a non-empty string"}
    voice = voice.strip()[:100]

    if not _DIALOG_LOCK.acquire(blocking=False):
        return {
            "ok": False,
            "error": "prompt_busy",
            "message": "Another interactive prompt is already active; answer or close it before asking again.",
        }

    api_key = _resolve_groq_api_key()
    if not api_key:
        _DIALOG_LOCK.release()
        return {
            "ok": False,
            "error": (
                "Voice transcription needs a Groq API key. Set MAC_MCP_VOICE_GROQ_API_KEY, GROQ_API_KEY, "
                "or MAC_MCP_VOICE_GROQ_DEFAULTS_DOMAIN to reuse an existing macOS app preference."
            ),
        }

    language = os.getenv("MAC_MCP_VOICE_LANGUAGE", _DEFAULT_LANGUAGE).strip() or _DEFAULT_LANGUAGE
    input_mode = os.getenv("MAC_MCP_VOICE_INPUT_DEVICE", _DEFAULT_INPUT_MODE).strip() or _DEFAULT_INPUT_MODE
    output_mode = os.getenv("MAC_MCP_VOICE_OUTPUT_DEVICE", _DEFAULT_OUTPUT_MODE).strip() or _DEFAULT_OUTPUT_MODE
    rate = os.getenv("MAC_MCP_VOICE_TTS_RATE", _DEFAULT_RATE).strip() or _DEFAULT_RATE

    try:
        with tempfile.TemporaryDirectory(prefix="mac-mcp-voice-") as temp_root:
            temp_dir = Path(temp_root)
            try:
                tts_backend = _speak_question(question_display, voice, rate, output_mode, temp_dir)
            except FileNotFoundError as exc:
                return {"ok": False, "error": f"Voice playback dependency is unavailable: {exc}"}
            except subprocess.CalledProcessError as exc:
                detail = (exc.stderr or exc.stdout or "").strip()
                return {
                    "ok": False,
                    "error": "Could not synthesize or play the voice question" + (f": {detail[:500]}" if detail else ""),
                }
            except Exception as exc:
                return {"ok": False, "error": f"Could not play voice question: {exc}"}

            recorded = _record_answer(total_timeout, input_mode, temp_dir)
            if not recorded.get("ok"):
                return {"ok": False, "error": recorded.get("error", "Voice recording failed")}
            if recorded.get("timed_out"):
                return {
                    "ok": True,
                    "response": None,
                    "timed_out": True,
                    "skipped": False,
                    "sender": sender_display,
                    "voice": voice,
                    "tts_backend": tts_backend,
                    "stt_backend": _GROQ_TRANSCRIPTION_MODEL,
                    "input_device": recorded.get("input_device"),
                }

            audio_path = Path(recorded.get("audio_path") or "")
            if not audio_path.exists():
                return {"ok": False, "error": "Voice helper did not produce an audio file"}

            try:
                response = _transcribe_audio(audio_path, language, api_key)
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

            skipped = _is_skip_response(response)
            return {
                "ok": True,
                "response": None if skipped else response,
                "timed_out": False,
                "skipped": skipped,
                "sender": sender_display,
                "voice": voice,
                "tts_backend": tts_backend,
                "stt_backend": _GROQ_TRANSCRIPTION_MODEL,
                "input_device": recorded.get("input_device"),
            }
    finally:
        _DIALOG_LOCK.release()
