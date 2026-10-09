"""Ranked search over the tools the caller may use (tool_discover).

Discovery answers "which tool should I use?": every visible tool is scored
against the query using its name, a short index of use cases and synonyms, and
its description, and the best matches come first with the reason they matched.
Results are paged with an opaque cursor so a long result never stops silently
at the limit. Ranking only reorders the list it is given; profile and
delegated-scope filtering happen before it.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

_STOPWORDS = frozenset({
    "a", "an", "the", "to", "of", "for", "in", "on", "at", "by", "with", "and", "or", "my", "me", "i", "it",
    "how", "do", "does", "what", "which", "tool", "tools", "use", "can", "is", "are", "be", "from", "into",
    "some", "this", "that", "please", "want", "need",
})

# Common task phrases and synonyms per tool. Words here count almost as much
# as words in the tool name; keep each entry to what a user would actually say.
TOOL_USE_CASES: Dict[str, Tuple[str, ...]] = {
    "browser_observe": ("read browser page", "see web page", "page content", "inspect website", "look at tab", "dom"),
    "browser_act": ("click button on page", "fill form", "type into web field", "submit form", "select dropdown",
                    "interact with website"),
    "browser_do": ("open url and extract", "one shot web research", "scrape page", "get price from website",
                   "visit page read fields"),
    "browser_find": ("find element on page", "locate button", "search page text"),
    "browser_list_tabs": ("list browser tabs", "open tabs", "tab handle", "which tabs"),
    "browser_close_tab": ("close tab",),
    "browser_screenshot": ("screenshot web page", "capture page image"),
    "browser_wait_for_download": ("wait download file browser",),
    "browser_checkpoint": ("sign in", "login 2fa", "two factor code", "otp verification", "captcha", "passkey",
                           "let the user log in"),
    "browser_upload_artifact": ("upload file to website", "attach file in browser"),
    "run_command": ("run shell command", "execute terminal", "bash zsh", "cli command"),
    "start_background_job": ("run long command", "background process", "start server", "long build", "watcher",
                             "dev server"),
    "get_job_output": ("background job output", "job logs"),
    "wait_jobs": ("wait for background job",),
    "stop_job": ("stop background job", "cancel job"),
    "run_commands_parallel": ("run several commands at once", "parallel shell"),
    "read_file": ("read file", "open file contents", "cat file"),
    "read_multiple_files": ("read several files", "read many files", "multiple files at once", "batch read"),
    "write_file": ("write file", "create file", "save text to file"),
    "write_files_batch": ("write several files", "write many files"),
    "edit_file": ("edit file", "replace text in file", "patch file"),
    "list_directory": ("list folder", "directory contents", "ls"),
    "directory_tree": ("folder tree", "directory structure"),
    "find_files": ("find files by name", "glob files"),
    "search_files": ("search file contents", "grep", "find text in files"),
    "spotlight_search": ("spotlight", "search mac for file"),
    "delete_path": ("delete file", "remove folder", "rm"),
    "move_file": ("move file", "rename file"),
    "copy_file": ("copy file", "duplicate file"),
    "http_request": ("http api call", "fetch url", "curl", "rest request"),
    "mac_snapshot": ("what is on screen", "overview of mac state", "open windows"),
    "mac_observe": ("read app ui", "native app elements", "accessibility tree"),
    "mac_act": ("click in native app", "type in mac app", "press button in app"),
    "mac_app": ("app specific action", "send email", "mail notes calendar finder reminders"),
    "open_app": ("launch app", "open application", "start app"),
    "screenshot": ("take screenshot", "screen capture"),
    "clipboard_get": ("read clipboard", "paste contents"),
    "clipboard_set": ("copy to clipboard",),
    "spawn_agents": ("start subagents", "delegate task", "parallel agents", "team of agents"),
    "wait_agents": ("wait for agents", "collect agent results"),
    "memory_search": ("recall memory", "remember fact", "search memory"),
    "lesson_search": ("past lessons", "what worked before"),
    "ask_user": ("ask the user", "question to human", "get confirmation"),
    "set_reminder": ("create reminder", "remind me"),
    "send_notification": ("show notification", "notify"),
    "process_list": ("running processes", "ps"),
    "kill_process": ("kill process", "terminate pid"),
    "get_system_info": ("system info", "cpu memory disk"),
}

DEFAULT_LIMIT = 8
MAX_LIMIT = 100


class CursorError(ValueError):
    """The cursor is malformed or was issued for a different query."""


def _stem(word: str) -> str:
    for suffix in ("ing", "es", "ed", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def _words(text: str) -> List[str]:
    return [word for word in re.split(r"[^a-z0-9]+", str(text or "").lower()) if word]


def query_tokens(query: str) -> List[str]:
    tokens = [_stem(word) for word in _words(query) if word not in _STOPWORDS]
    return list(dict.fromkeys(tokens))


@dataclass
class Match:
    name: str
    score: float
    reasons: List[str] = field(default_factory=list)
    order: int = 0


def score_tool(name: str, description: str, tokens: Sequence[str], query: str) -> Match:
    match = Match(name=name, score=0.0)
    q = str(query or "").strip().lower()
    if not tokens and not q:
        return match
    if q.replace(" ", "_") == name or q == name:
        match.score += 100
        match.reasons.append("exact tool name")
    name_words = {_stem(word) for word in name.split("_")}
    use_cases = TOOL_USE_CASES.get(name, ())
    use_case_words = {_stem(word) for phrase in use_cases for word in _words(phrase)}
    description_words = {_stem(word) for word in _words(description)}
    in_name = [token for token in tokens if token in name_words]
    in_use = [token for token in tokens if token in use_case_words and token not in name_words]
    in_desc = [token for token in tokens if token in description_words and token not in name_words
               and token not in use_case_words]
    match.score += 8 * len(in_name) + 5 * len(in_use) + 2 * len(in_desc)
    if tokens and len(set(in_name) | set(in_use) | set(in_desc)) == len(tokens):
        match.score += 6  # every query word is accounted for
    phrase = next((p for p in use_cases if q and (p in q or q in p)), None)
    if phrase:
        match.score += 12
        match.reasons.append(f"use case '{phrase}'")
    if in_name:
        match.reasons.append("name matches " + ", ".join(repr(t) for t in in_name))
    if in_use and not phrase:
        match.reasons.append("use cases mention " + ", ".join(repr(t) for t in in_use))
    if in_desc:
        match.reasons.append("description mentions " + ", ".join(repr(t) for t in in_desc))
    return match


def rank(entries: Iterable[Tuple[str, str]], query: str, *, core: Iterable[str] = (),
         demoted: Iterable[str] = ()) -> List[Match]:
    """Score (name, description) pairs; an empty query keeps registry order."""
    tokens = query_tokens(query)
    core_set, demoted_set = set(core), set(demoted)
    blank = not str(query or "").strip()
    matches: List[Match] = []
    for order, (name, description) in enumerate(entries):
        match = score_tool(name, description, tokens, query)
        match.order = order
        if not blank and match.score <= 0:
            continue
        if not blank:
            # Ties prefer the default catalog over compatibility primitives.
            match.score += 0.5 if name in core_set else 0.0
            match.score -= 1.0 if name in demoted_set else 0.0
        matches.append(match)
    if not blank:
        matches.sort(key=lambda item: (-item.score, item.order))
    return matches


def _query_key(query: str) -> str:
    return hashlib.sha256(str(query or "").strip().lower().encode("utf-8")).hexdigest()[:10]


def encode_cursor(query: str, offset: int) -> str:
    return f"{int(offset)}.{_query_key(query)}"


def decode_cursor(cursor: Optional[str], query: str) -> int:
    if not cursor:
        return 0
    offset, _, key = str(cursor).partition(".")
    if not offset.isdigit() or key != _query_key(query):
        raise CursorError("cursor does not belong to this query; repeat tool_discover without a cursor")
    return int(offset)


def short_description(full: str, summary: Optional[str], limit: int = 320) -> Tuple[str, bool]:
    """A complete short description: the hand-written summary, or whole sentences up to limit."""
    full = str(full or "").strip()
    if summary:
        return summary, len(full) > len(summary)
    if len(full) <= limit:
        return full, False
    sentences = re.split(r"(?<=[.!?])\s+", full)
    kept = ""
    for sentence in sentences:
        candidate = f"{kept} {sentence}".strip()
        if len(candidate) > limit:
            break
        kept = candidate
    if not kept:
        kept = full[:limit].rsplit(" ", 1)[0]
    return kept, True


_CONSTRAINT_KEYS = ("enum", "minimum", "maximum", "minItems", "maxItems", "minLength", "maxLength")


def parameter_summary(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Type, default and bounds of one input property, looking through Optional[...] anyOf."""
    out: Dict[str, Any] = {"type": spec.get("type"), "default": spec.get("default")}
    variants = [spec] + [item for item in spec.get("anyOf") or [] if isinstance(item, dict)]
    if out["type"] is None:
        types = [item.get("type") for item in variants[1:] if item.get("type") and item.get("type") != "null"]
        if types:
            out["type"] = types[0] if len(types) == 1 else types
    for variant in variants:
        for key in _CONSTRAINT_KEYS:
            if key in variant and key not in out:
                out[key] = variant[key]
    return out
