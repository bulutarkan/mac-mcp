"""Themed screens of the CLI, built from the data the commands already compute.

Only used when the theme is on (see cli_style); every command keeps its plain output otherwise.
"""
from __future__ import annotations

import argparse
import sys
import textwrap
import time
from typing import Optional
from urllib.parse import urlparse

from .cli_style import INDENT, SYMBOLS, Style, home_path

GROUPS = [
    ("Everyday", ["status", "start", "stop", "restart", "update"]),
    ("Connect", ["clients", "connect", "connect-config"]),
    ("Troubleshoot", ["doctor", "logs", "repair"]),
]
ADVANCED = ["dashboard", "privacy", "recipe", "logins", "catalog", "credential", "autostart", "conformance", "uninstall"]

SHORT_HELP = {
    "status": "How Mac MCP is doing", "start": "Start the server", "stop": "Stop the server",
    "restart": "Restart the server", "update": "Update to the latest stable release",
    "clients": "AI apps on this Mac", "connect": "Add Mac MCP to an AI app",
    "connect-config": "A connection snippet for an app", "doctor": "Check everything",
    "logs": "Recent log lines", "repair": "Fix the parts Mac MCP manages",
    "dashboard": "Open the dashboard", "privacy": "Stored data", "recipe": "Saved recipes",
    "logins": "Saved logins", "catalog": "Skill packs", "credential": "Tunnel credentials",
    "autostart": "Start at login", "conformance": "Computer Use checks", "uninstall": "Remove Mac MCP",
}

EXAMPLES = {
    "status": "mac-mcp status --local-only", "start": "mac-mcp start", "stop": "mac-mcp stop",
    "restart": "mac-mcp restart", "update": "mac-mcp update --check", "clients": "mac-mcp clients",
    "connect": "mac-mcp connect --client claude-code", "connect-config": "mac-mcp connect-config --client codex",
    "doctor": "mac-mcp doctor --verbose", "logs": "mac-mcp logs server -n 40", "repair": "mac-mcp repair",
    "dashboard": "mac-mcp dashboard", "privacy": "mac-mcp privacy", "recipe": "mac-mcp recipe list",
    "logins": "mac-mcp logins list", "catalog": "mac-mcp catalog list", "credential": "mac-mcp credential cloudflare status",
    "autostart": "mac-mcp autostart on", "conformance": "mac-mcp conformance", "uninstall": "mac-mcp uninstall",
}


def render_home(style: Style, version: str, running: Optional[bool]) -> None:
    status = ""
    if running is not None:
        status = (style.paint(SYMBOLS["dot"], "ok") + " Running") if running else (style.paint(SYMBOLS["dot"], "error") + " Stopped")
    style.header(version, extra=status)
    width = max(len(name) for _, names in GROUPS for name in names)
    for title, names in GROUPS:
        style.write(f"{INDENT}{style.paint(title, bold=True)}")
        lead = len(INDENT) + 2 + width + 2
        for name in names:
            # A description wraps under itself on a narrow terminal.
            parts = textwrap.wrap(SHORT_HELP[name], max(12, style.width - lead)) or [""]
            style.write(f"{INDENT}  {style.paint(name.ljust(width), 'brand')}  {parts[0]}")
            for part in parts[1:]:
                style.write(" " * lead + part)
        style.write()
    # The advanced commands wrap under their own start, never back to the margin.
    lead = len(INDENT) + len("Advanced  ")
    lines = textwrap.wrap(" · ".join(ADVANCED), max(20, style.width - lead), break_on_hyphens=False)
    for index, line in enumerate(lines):
        line = line.replace(" · ", f" {style.paint('·', dim=True)} ")
        head = f"{INDENT}{style.paint('Advanced', bold=True)}  " if index == 0 else " " * lead
        style.write(head + line)
    style.write()
    style.write(f"{INDENT}{style.paint('mac-mcp <command> --help for more.', dim=True)}")


def render_command_help(style: Style, parser: argparse.ArgumentParser) -> None:
    """A command's --help in the theme: a description, its options, one example."""
    command = parser.prog.split(" ", 1)[1] if " " in parser.prog else ""
    top = command.split(" ")[0]
    style.header(command or "help")
    # A subcommand's text is its help in the parent's list; argparse's own help never shows it.
    about = parser.description or getattr(parser, "summary", "")
    if about:
        for line in textwrap.wrap(about, style.width - len(INDENT)):
            style.write(f"{INDENT}{line}")
        style.write()
    name_room = max(20, style.width - len(INDENT) - 2)
    help_room = max(20, style.width - len(INDENT) - 6)
    options = [action for action in parser._actions if action.help != argparse.SUPPRESS]
    if options:
        style.write(f"{INDENT}{style.paint('Options', bold=True)}")
        for action in options:
            names = ", ".join(action.option_strings) or action.dest
            if action.choices and not action.option_strings:
                names = "{" + ",".join(map(str, action.choices)) + "}"
            for part in textwrap.wrap(names, name_room, break_on_hyphens=False) or [names]:
                style.write(f"{INDENT}  {style.paint(part, 'brand')}")
            if action.help:
                text = action.help % vars(action) if "%(" in action.help else action.help
                for part in textwrap.wrap(text, help_room):
                    style.write(f"{INDENT}      {style.paint(part, dim=True)}")
    example = EXAMPLES.get(top)
    if example:
        style.write()
        style.write(f"{INDENT}{style.paint('Example', bold=True)}  {example}")


_MODE_NAMES = {"cloudflare": "Cloudflare", "ngrok": "ngrok", "custom": "custom HTTPS"}


def _host(url: Optional[str]) -> str:
    return urlparse(url).netloc if url else ""


def render_status(style: Style, report: dict) -> None:
    server, public = report["server"], report["public_endpoint"]
    state = report["state"]
    rows = []
    if server["running"] and server.get("health") != "unreachable":
        rows.append(("ok", "Running", f"pid {server['pid']}", f"port {server['port']}"))
    elif server["running"]:
        rows.append(("warn", "Not answering", f"pid {server['pid']}", "/health does not answer"))
    elif state == "port_conflict":
        owners = ", ".join(f"{o['name'] or 'pid'} ({o['pid']})" for o in server.get("port_owners") or []) or "another program"
        rows.append(("error", "Port in use", f"port {server['port']}", f"used by {owners}"))
    elif state == "ownership_unverified":
        rows.append(("warn", "Not verified", "a server is running that Mac MCP cannot verify", ""))
    else:
        rows.append(("error", "Stopped", f"not answering on port {server['port']}", ""))

    mode = public.get("mode")
    if public.get("error"):
        rows.append(("error", "Public address", "configuration error", str(public["error"])))
    elif mode in (None, "none"):
        rows.append((None, "Public address", "local only", "AI apps on this Mac"))
    else:
        detail = _MODE_NAMES.get(mode, mode)
        tone = "ok"
        if public.get("tunnel_running") is False:
            detail, tone = f"{detail} · tunnel not running", "warn"
        elif public.get("route") == "unreachable":
            detail, tone = f"{detail} · does not answer", "warn"
        rows.append((tone, "Public address", _host(public.get("url")), detail))

    supervisor = report.get("supervisor") or {}
    if not supervisor.get("enabled"):
        rows.append((None, "Crash guard", "off", ""))
    elif not supervisor.get("loaded"):
        rows.append(("warn", "Crash guard", "not loaded", "mac-mcp start loads it"))
    else:
        recovery = supervisor.get("last_recovery") or {}
        detail = ""
        if recovery:
            detail = "last recovery " + time.strftime("%b %-d", time.localtime(float(recovery.get("at") or 0)))
        if supervisor.get("backoff_until"):
            detail = "backing off after repeated failures"
        rows.append(("ok", "Crash guard", "watching", detail))

    privacy = report.get("privacy") or {}
    if privacy.get("paused"):
        rows.append(("warn", "Privacy", "observation paused", "Settings › Privacy"))
    elif privacy.get("private_mode"):
        rows.append(("info", "Privacy", "private mode on", "nothing about this work is kept"))
    for stray in report.get("stray_processes") or []:
        rows.append(("warn", "Stray process", f"{stray['name']} pid {stray['pid']}", "not the selected mode"))

    style.header("status")
    style.rows(rows)
    style.write()
    style.write(f"{INDENT}{style.paint('Logs', bold=True)}  {style.paint(home_path(server['log']), dim=True)}")

    remediation = (report.get("remediation") or [""])[0]
    if state == "healthy":
        local = mode in (None, "none")
        style.summary("ok", "All good — AI apps on this Mac can reach it." if local else "All good — AI apps can reach this Mac.")
    elif state == "stopped":
        style.summary("error", "Mac MCP is not running", next_command="mac-mcp start")
    elif state in ("unresponsive", "ownership_unverified"):
        style.summary("error", "Mac MCP is not answering", next_command="mac-mcp restart")
    elif state == "port_conflict":
        style.summary("error", remediation or f"Port {server['port']} is taken")
    elif state == "degraded" and report.get("exit_code") == 0:
        # status --local-only: the local server is what was asked about.
        style.summary("ok", "Running here — the public address is not available, and this check leaves it out.")
    else:
        style.summary("warn", remediation or "The public address is not available")


DOCTOR_GROUPS = {
    "runtime": "Runtime", "cli": "Runtime", "state": "Runtime", "system": "Runtime",
    "server": "Server", "process": "Server", "network": "Server", "auth": "Server", "config": "Server", "security": "Server",
    "permissions": "Permissions", "dependencies": "Dependencies", "companion": "Dependencies",
    "agents": "Agents", "update": "Updates",
}
_GROUP_ORDER = ["Runtime", "Server", "Permissions", "Dependencies", "Agents", "Updates", "Other"]
# Groups whose passes read better as names than as a count.
_NAMED_PASSES = {"Permissions", "Dependencies"}
_TONES = {"pass": "ok", "warn": "warn", "fail": "error", "info": "info"}


_TOOL_NAMES = {"osascript", "cliclick", "cloudflared", "ngrok"}


def _pass_name(check_id: str) -> str:
    """Tools keep their own lower-case name; the rest read as words (Accessibility)."""
    name = check_id.split(".", 1)[-1].replace("_", " ")
    return name if name in _TOOL_NAMES else name[:1].upper() + name[1:]


def render_doctor(style: Style, report: dict, verbose: bool, support_path: Optional[str]) -> None:
    groups: dict = {}
    for check in report.get("checks", []):
        group = DOCTOR_GROUPS.get(str(check.get("category") or ""), "Other")
        groups.setdefault(group, []).append(check)
    width = max((len(name) for name in groups), default=0) + 2
    style.header("doctor")
    for name in [group for group in _GROUP_ORDER if group in groups]:
        checks = groups[name]
        # (symbol tone or None, text, dim): each entry wraps inside its own column.
        entries: list = []
        passed = [check for check in checks if check.get("status") == "pass"]
        if passed and not verbose:
            if name in _NAMED_PASSES:
                text = " · ".join(_pass_name(str(check["check_id"])) for check in passed)
            else:
                text = f"{len(passed)} check{'s' if len(passed) != 1 else ''} passed"
            entries.append(("ok", text, False))
        for check in checks:
            status = str(check.get("status") or "info")
            if status == "pass" and not verbose:
                continue
            entries.append((_TONES.get(status, "info"), str(check.get("summary")), False))
            if check.get("remediation") and status in {"warn", "fail"}:
                entries.append((None, "Fix: " + str(check["remediation"]), True))
        first = True
        room = style.width - len(INDENT) - width - 2
        for tone, text, dim in entries:
            lead = f"{style.paint(SYMBOLS[tone], tone)} " if tone else "  "
            for index, part in enumerate(textwrap.wrap(text, max(20, room), break_on_hyphens=False) or [""]):
                label = style.paint(name.ljust(width), bold=True) if first else " " * width
                shown = style.paint(part, dim=True) if dim else part
                style.write(f"{INDENT}{label}{lead if index == 0 else '  '}{shown}")
                first = False
    if support_path:
        style.write()
        style.write(f"{INDENT}Support bundle  {support_path}")
    fails = sum(1 for check in report.get("checks", []) if check.get("status") == "fail")
    warns = sum(1 for check in report.get("checks", []) if check.get("status") == "warn")
    hint = "" if verbose else "(--verbose: every check)"
    if fails:
        parts = [f"{fails} problem{'s' if fails != 1 else ''}"] + ([f"{warns} warning{'s' if warns != 1 else ''}"] if warns else [])
        style.summary("error", " and ".join(parts) + " — fix them, then run: mac-mcp doctor")
    elif report.get("health") == "degraded":
        style.summary("warn", "The local runtime is fine; the public address is not available", next_command="mac-mcp status")
    elif warns:
        style.summary("warn", f"{warns} thing{'s' if warns != 1 else ''} to look at")
    else:
        style.summary("ok", "Everything Mac MCP needs is in place.", note=hint)


def render_clients(style: Style, apps: list) -> None:
    """The AI apps on this Mac and whether Mac MCP is added; the rest in one line."""
    present = [app for app in apps if app.get("installed") or app.get("added")]
    missing = [app for app in apps if not (app.get("installed") or app.get("added"))]
    rows = []
    for app in present:
        if app.get("added") and app.get("keyless") is False:
            rows.append(("warn", app["name"], "added the old way",
                         f"key in its settings — mac-mcp connect --client {app['id']}"))
        elif app.get("added"):
            rows.append(("ok", app["name"], "added", ""))
        else:
            rows.append(("info", app["name"], "on this Mac · not added", ""))
    style.header("clients")
    style.rows(rows)
    if missing:
        if rows:
            style.write()
        text = "Not on this Mac: " + " · ".join(app["name"] for app in missing)
        for line in textwrap.wrap(text, style.width - len(INDENT)):
            style.write(f"{INDENT}{style.paint(line, dim=True)}")
    if not present:
        style.summary("info", "No AI app Mac MCP knows is on this Mac.")
        return
    added = sum(1 for app in present if app.get("added"))
    waiting = next((app for app in present if not app.get("added")), None)
    style.summary("ok", f"Mac MCP is added to {added} of {len(present)} apps on this Mac",
                  next_command=f"mac-mcp connect --client {waiting['id']}" if waiting else None)
