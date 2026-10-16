"""An optional usage limit for a scheduled AI run (#211): stop the agent at N tokens or $N.

Off unless the owner sets it. A limit is only offered where Mac MCP can see the usage while
the agent works, because a limit checked after the run cannot stop anything:

- tokens: Claude Code (each message), OpenCode and Kilo (each step), pi (each turn);
- cost:   OpenCode and Kilo (each step reports its price).

Codex, Cline, Goose, Grok Build and Antigravity report usage only when they finish, and
Claude Code reports its price only then, so a limit they cannot keep is refused with a plain
reason instead of being promised. The time limit stays the fail-safe for every agent.

The meter reads the agent's saved event stream from where it left off, so a check costs only
the lines written since the last one.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

LIVE_TOKENS = frozenset({"claude", "opencode", "kilo", "pi"})
LIVE_COST = frozenset({"opencode", "kilo"})
MAX_TOKENS = 1_000_000_000
MAX_COST_USD = 10_000.0
_READ_CHUNK = 4 * 1024 * 1024


class BudgetError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def validate(raw: Any) -> Optional[Dict[str, Any]]:
    """{"max_tokens": int, "max_cost_usd": float} with either one set, or None for no limit."""
    if raw is None or raw == {}:
        return None
    if not isinstance(raw, dict):
        raise BudgetError("usage_limit_invalid", "The usage limit must be {max_tokens, max_cost_usd}.")
    out: Dict[str, Any] = {}
    tokens = raw.get("max_tokens")
    if tokens not in (None, 0, ""):
        if not isinstance(tokens, int) or isinstance(tokens, bool) or not 1_000 <= tokens <= MAX_TOKENS:
            raise BudgetError("usage_limit_tokens_invalid", "The token limit must be a whole number from 1,000.")
        out["max_tokens"] = tokens
    cost = raw.get("max_cost_usd")
    if cost not in (None, 0, ""):
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not 0.01 <= float(cost) <= MAX_COST_USD:
            raise BudgetError("usage_limit_cost_invalid", "The cost limit must be at least $0.01.")
        out["max_cost_usd"] = round(float(cost), 2)
    return out or None


def support(provider: Optional[str]) -> Dict[str, bool]:
    key = str(provider or "").lower()
    return {"tokens": key in LIVE_TOKENS, "cost": key in LIVE_COST}


def unenforceable(budget: Optional[Dict[str, Any]], provider: Optional[str], label: Optional[str] = None) -> Optional[str]:
    """Why this agent cannot keep this limit, or None when it can (or there is no limit)."""
    if not budget:
        return None
    if not provider:
        return "Choose an agent for this task: the usage limit depends on what the agent reports while it works."
    can = support(provider)
    name = label or provider
    if budget.get("max_tokens") and not can["tokens"]:
        return (f"{name} reports its token use only when it finishes, so Mac MCP can't stop it at a token limit. "
                "Use the time limit, or an agent that reports as it works (Claude Code, OpenCode, Kilo Code or pi).")
    if budget.get("max_cost_usd") and not can["cost"]:
        return (f"{name} doesn't report its price while it works, so Mac MCP can't stop it at a cost limit. "
                "Use a token limit or the time limit, or OpenCode or Kilo Code, which report the price of each step.")
    return None


def default_provider() -> Optional[str]:
    """The Default Agent from Settings › Subagents, read without checking that it can start."""
    from .runtime_settings import load_runtime_settings_state

    state = load_runtime_settings_state()
    subagents = state.data.get("subagents") if state.ok else None
    preset = subagents.get("default") if isinstance(subagents, dict) else None
    provider = preset.get("provider") if isinstance(preset, dict) else None
    return str(provider).strip().lower() or None if isinstance(provider, str) else None


def describe(budget: Optional[Dict[str, Any]]) -> str:
    parts = []
    if budget and budget.get("max_tokens"):
        parts.append(f"{budget['max_tokens']:,} tokens")
    if budget and budget.get("max_cost_usd"):
        parts.append(f"${budget['max_cost_usd']:,.2f}")
    return " or ".join(parts)


class Meter:
    """Usage so far from an agent's stdout.log, read incrementally."""

    def __init__(self, path: Path, provider: str):
        self.path = path
        self.provider = str(provider or "").lower()
        self._offset = 0
        self._partial = b""
        self._tokens: Dict[str, int] = {}
        self._cost: Dict[str, float] = {}
        self._unpriced: set = set()  # steps that reported tokens but no price
        self._final: Optional[Dict[str, Any]] = None

    def read(self) -> Dict[str, Any]:
        try:
            with self.path.open("rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read(_READ_CHUNK)
        except OSError:
            chunk = b""
        if chunk:
            self._offset += len(chunk)
            lines = (self._partial + chunk).split(b"\n")
            self._partial = lines.pop()  # a line still being written
            for line in lines:
                self._event(line)
        return self.usage()

    def usage(self) -> Dict[str, Any]:
        if self._final is not None:
            return {**self._final, "cost_unknown": False}
        cost = round(sum(self._cost.values()), 4) if self._cost else None
        return {"tokens": sum(self._tokens.values()), "cost_usd": cost, "cost_unknown": bool(self._unpriced)}

    def _event(self, line: bytes) -> None:
        try:
            event = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(event, dict):
            return
        kind = str(event.get("type") or "")
        if self.provider == "claude":
            self._claude(event, kind)
        elif self.provider in {"opencode", "kilo"} and kind == "step_finish" and isinstance(event.get("part"), dict):
            self._step(event["part"])
        elif self.provider == "pi" and kind == "turn_end":
            from .extra_harness_providers import usage_report

            report = usage_report("pi", event)
            if report:
                event_id, counts = report
                total = counts["total"]
                if total is None and counts["input"] is not None and counts["output"] is not None:
                    total = counts["input"] + counts["output"]
                if total is not None:
                    self._tokens[event_id] = total

    def _claude(self, event: Dict[str, Any], kind: str) -> None:
        from .provider_usage import normalize_claude_usage

        if kind == "assistant" and isinstance(event.get("message"), dict):
            message = event["message"]
            total = normalize_claude_usage(message.get("usage")).total_tokens
            key = str(message.get("id") or "")
            if key and total is not None:
                # One message streams as several events with the same id; the last one counts.
                self._tokens[key] = max(total, self._tokens.get(key, 0))
        elif kind == "result":
            total = normalize_claude_usage(event.get("usage")).total_tokens
            if total is not None:
                cost = event.get("total_cost_usd")
                self._final = {"tokens": total, "cost_usd": float(cost) if isinstance(cost, (int, float)) else None}

    def _step(self, part: Dict[str, Any]) -> None:
        from .provider_usage import normalize_opencode_usage

        key = str(part.get("id") or len(self._tokens))
        normalized = normalize_opencode_usage(part.get("tokens"))
        total = normalized.total_tokens
        if total is None and normalized.input_tokens is not None and normalized.output_tokens is not None:
            total = normalized.input_tokens + normalized.output_tokens  # Kilo: input already counts the cache
        if total is not None:
            self._tokens[key] = total
        cost = part.get("cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            self._cost[key] = float(cost)
            self._unpriced.discard(key)
        elif total:
            self._unpriced.add(key)


def over(budget: Optional[Dict[str, Any]], usage: Dict[str, Any]) -> Optional[str]:
    """'tokens' or 'cost' once a limit is reached, 'cost_unknown' when a cost limit can no longer
    be kept (a step came without its price), else None."""
    if not budget:
        return None
    if budget.get("max_tokens") and int(usage.get("tokens") or 0) >= budget["max_tokens"]:
        return "tokens"
    if budget.get("max_cost_usd") and usage.get("cost_unknown"):
        return "cost_unknown"  # fail closed: an unpriced step would let spending pass the limit unseen
    if budget.get("max_cost_usd") and usage.get("cost_usd") is not None and float(usage["cost_usd"]) >= budget["max_cost_usd"]:
        return "cost"
    return None


def stop_reason(budget: Dict[str, Any], limit: str, usage: Dict[str, Any], label: Optional[str] = None) -> str:
    if limit == "cost_unknown":
        return (f"Stopped: {label or 'the agent'} didn't report a price for its work, so the "
                f"${budget['max_cost_usd']:,.2f} cost limit couldn't be kept (used {int(usage.get('tokens') or 0):,} tokens)")
    if limit == "tokens":
        return f"Stopped at its {budget['max_tokens']:,}-token limit (used {int(usage.get('tokens') or 0):,})"
    return f"Stopped at its ${budget['max_cost_usd']:,.2f} cost limit (used ${float(usage.get('cost_usd') or 0):,.2f})"
