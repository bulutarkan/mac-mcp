from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

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

_SOURCE_TOOLS = frozenset({
    "read_file", "read_multiple_files", "clipboard_get", "run_command",
    "run_commands_parallel", "get_job_output", "wait_jobs",
})
_BROWSER_EGRESS_TOOLS = frozenset({
    "browser_open_url", "browser_act", "browser_do", "browser_execute_js",
    "browser_type_selector", "browser_press_key", "open_url", "http_request",
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


def sensitive_path_class(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
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


def secret_fingerprints(text: str, *, include_entropy: bool = True, include_whole: bool = False) -> frozenset[str]:
    fingerprints = {_digest(candidate) for candidate in _direct_secret_candidates(text)}
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


def scan_sensitive_source(tool: str, arguments: Mapping[str, Any], result: Any) -> SensitiveSourceScan:
    name = str(tool or "")
    if name not in _SOURCE_TOOLS:
        return SensitiveSourceScan(False)

    source_class = sensitive_path_class(_result_path(arguments, result))
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
        values = [str(arguments.get("url") or ""), str(arguments.get("body") or "")]
        headers = arguments.get("headers") or {}
        if isinstance(headers, Mapping):
            values.extend(str(value or "") for value in headers.values())
        return values
    if name in {"browser_act", "browser_do"}:
        values: list[str] = []
        for action in arguments.get("actions") or []:
            if not isinstance(action, Mapping):
                continue
            typ = str(action.get("type") or "").lower().replace("-", "_")
            if typ not in {"type", "type_text", "paste"}:
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


def scan_sensitive_egress(
    tool: str,
    arguments: Mapping[str, Any],
    known_fingerprints: Iterable[str],
    *,
    clipboard_sensitive: bool = False,
) -> EgressScan:
    name = str(tool or "")
    if name not in _BROWSER_EGRESS_TOOLS:
        return EgressScan(False)

    if name == "browser_press_key":
        key = str(arguments.get("key") or "").strip().lower()
        modifiers = {str(item).strip().lower() for item in (arguments.get("modifiers") or [])}
        if clipboard_sensitive and key == "v" and modifiers.intersection({"cmd", "command", "meta"}):
            return EgressScan(True, "sensitive_clipboard_paste", "browser_paste")
        return EgressScan(False)

    known = set(known_fingerprints)
    for payload in _browser_payload_strings(name, arguments):
        if not payload:
            continue
        if contains_direct_secret(payload):
            return EgressScan(True, "direct_secret_pattern", "browser_payload")
        payload_fingerprints = set(secret_fingerprints(payload, include_entropy=True, include_whole=True))
        payload_fingerprints.update(_payload_candidate_fingerprints(payload))
        if known.intersection(payload_fingerprints):
            return EgressScan(True, "tainted_secret_match", "browser_payload")
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


def format_security_approval_question(payload: Mapping[str, Any]) -> str:
    origin = redact_sensitive_text(str(payload.get("origin") or "unknown origin"))[:180]
    title = " ".join(redact_sensitive_text(str(payload.get("tab_title") or "unknown")).split())[:140]
    handle = redact_sensitive_text(str(payload.get("tab_handle") or "unknown"))[:80]
    tool = redact_sensitive_text(str(payload.get("tool") or "unknown"))[:100]
    target = redact_sensitive_text(str(payload.get("target_summary") or tool or "host action"))[:220]
    reason = redact_sensitive_text(str(payload.get("reason_code") or "web_host_boundary"))[:100]
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
            if typ in {"type", "type_text", "paste"}:
                for key in ("text", "value", "content"):
                    if key in copy:
                        copy[key] = "[BROWSER INPUT REDACTED]"
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
        files = []
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
    for key in ("stdout", "stderr", "output", "content", "text", "result"):
        if key in safe and isinstance(safe[key], str):
            safe[key] = "[SENSITIVE OUTPUT REDACTED]"
    if name in {"run_commands_parallel", "wait_jobs"}:
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
