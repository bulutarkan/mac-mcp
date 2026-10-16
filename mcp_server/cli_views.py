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


def _render_checklist(style: Style, groups: list, verbose: bool, named_passes: frozenset = frozenset()) -> None:
    """Checks by group: passes fold into one line per group (names or a count); the rest are
    written out, with their fix under warnings and failures."""
    width = max((len(name) for name, _ in groups), default=0) + 2
    for name, checks in groups:
        # (symbol tone or None, text, dim): each entry wraps inside its own column.
        entries: list = []
        passed = [check for check in checks if check.get("status") == "pass"]
        if passed and not verbose:
            if name in named_passes:
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


def render_doctor(style: Style, report: dict, verbose: bool, support_path: Optional[str]) -> None:
    groups: dict = {}
    for check in report.get("checks", []):
        group = DOCTOR_GROUPS.get(str(check.get("category") or ""), "Other")
        groups.setdefault(group, []).append(check)
    style.header("doctor")
    _render_checklist(style, [(name, groups[name]) for name in _GROUP_ORDER if name in groups], verbose,
                      frozenset(_NAMED_PASSES))
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
            rows.append(("warn", app["name"], "added the old way", "key in its settings"))
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
    old = next((app for app in present if app.get("added") and app.get("keyless") is False), None)
    waiting = next((app for app in present if not app.get("added")), None)
    if old:
        # The command sits in the summary, where it never breaks: an old setup is updated first.
        style.summary("warn", f"{old['name']} holds Mac MCP's key in its own settings",
                      next_command=f"mac-mcp connect --client {old['id']}")
        return
    style.summary("ok", f"Mac MCP is added to {added} of {len(present)} apps on this Mac",
                  next_command=f"mac-mcp connect --client {waiting['id']}" if waiting else None)


def _wrapped_note(style: Style, title: str, text: str) -> None:
    style.write(f"{INDENT}{style.paint(title, bold=True)}")
    for line in str(text).splitlines():
        for part in textwrap.wrap(line, style.width - len(INDENT) - 2) or [""]:
            style.write(f"{INDENT}  {part}")


def render_connect_config(style: Style, rendered, endpoint_kind: str, endpoint_url: str,
                          public_ok: Optional[bool], auth_required: bool) -> None:
    """The facts, then the snippet exactly as the app takes it (no colour, no indent)."""
    rows = [(None, "Client", rendered.client, ""), (None, "Target", rendered.target, ""),
            (None, "Endpoint", f"{endpoint_kind} · {endpoint_url}", "")]
    if public_ok is not None:
        rows.append(("ok", "Public check", "reachable", "") if public_ok else
                    ("warn", "Public check", "not reachable right now", "the app connects once mac-mcp status says it is healthy"))
    rows.append((None, "Authentication", "API key required · never printed" if auth_required else "disabled", ""))
    style.header("connect-config")
    style.rows(rows)
    style.write()
    for line in textwrap.wrap(f"Copy into {rendered.target}:", style.width - len(INDENT)):
        style.write(f"{INDENT}{style.paint(line, dim=True)}")
    style.write()
    # Raw, so a copy from the terminal pastes into the app's config unchanged.
    style.stream.write(rendered.snippet.rstrip("\n") + "\n")
    for title, text in (("Secret setup", rendered.secret_instruction), ("Verify", rendered.verify_instruction)):
        if text:
            style.write()
            _wrapped_note(style, title, text)
    if rendered.client == "chatgpt" and auth_required:
        style.write()
        _wrapped_note(style, "Note", "Use the query-key URL only because this ChatGPT connection path is header-limited; "
                                     "upstream proxies/tunnels may observe query strings.")
    if public_ok is False:
        style.summary("warn", "Snippet ready, but the public address does not answer yet", next_command="mac-mcp status")
    else:
        style.summary("ok", "Snippet ready — no secret is printed.")


def render_error(style: Style, command: str, message: str, next_command: Optional[str] = None) -> None:
    """A command that could not run: the header, then the reason (the header leaves the gap)."""
    style.header(command)
    style.summary("error", message, next_command=next_command, gap=False)


def _short_commit(commit: Optional[str]) -> str:
    return (commit or "unknown")[:8]


def render_update_check(style: Style, info) -> None:
    """The installed commit against the newest verified stable release, and what to do."""
    stable = _short_commit(info.target_commit)
    if info.release_id:
        stable += f" · {info.release_id}" + (f" (v{info.release_version})" if info.release_version else "")
    rows = [(None, "Installed", _short_commit(info.deployed_commit), ""),
            (None, "Verified stable", stable, ""),
            (None, "Branch", f"{info.remote}/{info.branch}", "")]
    if info.unverified_ahead and not info.update_available:
        plural = "commit" if info.unverified_ahead == 1 else "commits"
        rows.append((None, "Not released", f"{info.unverified_ahead} development {plural} ahead", "not verified yet"))
    style.header("update")
    style.rows(rows)
    if info.dirty:
        style.summary("error", "Update blocked: the repository has local changes")
    elif info.update_available:
        style.summary("ok", "A verified update is ready", next_command="mac-mcp update")
    elif info.unverified_ahead:
        style.summary("ok", "No newer verified stable release.")
    else:
        style.summary("ok", "Mac MCP is up to date.")


def render_package_update(style: Style, payload: dict) -> None:
    style.header("update")
    style.rows([(None, "Download", str(payload.get("download_url") or ""), "")])
    style.summary("info", str(payload.get("message") or ""))


def render_autostart(style: Style, on: bool, warnings: str = "") -> None:
    style.header("autostart")
    if on:
        style.rows([("ok", "Start at login", "on", "a server you left running starts again when you log in")])
    else:
        style.rows([("info", "Start at login", "off", "start it with mac-mcp start or the menu bar app")])
    lines = [line.strip() for line in warnings.splitlines() if line.strip()]
    if lines:
        style.write()
    for line in lines:
        note_lines(style, line, "warn")
    if on:
        style.summary("ok", "Start at login is on")
    else:
        style.summary("ok", "Start at login is off", next_command="mac-mcp autostart on")


def render_credential_status(style: Style, state) -> None:
    """The Cloudflare Tunnel credential: saved, missing, or saved in an unsafe file."""
    style.header("credential")
    if state.configured and state.secure:
        style.rows([("ok", "Cloudflare Tunnel", "saved", "owner-only file")])
        style.summary("ok", "The Cloudflare Tunnel credential is saved")
    elif not state.configured:
        style.rows([("info", "Cloudflare Tunnel", "not saved", "")])
        style.summary("warn", "No Cloudflare Tunnel credential is saved", next_command="mac-mcp credential cloudflare save")
    else:
        style.rows([("error", "Cloudflare Tunnel", "saved but unsafe", str(state.reason))])
        style.summary("error", "The saved credential is not safe", next_command="mac-mcp credential cloudflare save")


def dim_lines(style: Style, text: str, indent: str = INDENT) -> None:
    """A dim aside, wrapped to the width under `indent`."""
    for line in str(text).splitlines():
        for part in textwrap.wrap(line, style.width - len(indent), break_on_hyphens=False) or [""]:
            style.write(f"{indent}{style.paint(part, dim=True)}")


def render_catalog_list(style: Style, listed: dict) -> None:
    rows = []
    # The id is the label: it is short, and it is what review and install take.
    for pack in listed["packs"]:
        installed = pack.get("installed_version")
        if installed and pack.get("update_available"):
            tone, state = "warn", f"update available, installed {installed}"
        elif installed:
            tone, state = "ok", f"installed {installed}"
        else:
            tone, state = "info", "not installed"
        rows.append((tone, pack["id"], f"{pack['version']} · {pack['kind']}", f"{state} · {pack['name']}"))
    style.header("catalog")
    style.rows(rows)
    if rows:
        style.write()
    dim_lines(style, f"Signed by {listed.get('signer')}")
    updates = [pack for pack in listed["packs"] if pack.get("installed_version") and pack.get("update_available")]
    if updates:
        count = len(updates)
        style.summary("warn", f"{count} update{'s' if count != 1 else ''} available",
                      next_command=f"mac-mcp catalog review {updates[0]['id']}")
    else:
        count = len(listed["packs"])
        style.summary("ok", f"{count} pack{'s' if count != 1 else ''} in the catalog")


def render_catalog_review(style: Style, report: dict) -> None:
    """A pack before it is installed: where it comes from, what it may do, what it brings."""
    pack = report["pack"]
    installed = report.get("installed_version")
    rows = [
        (None, "Pack", f"{pack['name']} · {pack['id']} {pack['version']} · {pack['kind']}", ""),
        (None, "Publisher", str(pack.get("publisher")), ""),
        (None, "Signed by", str(report.get("signer")), f"catalog {str(report.get('catalog_digest'))[:16]}"),
        (None, "Action", str(report["action"]), f"installed {installed}" if installed else ""),
        (None, "Capabilities", ", ".join(report["capabilities"]) or "none", ""),
    ]
    effective = report.get("effective") or {}
    if effective.get("tools"):
        rows.append((None, "Recipe tools", ", ".join(effective["tools"]), ""))
    changes = report.get("permission_changes")
    if changes and (changes["added"] or changes["removed"]):
        rows.append((None, "Permissions", " · ".join([f"+{name}" for name in changes["added"]]
                                                     + [f"-{name}" for name in changes["removed"]]), ""))
    for index, item in enumerate(report.get("files") or []):
        rows.append((None, "Files" if index == 0 else "", item["path"], f"{item['size']} B · sha256 {item['sha256'][:16]}"))
    if report.get("scripts_held"):
        rows.append(("warn", "Held back", ", ".join(report["scripts_held"]), str(report.get("script_note") or "")))
    style.header("catalog")
    style.rows(rows)
    style.write()
    dim_lines(style, str(pack.get("description") or ""))
    if report.get("note"):
        dim_lines(style, str(report["note"]))


PREVIEW_CHARS = 4000  # catalog.enable_script's preview holds at most this many characters


def visible_controls(text: str) -> str:
    """Control characters as visible text (^[ for ESC, ^M for CR, <U+009B> for C1), so a script
    cannot move the cursor or recolour the screen that reviews it. Tabs and newlines stay."""
    shown = []
    for char in text:
        code = ord(char)
        if char in "\t\n":
            shown.append(char)
        elif code < 32:
            shown.append("^" + chr(code + 64))
        elif code == 127:
            shown.append("^?")
        elif 0x80 <= code <= 0x9F:
            shown.append(f"<U+{code:04X}>")
        else:
            shown.append(char)
    return "".join(shown)


def render_script_review(style: Style, script: dict, command: str, full_path: Optional[str] = None) -> None:
    """A held script before it is enabled. Its text is written as it is, unindented and
    unwrapped, with only control characters made visible; a cut preview says so."""
    content = str(script["content"])
    style.header("catalog")
    style.rows([(None, "Script", script["path"], ""), (None, "SHA-256", script["sha256"], "")])
    style.write()
    style.write(f"{INDENT}{style.paint('Script:', dim=True)}")
    style.stream.write(visible_controls(content).rstrip("\n") + "\n")
    style.write(f"{INDENT}{style.paint('End of script', dim=True)}")
    if len(content) >= PREVIEW_CHARS:
        style.write()
        where = f" Read the whole file before you enable it: {full_path}" if full_path else " Read the whole file before you enable it."
        note_lines(style, f"This shows the first {PREVIEW_CHARS} characters of {script.get('size')} bytes.{where}", "warn")
    style.summary("warn", "Nothing enabled · this script can run anything", next_command=command)


CONFORMANCE_GROUPS = {"browser": "Browser", "native": "Native app", "observe": "Observation", "workspace": "Workspace",
                      "computer_plan": "Planning", "live": "Live"}
_CONFORMANCE_ORDER = ["Browser", "Native app", "Observation", "Workspace", "Planning", "Live", "Other"]


def conformance_group(check: dict) -> str:
    if str(check.get("category") or "computer_use") != "computer_use":
        return "Live"
    return CONFORMANCE_GROUPS.get(str(check.get("check_id") or "").split(".", 1)[0], "Other")


def render_conformance(style: Style, report: dict) -> None:
    """The Computer Use checks by area (after the header and the run step), then one sentence."""
    checks = report.get("checks", [])
    groups: dict = {}
    for check in checks:
        groups.setdefault(conformance_group(check), []).append(check)
    style.write()
    _render_checklist(style, [(name, groups[name]) for name in _CONFORMANCE_ORDER if name in groups], False)
    fails = sum(1 for check in checks if check.get("status") == "fail")
    warns = sum(1 for check in checks if check.get("status") == "warn")
    metrics = report.get("metrics", {})
    if fails:
        style.summary("error", f"{fails} failed — fix {'it' if fails == 1 else 'them'}, then run: mac-mcp conformance")
    elif warns:
        style.summary("warn", f"{warns} thing{'s' if warns != 1 else ''} to look at")
    else:
        ms = int(report.get("duration_ms") or 0)
        took = f"{ms} ms" if ms < 1000 else f"{ms / 1000:.1f}s"
        style.summary("ok", f"All {len(checks)} Computer Use checks passed",
                      note=f"{metrics.get('deterministic_pass_rate', 0)}% · {metrics.get('focus_safety_regressions', 0)} "
                           f"focus regressions · {took}")


def note_lines(style: Style, text: str, tone: str) -> None:
    """A sentence with its symbol, wrapped under its own first word."""
    lines = textwrap.wrap(str(text), style.width - len(INDENT) - 2, break_on_hyphens=False) or [""]
    for index, line in enumerate(lines):
        lead = f"{style.paint(SYMBOLS[tone], tone)} " if index == 0 else "  "
        style.write(f"{INDENT}{lead}{line}")


UNINSTALL_NOTES = ("Nothing is deleted: removed parts go to one folder in the Trash, so you can put them back.",
                   "Not changed: AI app configurations that point at Mac MCP, the PATH line in ~/.zprofile, "
                   "saved logins in your Keychain, and the Chrome companion (remove it at chrome://extensions).")


def render_uninstall_plan(style: Style, items: list) -> None:
    """What uninstall would move to the Trash and what it keeps; each part's path under its name."""
    moving = [item for item in items if item.remove and item.exists]
    kept = [item for item in items if not item.remove and item.exists]
    flags = {"state": ("--include-state", "your settings, keys and tokens"), "source": ("--include-source", "the source checkout")}
    style.header("uninstall")
    for title, group in (("Would move to the Trash", moving), ("Kept", kept)):
        style.write(f"{INDENT}{style.paint(title, bold=True)}")
        if not group:
            style.write(f"{INDENT}  {style.paint('nothing', dim=True)}")
        for item in group:
            for line in textwrap.wrap(item.label, style.width - len(INDENT) - 2) or [""]:
                style.write(f"{INDENT}  {line}")
            detail = home_path(item.path) + (f" · {item.note}" if item.note and item.remove else "")
            dim_lines(style, detail, indent=INDENT + "    ")
        for item in group if title == "Kept" else []:
            if item.key in flags:
                flag, what = flags[item.key]
                dim_lines(style, f"Add {flag} to move {what} too.", indent=INDENT + "  ")
        style.write()
    dim_lines(style, UNINSTALL_NOTES[0])
