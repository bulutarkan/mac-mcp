from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import unquote, unquote_plus

# Intentionally conservative direct-secret patterns. Generic high-entropy values are
# only used as source fingerprints; they do not independently block ordinary user text.
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----.*?-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+([A-Za-z0-9._~+/=-]{8,})")
_KEY_VALUE_RE = re.compile(
    r"(?im)(?:^|[\s,{;])(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|client[_-]?secret|password|passwd|passphrase|credential)\s*[=:]\s*[\"']?([^\s\"'&,;}]{8,})"
)
_KNOWN_TOKEN_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{12,}|AIza[A-Za-z0-9_-]{20,})\b"
)
_HIGH_ENTROPY_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_+/=-]{32,256}(?![A-Za-z0-9])")
_URL_SECRET_RE = re.compile(
    r"(?i)([?&](?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|passwd)=)([^&#\s]+)"
)
_ENV_SECRET_RE = re.compile(
    r"(?im)^(\s*[A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)[A-Z0-9_]*\s*=\s*)(.+)$"
)
# The owner's decision (#85): an API key in a URL query string (ChatGPT's connector URL carries
# ?ApiKey=) is never blocked or flagged by the egress checks. Logs still redact it (_URL_SECRET_RE).
_URL_API_KEY_RE = re.compile(r"(?i)([?&]api[_-]?key=)[^&#\s\"'<>]*")
# Opaque values from a source no path vouches for (the clipboard). A random-looking key mixes upper
# case, lower case and digits, carries at least 4.1 bits/char (hex digests, git SHAs and UUIDs top out
# at 4.0-4.09: 16-17 symbols) and comes in short case runs (paths and CamelCase identifiers average 4+
# characters per run; random base62/base64 keys of 32+ characters stay under 3.5 in 99.9% of draws).
_OPAQUE_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_+/-])[A-Za-z0-9_+/-]{32,256}={0,2}(?![A-Za-z0-9_+/=-])")
_URL_SPAN_RE = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://\S+")
_CASE_RUN_RE = re.compile(r"[A-Z]?[a-z]+|[A-Z]+|[0-9]+")
_OPAQUE_MIN_ENTROPY = 4.1
_OPAQUE_MAX_MEAN_RUN = 3.5
# NAME_TOKEN=value anywhere in a command line (export, inline env, flags).
_ASSIGNED_SECRET_RE = re.compile(
    r"\b([A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)[A-Z0-9_]*=)(\"[^\"]*\"|'[^']*'|[^\s;&|]+)"
)

_SOURCE_TOOLS = frozenset({
    "read_file", "read_multiple_files", "clipboard_get", "run_command",
    "run_commands_parallel", "get_job_output", "wait_jobs", "search_files",
    # Other tools that hand host content to the model: AppleScript output, process
    # command lines, delegated-agent reports and stored memories.
    "run_applescript", "process_list", "get_agent", "memory_get", "memory_search", "knowledge_search",
    # Job records carry the command line.
    "list_jobs", "get_job_status",
})
# grep -rn output: "path:line:content".
_SEARCH_LINE_RE = re.compile(r"^(?P<path>[^\n:]+):(?P<line>\d+):(?P<content>.*)$")
_BROWSER_EGRESS_TOOLS = frozenset({
    "browser_open_url", "browser_act", "browser_do", "browser_execute_js",
    "browser_type_selector", "browser_press_key", "browser_upload_artifact", "open_url", "http_request",
})


@dataclass(frozen=True)
class SensitiveSourceScan:
    sensitive: bool
    reasons: tuple[str, ...] = ()
    fingerprints: frozenset[str] = frozenset()
    source_class: Optional[str] = None


@dataclass(frozen=True)
class EgressScan:
    sensitive: bool
    reason: Optional[str] = None
    target_class: Optional[str] = None


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()


def _entropy(text: str) -> float:
    if not text:
        return 0.0
    counts: dict[str, int] = {}
    for char in text:
        counts[char] = counts.get(char, 0) + 1
    length = len(text)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def without_url_api_keys(text: str) -> str:
    """The text with every URL api_key/ApiKey query value emptied (#85, see _URL_API_KEY_RE)."""
    return _URL_API_KEY_RE.sub(r"\1", str(text or ""))


def _configured_token_paths() -> frozenset[str]:
    """Mac MCP's own credential files (dashboard, Chrome companion, Cloudflare tunnel) where the
    configuration puts them, canonical. Imported on use: those modules pull in more than this one."""
    from .chrome_background_bridge import chrome_companion_token_path
    from .public_endpoint import cloudflare_token_path
    from .security import dashboard_token_path

    found: set[str] = set()
    for resolver in (dashboard_token_path, chrome_companion_token_path, cloudflare_token_path):
        try:
            found.add(os.path.realpath(resolver().expanduser()))
        except (OSError, RuntimeError, ValueError):
            continue
    return frozenset(found)


def sensitive_path_class(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    try:
        # By canonical path: these names have no extension, and a symlink or ../ must not hide them.
        if os.path.realpath(path) in _configured_token_paths():
            return "token_file"
    except (OSError, ValueError):
        pass
    lower = str(path).lower()
    name = path.name.lower()
    parts = {part.lower() for part in path.parts}

    if name == ".env" or name.startswith(".env."):
        return "env_file"
    if ".ssh" in parts and name.startswith("id_") and not name.endswith(".pub"):
        return "ssh_private_key"
    if name in {"credentials", "credentials.json", "service-account.json", "service_account.json"}:
        return "credential_file"
    if name in {".netrc", ".npmrc", ".pypirc", ".git-credentials", "auth.json"}:
        return "credential_file"
    if name.endswith((".pem", ".key", ".p12", ".pfx")):
        return "private_key_file"
    if "token" in name and name.endswith((".json", ".pickle", ".pkl", ".txt")):
        return "token_file"
    if "/.aws/credentials" in lower or "/.config/gcloud/application_default_credentials.json" in lower:
        return "credential_file"
    return None


def _direct_secret_candidates(text: str) -> list[str]:
    if not text:
        return []
    found: list[str] = []
    found.extend(match.group(0) for match in _PRIVATE_KEY_RE.finditer(text))
    found.extend(match.group(1) for match in _BEARER_RE.finditer(text))
    found.extend(match.group(1) for match in _KEY_VALUE_RE.finditer(text))
    found.extend(match.group(0) for match in _KNOWN_TOKEN_RE.finditer(text))
    return [candidate.strip() for candidate in found if candidate and candidate.strip()]


def _entropy_candidates(text: str) -> list[str]:
    found: list[str] = []
    for match in _HIGH_ENTROPY_RE.finditer(text or ""):
        token = match.group(0).strip()
        if len(set(token)) < 10:
            continue
        if not any(ch.isalpha() for ch in token) or not any(ch.isdigit() for ch in token):
            continue
        if _entropy(token) >= 3.6:
            found.append(token)
    return found


def _opaque_candidates(text: str, *, skip_urls: bool = True) -> list[str]:
    """Random-looking tokens (see _OPAQUE_MIN_ENTROPY). URLs are skipped by default: document and
    file ids in links look random too, and an API key in a query string is not flagged (#85)."""
    value = str(text or "")
    if skip_urls:
        value = _URL_SPAN_RE.sub(" ", value)
    found: list[str] = []
    for match in _OPAQUE_TOKEN_RE.finditer(value):
        token = match.group(0)
        if not (any(ch.isupper() for ch in token) and any(ch.islower() for ch in token)
                and any(ch.isdigit() for ch in token)):
            continue
        if _entropy(token) < _OPAQUE_MIN_ENTROPY:
            continue
        runs = _CASE_RUN_RE.findall(token)
        if runs and sum(len(run) for run in runs) / len(runs) > _OPAQUE_MAX_MEAN_RUN:
            continue
        found.append(token)
    return found


def _env_secret_values(text: str) -> list[str]:
    """Values of NAME_TOKEN= / SERVICE_API_KEY= style lines (.env files). Only for remembering what
    was read: on its own this pattern would also match ordinary outgoing code samples."""
    values = []
    for match in _ENV_SECRET_RE.finditer(text or ""):
        value = match.group(2).strip().strip("\"'")
        if len(value) >= 8:
            values.append(value)
    return values


def secret_fingerprints(text: str, *, include_entropy: bool = True, include_whole: bool = False) -> frozenset[str]:
    fingerprints = {_digest(candidate) for candidate in _direct_secret_candidates(text)}
    fingerprints.update(_digest(value) for value in _env_secret_values(text))
    if include_entropy:
        fingerprints.update(_digest(candidate) for candidate in _entropy_candidates(text))
    normalized = str(text or "").strip()
    if include_whole and normalized:
        fingerprints.add(_digest(normalized))
        # Exact line fingerprints cover the common "copy one secret line" path
        # without retaining any raw secret material in state.
        for line in normalized.splitlines()[:256]:
            item = line.strip()
            if len(item) >= 8:
                fingerprints.add(_digest(item))
    return frozenset(fingerprints)


def contains_direct_secret(text: str) -> bool:
    return bool(_direct_secret_candidates(str(text or "")))


def redact_sensitive_text(text: str) -> str:
    value = str(text or "")
    value = _PRIVATE_KEY_RE.sub("[PRIVATE KEY REDACTED]", value)
    value = _BEARER_RE.sub("Bearer [REDACTED]", value)
    value = _KEY_VALUE_RE.sub(lambda m: m.group(0).replace(m.group(1), "[REDACTED]"), value)
    value = _KNOWN_TOKEN_RE.sub("[REDACTED]", value)
    value = _URL_SECRET_RE.sub(lambda m: m.group(1) + "[REDACTED]", value)
    value = _ENV_SECRET_RE.sub(lambda m: m.group(1) + "[REDACTED]", value)
    return value


def mask_command_credentials(command: str) -> str:
    """A shell command as job records keep and list it: credentials masked, the rest readable."""
    value = redact_sensitive_text(command)
    value = _ASSIGNED_SECRET_RE.sub(lambda m: m.group(1) + "[REDACTED]", value)
    for token in sorted(set(_opaque_candidates(value, skip_urls=False)), key=len, reverse=True):
        value = value.replace(token, "[REDACTED]")
    return value


def _walk_strings(value: Any, *, depth: int = 0, limit: int = 256) -> Iterable[str]:
    if depth > 6 or limit <= 0:
        return
    if isinstance(value, str):
        yield value
        text = value.strip()
        if text[:1] in {"{", "["}:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return
            yield from _walk_strings(parsed, depth=depth + 1, limit=max(1, limit - 1))
        return
    if isinstance(value, Mapping):
        count = 0
        for child in value.values():
            if count >= limit:
                break
            for item in _walk_strings(child, depth=depth + 1, limit=limit - count):
                yield item
                count += 1
                if count >= limit:
                    break
        return
    if isinstance(value, (list, tuple)):
        count = 0
        for child in value[:limit]:
            for item in _walk_strings(child, depth=depth + 1, limit=limit - count):
                yield item
                count += 1
                if count >= limit:
                    break
        return
    if hasattr(value, "model_dump"):
        try:
            yield from _walk_strings(value.model_dump(), depth=depth + 1, limit=limit)
        except Exception:
            return


def _result_path(arguments: Mapping[str, Any], result: Any) -> Optional[str]:
    for candidate in (arguments.get("path"),):
        if candidate:
            return str(candidate)
    if isinstance(result, Mapping) and result.get("path"):
        return str(result.get("path"))
    return None


def _file_records(result: Any) -> Optional[list[Mapping[str, Any]]]:
    """read_multiple_files' per-file records, also when an MCP call delivers them serialized."""
    if isinstance(result, (list, tuple)) and len(result) == 1:
        result = getattr(result[0], "text", result[0])
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return None
    files = result.get("files") if isinstance(result, Mapping) else None
    if not isinstance(files, list):
        return None
    return [item for item in files if isinstance(item, Mapping)]


def _scan_search_results(result: Any) -> SensitiveSourceScan:
    """Fingerprint secrets in search matches without keeping any raw value.

    A search can surface an opaque token that matches no direct-secret pattern, so
    every high-entropy value in a matched line becomes a known fingerprint; a match
    inside a credential file fingerprints the whole line as well.
    """
    if isinstance(result, Mapping) and isinstance(result.get("results"), str):
        texts = [result["results"]]
    else:
        # MCP calls deliver the dict as serialized content; walk it the same way.
        texts = list(_walk_strings(result))
    reasons: set[str] = set()
    fingerprints: set[str] = set()
    source_class: Optional[str] = None
    lines = [line for text in texts for line in str(text or "").splitlines()]
    path_classes: dict[str, Optional[str]] = {}  # one canonical-path lookup per file, not per line
    for raw_line in lines[:5000]:
        match = _SEARCH_LINE_RE.match(raw_line)
        path, content = (match.group("path"), match.group("content")) if match else ("", raw_line)
        if path and path not in path_classes:
            path_classes[path] = sensitive_path_class(path)
        path_class = path_classes.get(path) if path else None
        direct = secret_fingerprints(content, include_entropy=False)
        entropy = secret_fingerprints(content, include_entropy=True).difference(direct)
        if direct:
            reasons.add("secret_pattern")
        if entropy:
            reasons.add("high_entropy_secret")
        fingerprints.update(direct)
        fingerprints.update(entropy)
        if path_class:
            source_class = source_class or path_class
            reasons.add("sensitive_source")
            fingerprints.update(secret_fingerprints(content, include_entropy=True, include_whole=True))
    return SensitiveSourceScan(bool(reasons), tuple(sorted(reasons)), frozenset(fingerprints), source_class)


def scan_sensitive_source(tool: str, arguments: Mapping[str, Any], result: Any) -> SensitiveSourceScan:
    name = str(tool or "")
    if name not in _SOURCE_TOOLS:
        return SensitiveSourceScan(False)
    if name == "search_files":
        return _scan_search_results(result)
    if name == "read_multiple_files":
        records = _file_records(result)
        if records is not None:
            # Each file by its own path, so a credential file among ordinary ones counts as one.
            scans = [scan_sensitive_source("read_file", {"path": record.get("path")}, record) for record in records]
            return SensitiveSourceScan(
                any(scan.sensitive for scan in scans),
                tuple(sorted({reason for scan in scans for reason in scan.reasons})),
                frozenset(fingerprint for scan in scans for fingerprint in scan.fingerprints),
                next((scan.source_class for scan in scans if scan.source_class), None),
            )

    source_class = sensitive_path_class(_result_path(arguments, result))
    if source_class is None and isinstance(result, Mapping) and result.get("path"):
        # The path the tool actually read (resolved), when the requested one was relative.
        source_class = sensitive_path_class(result.get("path"))
    reasons: set[str] = set()
    fingerprints: set[str] = set()
    include_whole = bool(source_class)

    if name in {"run_command", "run_commands_parallel"}:
        commands = arguments.get("commands") if name == "run_commands_parallel" else [arguments.get("command")]
        command_text = "\n".join(str(item or "") for item in (commands or []))
        lower = command_text.lower()
        if any(marker in lower for marker in ("/.ssh/", "~/.ssh/", ".env", "security find-generic-password", "security find-internet-password", "printenv", " env")):
            source_class = source_class or "credential_command_output"
            include_whole = True

    for text in _walk_strings(result):
        if name == "clipboard_get":
            # No path vouches for clipboard text, so only random-looking tokens are remembered (#113).
            text = without_url_api_keys(text)
            opaque = {_digest(token) for token in _opaque_candidates(text)}
            if opaque:
                reasons.add("high_entropy_secret")
                fingerprints.update(opaque)
        direct = secret_fingerprints(text, include_entropy=False, include_whole=False)
        entropy = secret_fingerprints(text, include_entropy=True, include_whole=False).difference(direct)
        if direct:
            reasons.add("secret_pattern")
        if entropy and (include_whole or direct):
            reasons.add("high_entropy_secret")
        fingerprints.update(direct)
        if include_whole:
            fingerprints.update(secret_fingerprints(text, include_entropy=True, include_whole=True))

    if source_class:
        reasons.add("sensitive_source")
    return SensitiveSourceScan(bool(reasons), tuple(sorted(reasons)), frozenset(fingerprints), source_class)


def _browser_payload_strings(tool: str, arguments: Mapping[str, Any]) -> list[str]:
    name = str(tool or "")
    if name == "browser_type_selector":
        return [str(arguments.get("text") or "")]
    if name == "browser_execute_js":
        return [str(arguments.get("js") or "")]
    if name in {"browser_open_url", "open_url"}:
        return [str(arguments.get("url") or "")]
    if name == "http_request":
        values: list[str] = [str(arguments.get("url") or ""), str(arguments.get("body") or "")]
        headers = arguments.get("headers") or {}
        if isinstance(headers, Mapping):
            values.extend(str(value or "") for value in headers.values())
        return values
    if name in {"browser_act", "browser_do"}:
        values = []
        for action in arguments.get("actions") or []:
            if not isinstance(action, Mapping):
                continue
            typ = str(action.get("type") or "").lower().replace("-", "_")
            if typ == "fill" and isinstance(action.get("values"), Mapping):
                values.extend(str(value) for value in action["values"].values() if value is not None)
                continue
            if typ not in {"type", "type_text", "paste", "post"}:
                continue
            for key in ("text", "value", "content"):
                if action.get(key) is not None:
                    values.append(str(action.get(key)))
        return values
    return []


def _payload_candidate_fingerprints(text: str) -> frozenset[str]:
    value = str(text or "")
    candidates: set[str] = set()
    stripped = value.strip()
    if 8 <= len(stripped) <= 4096:
        candidates.add(stripped)
    for match in re.finditer(r"[\"']([^\"'\n\r]{8,512})[\"']", value):
        candidates.add(match.group(1).strip())
    for match in re.finditer(r"(?<![A-Za-z0-9])[A-Za-z0-9_+/.=-]{8,256}(?![A-Za-z0-9])", value):
        candidates.add(match.group(0).strip())
    for match in re.finditer(r"(?:^|[?&=,:;\s])([^?&=,:;\s]{8,256})(?=$|[?&,:;\s])", value):
        candidates.add(match.group(1).strip("\"'"))
    return frozenset(_digest(candidate) for candidate in candidates if candidate)


# Encodings an agent can apply locally before sending (#7). Decoded forms are tested like the raw
# payload; the plaintext path is unchanged. Bounded so a large body cannot make this expensive.
_B64_TOKEN_RE = re.compile(r"[A-Za-z0-9+/_-]{16,}={0,2}")
_HEX_TOKEN_RE = re.compile(r"(?:[0-9a-fA-F]{2}){8,}")
_MAX_DECODE_INPUT = 2_000_000
_MAX_DECODED_VARIANTS = 64
_MAX_DECODE_DEPTH = 2


def _printable_text(raw: bytes) -> Optional[str]:
    if len(raw) < 6:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    printable = sum(1 for char in text if char.isprintable() or char in "\n\r\t")
    return text if printable >= 0.9 * len(text) else None


def _decode_token(token: str) -> list[str]:
    found: list[str] = []
    compact = re.sub(r"\s+", "", token)
    if _HEX_TOKEN_RE.fullmatch(compact):
        try:
            text = _printable_text(binascii.unhexlify(compact))
        except (binascii.Error, ValueError):
            text = None
        if text:
            found.append(text)
    if len(compact) >= 16 and re.fullmatch(r"[A-Za-z0-9+/_-]+={0,2}", compact):
        padded = compact + "=" * (-len(compact) % 4)
        for decoder in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                text = _printable_text(decoder(padded))
            except (binascii.Error, ValueError):
                continue
            if text and text not in found:
                found.append(text)
    return found


def _decodings(text: str) -> list[str]:
    """One layer of decoding: percent-encoding, and base64/hex tokens (or the whole text)."""
    found: list[str] = []
    if "%" in text:
        found.extend((unquote(text), unquote_plus(text)))
    tokens: list[str] = []
    stripped = text.strip()
    if stripped and re.fullmatch(r"[A-Za-z0-9+/_=\s-]+", stripped):
        tokens.append(stripped)  # a whole multi-line base64 body
    tokens.extend(match.group(0) for match in _B64_TOKEN_RE.finditer(text))
    tokens.extend(match.group(0) for match in _HEX_TOKEN_RE.finditer(text))
    for token in tokens:
        found.extend(_decode_token(token))
    return found


def decoded_variants(payload: str) -> list[str]:
    """Readable texts hidden in a payload as base64/base64url, hex or percent-encoding,
    up to two layers deep (e.g. base64 of hex), bounded in count."""
    seen: set[str] = set()
    out: list[str] = []
    frontier = [str(payload or "")[:_MAX_DECODE_INPUT]]
    for _depth in range(_MAX_DECODE_DEPTH):
        next_layer: list[str] = []
        for text in frontier:
            for candidate in _decodings(text):
                if not candidate or candidate == text or candidate in seen:
                    continue
                seen.add(candidate)
                out.append(candidate)
                next_layer.append(candidate)
                if len(out) >= _MAX_DECODED_VARIANTS:
                    return out
        frontier = next_layer
        if not frontier:
            break
    return out


# Uploads (#236) are judged by the file's own bytes, not only its path. Printable runs cover text
# files and secrets stored inside binary ones; compressed formats hide their text either way.
# Bounded so a large video or archive cannot stall the call.
_UPLOAD_SCAN_BYTES = 8 * 1024 * 1024
_PRINTABLE_RUN_RE = re.compile(rb"[\t\n\r\x20-\x7e]{8,}")


def scan_upload_content(path: Any, known_fingerprints: Iterable[str]) -> Optional[str]:
    if not path:
        return None
    try:
        with open(Path(str(path)).expanduser(), "rb") as handle:
            raw = handle.read(_UPLOAD_SCAN_BYTES)
    except (OSError, ValueError):
        return None
    text = "\n".join(match.group(0).decode("ascii") for match in _PRINTABLE_RUN_RE.finditer(raw))
    if contains_direct_secret(text):
        return "artifact_content_secret"
    known = set(known_fingerprints)
    if not known:
        return None
    found = set(secret_fingerprints(text, include_entropy=True))
    found.update(_digest(line.strip()) for line in text.splitlines() if len(line.strip()) >= 8)
    found.update(_payload_candidate_fingerprints(text))
    whole = raw.decode("utf-8", errors="ignore").strip()
    if whole:
        found.add(_digest(whole))
    return "artifact_content_tainted_secret" if known.intersection(found) else None


def scan_sensitive_egress(
    tool: str,
    arguments: Mapping[str, Any],
    known_fingerprints: Iterable[str],
    *,
    clipboard_sensitive: bool = False,
    upload_content: Optional[str] = None,
) -> EgressScan:
    """upload_content is scan_upload_content's verdict, read by the caller outside its lock."""
    name = str(tool or "")
    if name not in _BROWSER_EGRESS_TOOLS:
        return EgressScan(False)

    if name == "browser_upload_artifact":
        path_class = sensitive_path_class(arguments.get("path"))
        if path_class:
            return EgressScan(True, "sensitive_artifact_path", path_class)
        if upload_content:
            return EgressScan(True, upload_content, "artifact_content")
        return EgressScan(False)

    if name == "browser_press_key":
        key = str(arguments.get("key") or "").strip().lower()
        modifiers = {str(item).strip().lower() for item in (arguments.get("modifiers") or [])}
        if clipboard_sensitive and key == "v" and modifiers.intersection({"cmd", "command", "meta"}):
            return EgressScan(True, "sensitive_clipboard_paste", "browser_paste")
        return EgressScan(False)

    known = set(known_fingerprints)
    for payload in _browser_payload_strings(name, arguments):
        payload = without_url_api_keys(payload)
        if not payload:
            continue
        for index, text in enumerate([payload, *decoded_variants(payload)]):
            text = without_url_api_keys(text)
            encoded = "encoded_" if index else ""
            if contains_direct_secret(text):
                return EgressScan(True, f"{encoded}direct_secret_pattern", "browser_payload")
            payload_fingerprints = set(secret_fingerprints(text, include_entropy=True, include_whole=True))
            payload_fingerprints.update(_payload_candidate_fingerprints(text))
            if known.intersection(payload_fingerprints):
                return EgressScan(True, f"{encoded}tainted_secret_match", "browser_payload")
    return EgressScan(False)


def safe_target_summary(tool: str, arguments: Mapping[str, Any], *, secret_egress: bool = False) -> str:
    name = str(tool or "tool")
    args = arguments or {}
    if secret_egress:
        if name == "browser_type_selector":
            selector = str(args.get("css_selector") or "field")[:80]
            return f"browser typing sensitive value → {selector}"
        if name in {"browser_act", "browser_do"}:
            return "browser action includes sensitive type/paste"
        if name == "browser_execute_js":
            return "browser JavaScript includes sensitive value"
        if name in {"browser_open_url", "open_url"}:
            return "external URL includes sensitive value"
        if name == "http_request":
            return "outbound HTTP request includes sensitive value"
        if name == "browser_press_key":
            return "browser paste from sensitive clipboard"
        if name == "browser_upload_artifact":
            return "browser upload of sensitive local artifact"
    if name in {"run_command", "start_background_job"}:
        raw_command = str(args.get("command") or "")
        lower = raw_command.lower()
        if any(marker in lower for marker in (
            "~/.ssh/", "/.ssh/", ".env", ".netrc", ".npmrc", ".pypirc",
            ".git-credentials", "credentials.json", "service-account.json",
            "security find-generic-password", "security find-internet-password",
        )):
            return "command: [sensitive credential source]"
        command = redact_sensitive_text(raw_command).replace("\n", " ").strip()
        return "command: " + (command[:180] + ("…" if len(command) > 180 else ""))
    if name == "run_commands_parallel":
        return f"parallel commands: {len(args.get('commands') or [])}"
    if name in {"read_file", "write_file", "edit_file", "delete_path", "get_file_info"}:
        path = str(args.get("path") or "")
        classification = sensitive_path_class(path)
        if classification:
            return f"{name}: [{classification}]"
        safe = redact_sensitive_text(path)
        return f"{name}: {safe[:180]}"
    if name.startswith("browser_"):
        return name.replace("_", " ")
    return name.replace("_", " ")


def safe_server_approval_target_summary(tool: str, arguments: Mapping[str, Any]) -> str:
    name = str(tool or "tool")
    args = arguments or {}
    if name in {"run_command", "start_job", "start_background_job"}:
        return "local shell command"
    if name == "run_commands_parallel":
        return f"{len(args.get('commands') or [])} local shell commands"
    if name == "mac_mcp_update":
        return "Mac MCP update operation"
    if name.startswith("browser_"):
        return name.replace("_", " ")
    if name.startswith("mac_") or name in {"click", "type_text", "press_key"}:
        return "native Mac interaction"
    if name == "http_request":
        return "outbound HTTP request"
    if name in {"delete_path", "move_file", "copy_file", "write_file", "edit_file", "write_files_batch"}:
        return name.replace("_", " ")
    return name.replace("_", " ")


def format_security_approval_question(payload: Mapping[str, Any]) -> str:
    origin = redact_sensitive_text(str(payload.get("origin") or "unknown origin"))[:180]
    title = " ".join(redact_sensitive_text(str(payload.get("tab_title") or "unknown")).split())[:140]
    handle = redact_sensitive_text(str(payload.get("tab_handle") or "unknown"))[:80]
    tool = redact_sensitive_text(str(payload.get("tool") or "unknown"))[:100]
    target = redact_sensitive_text(str(payload.get("target_summary") or tool or "host action"))[:220]
    reason = redact_sensitive_text(str(payload.get("reason_code") or "web_host_boundary"))[:100]
    if reason == "server_risk_profile":
        return (
            "Mac MCP's Server Approval profile requires confirmation for this high-risk action.\n\n"
            f"Requested action: {tool}\n"
            f"Target: {target}\n"
            "Approval source: Mac MCP server\n\n"
            "Allow this exact action once? A different command, target, or action will require a new approval."
        )
    return (
        "A web-scoped session is requesting a protected action.\n\n"
        f"Source origin: {origin}\n"
        f"Untrusted tab title: {title}\n"
        f"Tab handle: {handle}\n"
        f"Requested action: {tool}\n"
        f"Target: {target}\n"
        f"Reason: {reason}\n\n"
        "Allow this exact action once? Navigation, a different command/path, or another transfer will require a new approval."
    )


def action_fingerprint(tool: str, arguments: Mapping[str, Any]) -> str:
    payload = json.dumps({"tool": str(tool), "arguments": arguments}, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return _digest(payload)



def sanitize_tool_arguments(tool: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Return a telemetry-safe copy of tool arguments.

    Content-bearing fields that can carry credentials are intentionally hidden even
    when a value does not match a known token pattern. This keeps tainted arbitrary
    secrets out of call-start events and REST telemetry.
    """
    name = str(tool or "")
    data = dict(arguments or {})
    if name == "browser_type_selector" and "text" in data:
        data["text"] = "[BROWSER INPUT REDACTED]"
    elif name in {"browser_act", "browser_do"}:
        actions = []
        for action in data.get("actions") or []:
            if not isinstance(action, Mapping):
                actions.append(action)
                continue
            copy = dict(action)
            typ = str(copy.get("type") or "").lower().replace("-", "_")
            if typ in {"type", "type_text", "paste", "post"}:
                for key in ("text", "value", "content"):
                    if key in copy:
                        copy[key] = "[BROWSER INPUT REDACTED]"
            if typ == "fill" and isinstance(copy.get("values"), Mapping):
                # Field labels stay readable; what goes into them does not.
                copy["values"] = {str(label): "[BROWSER INPUT REDACTED]" for label in copy["values"]}
            actions.append(copy)
        if "actions" in data:
            data["actions"] = actions
    elif name == "browser_execute_js" and "js" in data:
        data["js"] = "[JAVASCRIPT REDACTED]"
    elif name == "browser_open_url" and data.get("url"):
        origin = SecuritySafeURL.origin(str(data.get("url") or ""))
        data["url"] = (origin + "/[URL DETAILS REDACTED]") if origin else "[URL REDACTED]"
    elif name == "clipboard_set" and "content" in data:
        data["content"] = "[CLIPBOARD CONTENT REDACTED]"
    elif name == "write_file" and "content" in data:
        data["content"] = "[FILE CONTENT REDACTED]"
    elif name == "write_files_batch" and isinstance(data.get("files"), list):
        redacted_files = []
        for item in data["files"]:
            if isinstance(item, Mapping):
                copy = dict(item)
                if "content" in copy:
                    copy["content"] = "[FILE CONTENT REDACTED]"
                redacted_files.append(copy)
            else:
                redacted_files.append(item)
        data["files"] = redacted_files
    elif name == "edit_file":
        for key in ("old_string", "new_string"):
            if key in data:
                data[key] = "[FILE EDIT CONTENT REDACTED]"
    elif name == "http_request":
        if data.get("url"):
            origin = SecuritySafeURL.origin(str(data.get("url") or ""))
            data["url"] = (origin + "/[URL DETAILS REDACTED]") if origin else "[URL REDACTED]"
        if "body" in data and data.get("body") is not None:
            data["body"] = "[HTTP BODY REDACTED]"
        if isinstance(data.get("headers"), Mapping):
            data["headers"] = {str(key): "[HTTP HEADER REDACTED]" for key in data["headers"]}
    return data


class SecuritySafeURL:
    @staticmethod
    def origin(url: str) -> Optional[str]:
        try:
            parsed = __import__("urllib.parse", fromlist=["urlsplit"]).urlsplit(url)
        except ValueError:
            return None
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        host = parsed.hostname.lower().rstrip(".")
        default_port = 443 if parsed.scheme == "https" else 80
        try:
            port = parsed.port
        except ValueError:
            return None
        suffix = f":{port}" if port and port != default_port else ""
        return f"{parsed.scheme}://{host}{suffix}"

def redact_sensitive_source_result(tool: str, arguments: Mapping[str, Any], result: Any) -> Any:
    """Return a telemetry-safe result when a host read produced sensitive material.

    This intentionally favors metadata over content. The execution result returned to
    the MCP caller is unchanged; only observability persistence uses this view.
    """
    scan = scan_sensitive_source(tool, arguments, result)
    if not scan.sensitive:
        return result
    name = str(tool or "")
    if not isinstance(result, Mapping):
        return "[SENSITIVE OUTPUT REDACTED]"
    safe = dict(result)
    if name == "read_file":
        if "content" in safe:
            safe["content"] = "[SENSITIVE OUTPUT REDACTED]"
        return safe
    if name == "read_multiple_files":
        files: list[Any] = []
        for item in safe.get("files") or []:
            if isinstance(item, Mapping):
                copy = dict(item)
                if "content" in copy:
                    copy["content"] = "[SENSITIVE OUTPUT REDACTED]"
                files.append(copy)
            else:
                files.append("[SENSITIVE OUTPUT REDACTED]")
        safe["files"] = files
        return safe
    if name == "clipboard_get":
        if "content" in safe:
            safe["content"] = "[SENSITIVE OUTPUT REDACTED]"
        return safe
    for key in ("stdout", "stderr", "output", "content", "text", "result", "results"):
        if key in safe and isinstance(safe[key], str):
            safe[key] = "[SENSITIVE OUTPUT REDACTED]"
    if name in {"run_commands_parallel", "wait_jobs", "process_list", "get_agent", "memory_get", "memory_search",
                "knowledge_search", "list_jobs", "get_job_status"}:
        # Nested command/job outputs can vary by provider; once a result is known
        # sensitive, do not persist nested free-form strings.
        def scrub(value: Any, depth: int = 0) -> Any:
            if depth > 6:
                return "[nested value omitted]"
            if isinstance(value, str):
                return "[SENSITIVE OUTPUT REDACTED]"
            if isinstance(value, Mapping):
                return {str(k): scrub(v, depth + 1) for k, v in value.items()}
            if isinstance(value, list):
                return [scrub(v, depth + 1) for v in value]
            return value
        safe = scrub(safe)
    return safe
