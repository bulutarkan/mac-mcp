from __future__ import annotations

import asyncio
from pathlib import Path
import json
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status
from mcp.types import ToolAnnotations
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, Mount

from mcp.server.transport_security import TransportSecuritySettings
from .security import RateLimiter, Settings, authenticate, client_ip, load_settings, rate_limit, setup_audit_logger
from .observability import ObservedFastMCP, TelemetryManager
from .policy import current_policy_context, reset_policy_context, set_policy_context
from .scoped_auth import resolve_request_identity
from .dashboard_routes import create_dashboard_routes, rest_telemetry_middleware
from .tools_terminal import run_command, process_list, kill_process, get_system_info
from .tools_jobs import (
    start_background_job, get_job_status, get_job_output,
    stop_job, list_jobs, wait_jobs, run_commands_parallel,
)
from .tools_agents import (
    agent_catalog, spawn_agent, spawn_agents, wait_agents,
    list_agents, get_agent, agent_action,
)
from .tools_files import (
    write_file, write_files_batch, read_file, read_multiple_files,
    edit_file, move_file, copy_file, delete_path,
    list_directory, directory_tree, create_directory, get_file_info, find_files,
)
from .tools_macos import (
    run_applescript, send_notification, clipboard_get, clipboard_set,
    open_app, open_url, set_volume, get_volume, set_brightness,
    screenshot, set_reminder, get_running_apps,
)
from .tools_ui import observe_ui, act_ui
from .tools_search import search_files, spotlight_search
from .tools_http import http_request
from .tools_browser import (
    browser_open_url, browser_list_tabs, browser_activate_tab, browser_close_tab,
    browser_execute_js, browser_click_selector, browser_type_selector,
    browser_wait_for_selector, browser_get_html, browser_wait_for_download,
    browser_screenshot, browser_scroll, browser_press_key,
    browser_coordinate_click, browser_get_snapshot,
)
from .tools_browser_agent import browser_observe, browser_find, browser_act
from .tools_interactive import ask_choice, ask_confirmation, ask_user
from .tools_voice import ask_user_voice
from .tools_update import mac_mcp_update
from .tools_memory import memory_add, memory_search, memory_get, memory_update, memory_delete
from .tools_skills import skill_list, skill_search, skill_get, skill_register, skill_update_index
from .menu_app_bootstrap import bootstrap_menu_app_and_legacy_state


def _log(audit_logger, tool: str, fn):
    start = time.perf_counter()
    outcome = "ok"
    try:
        result = fn()
        return result
    except HTTPException as exc:
        outcome = f"error:{exc.status_code}:{exc.detail}"
        raise
    except Exception as exc:
        outcome = f"error:500:{exc}"
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Server error: {exc}") from exc
    finally:
        ms = int((time.perf_counter() - start) * 1000)
        audit_logger.info(json.dumps({"tool": tool, "outcome": outcome, "duration_ms": ms}))


def create_app():
    bootstrap_menu_app_and_legacy_state()
    settings = load_settings()
    limiter = RateLimiter(settings.rate_limit_per_minute)
    audit_logger = setup_audit_logger()
    telemetry = TelemetryManager()

    mcp = ObservedFastMCP(
        telemetry=telemetry,
        name="mac-mcp",
        instructions=(
            "You are connected to the user's local Mac through Mac MCP. "
            "Default home directory is the current user's home. "
            "Use run_command for shell work and the dedicated macOS/browser/UI tools when they fit better. "
            "For browser visual grounding, prefer one browser_observe call with visual='viewport' or visual='full_page'; "
            "it can target a background tab and returns compact DOM plus MCP image content without focusing the browser. "
            "Prefer the smallest number of tool calls that safely completes and verifies the task."
        ),
        streamable_http_path="/mcp",
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    class SecurityMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            if request.method == "HEAD" and request.url.path == "/mcp":
                return Response(status_code=200, headers={
                    "content-type": "text/event-stream; charset=utf-8",
                    "mcp-session-id": uuid.uuid4().hex,
                })
            if request.url.path.startswith("/mcp"):
                try:
                    rate_key, policy_context = resolve_request_identity(
                        settings, request.headers.get("authorization")
                    )
                    ip = client_ip(request)
                    rate_limit(limiter, rate_key, ip)
                except HTTPException as exc:
                    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
                context_token = set_policy_context(policy_context)
                try:
                    return await call_next(request)
                finally:
                    reset_policy_context(context_token)
            return await call_next(request)

    # ── Terminal tools ──────────────────────────────────────────────────────
    @mcp.tool(name="run_command",
              description="Run any shell command in zsh on the local Mac. Full access.")
    def _run_command(command: str, timeout_s: Optional[int] = None) -> Dict[str, Any]:
        return _log(audit_logger, "run_command",
                    lambda: run_command(settings, command=command, timeout_s=timeout_s))

    @mcp.tool(name="process_list",
              description="List running processes. Optional filter by name substring.")
    def _process_list(filter: Optional[str] = None) -> Dict[str, Any]:
        return _log(audit_logger, "process_list",
                    lambda: process_list(settings, filter=filter))

    @mcp.tool(name="kill_process",
              description="Kill a process by PID. signal: TERM (graceful) or KILL (force).")
    def _kill_process(pid: int, signal: str = "TERM") -> Dict[str, Any]:
        return _log(audit_logger, "kill_process",
                    lambda: kill_process(settings, pid=pid, signal=signal))

    @mcp.tool(name="get_system_info",
              description="Get Mac system info: CPU, memory, disk, battery, network, uptime.")
    def _get_system_info() -> Dict[str, Any]:
        return _log(audit_logger, "get_system_info", lambda: get_system_info(settings))

    @mcp.tool(name="start_background_job",
              description=(
                  "Start a shell command and return immediately with job_id. "
                  "Default timeout is 60 seconds; set timeout_s explicitly (up to 600) for longer npm installs, "
                  "builds, downloads, dev servers, docker, or tests."
              ))
    def _start_background_job(command: str, cwd: Optional[str] = None,
                              env: Optional[Dict[str, str]] = None,
                              timeout_s: Optional[int] = None,
                              no_output_timeout_s: Optional[int] = None) -> Dict[str, Any]:
        return _log(audit_logger, "start_background_job",
                    lambda: start_background_job(settings, command=command, cwd=cwd, env=env,
                                                 timeout_s=timeout_s, no_output_timeout_s=no_output_timeout_s))

    @mcp.tool(name="get_job_status",
              description="Get status for a background job by job_id.")
    def _get_job_status(job_id: str) -> Dict[str, Any]:
        return _log(audit_logger, "get_job_status",
                    lambda: get_job_status(settings, job_id=job_id))

    @mcp.tool(name="get_job_output",
              description="Read stdout/stderr for a background job. Use since_offset for incremental output or tail_lines for recent logs.")
    def _get_job_output(job_id: str, tail_lines: Optional[int] = None,
                        since_offset: Optional[int] = None,
                        stream: str = "both") -> Dict[str, Any]:
        return _log(audit_logger, "get_job_output",
                    lambda: get_job_output(settings, job_id=job_id, tail_lines=tail_lines,
                                           since_offset=since_offset, stream=stream))

    @mcp.tool(name="stop_job",
              description="Stop a background job by job_id. signal: TERM, KILL, INT, HUP.")
    def _stop_job(job_id: str, signal: str = "TERM") -> Dict[str, Any]:
        return _log(audit_logger, "stop_job",
                    lambda: stop_job(settings, job_id=job_id, signal_name=signal))

    @mcp.tool(name="list_jobs",
              description="List background jobs. status_filter can be running, stalled, completed, failed, timeout, killed.")
    def _list_jobs(status_filter: Optional[str] = None) -> Dict[str, Any]:
        return _log(audit_logger, "list_jobs",
                    lambda: list_jobs(settings, status_filter=status_filter))

    @mcp.tool(name="wait_jobs",
              description=(
                  "Wait for background jobs to finish, optionally returning output. "
                  "timeout_s defaults to 60 seconds; a bounded response is returned if jobs are still running."
              ))
    def _wait_jobs(job_ids: List[str], timeout_s: Optional[int] = None,
                   return_output: bool = False) -> Dict[str, Any]:
        return _log(audit_logger, "wait_jobs",
                    lambda: wait_jobs(settings, job_ids=job_ids, timeout_s=timeout_s,
                                      return_output=return_output))

    @mcp.tool(name="run_commands_parallel",
              description=(
                  "Run multiple shell commands in parallel and collect results. Best for independent checks "
                  "like tests, lint, rg, scripts. timeout_s defaults to a bounded 60 seconds."
              ))
    def _run_commands_parallel(commands: List[str], cwd: Optional[str] = None,
                               timeout_s: Optional[int] = None,
                               return_output: bool = True) -> Dict[str, Any]:
        return _log(audit_logger, "run_commands_parallel",
                    lambda: run_commands_parallel(settings, commands=commands, cwd=cwd,
                                                  timeout_s=timeout_s, return_output=return_output))

    # ── Agent delegation tools ──────────────────────────────────────────────
    @mcp.tool(
        name="agent_catalog",
        description="List available OpenCode/Codex providers, models, and reasoning options without starting an agent.",
    )
    async def _agent_catalog(provider: Optional[str] = None, model_filter: Optional[str] = None,
                             free_only: bool = False, limit: int = 80) -> Dict[str, Any]:
        return await asyncio.to_thread(
            _log, audit_logger, "agent_catalog",
            lambda: agent_catalog(settings, provider=provider, model_filter=model_filter,
                                  free_only=free_only, limit=limit),
        )

    @mcp.tool(
        name="spawn_agent",
        description=(
            "Delegate one task to OpenCode or Codex in a non-blocking background process. "
            "Supports idle timeout and same-model retries; concise final handoff is the default. "
            "Codex enforces access_mode; OpenCode read_only is refused and its other modes are not a hard sandbox."
        ),
    )
    def _spawn_agent(provider: str, prompt: str, model: Optional[str] = None,
                     reasoning: Optional[str] = None, cwd: Optional[str] = None,
                     timeout_s: Optional[int] = None, title: Optional[str] = None,
                     result_style: str = "concise", access_mode: str = "workspace_write",
                     idle_timeout_s: Optional[int] = None, retries: int = 0,
                     scope: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        context = current_policy_context()
        return _log(audit_logger, "spawn_agent",
                    lambda: spawn_agent(settings, provider=provider, prompt=prompt, model=model,
                                        reasoning=reasoning, cwd=cwd, timeout_s=timeout_s,
                                        title=title, result_style=result_style, access_mode=access_mode,
                                        idle_timeout_s=idle_timeout_s, retries=retries, scope=scope,
                                        parent_scope=context.scope, parent_profile=context.profile))

    @mcp.tool(
        name="spawn_agents",
        description=(
            "Spawn 1-10 background agents as one team in a single call. All children inherit the same "
            "provider, model, reasoning and access_mode. Codex enforces access_mode; OpenCode read_only is refused "
            "and its other modes are not a hard sandbox. Returns immediately with team_id and agent_ids."
        ),
    )
    def _spawn_agents(tasks: List[Dict[str, Any]], provider: str, model: Optional[str] = None,
                      reasoning: Optional[str] = None, cwd: Optional[str] = None,
                      timeout_s: Optional[int] = None, idle_timeout_s: Optional[int] = None,
                      retries: int = 1, result_style: str = "concise",
                      access_mode: str = "read_only", title: Optional[str] = None,
                      scope: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        context = current_policy_context()
        return _log(audit_logger, "spawn_agents",
                    lambda: spawn_agents(settings, tasks=tasks, provider=provider, model=model,
                                         reasoning=reasoning, cwd=cwd, timeout_s=timeout_s,
                                         idle_timeout_s=idle_timeout_s, retries=retries,
                                         result_style=result_style, access_mode=access_mode, title=title,
                                         scope=scope, parent_scope=context.scope, parent_profile=context.profile))

    @mcp.tool(
        name="wait_agents",
        description=(
            "Bounded wait for a team or explicit agent_ids. mode: all, any, majority. "
            "Returns concise results for agents that finished, avoiding repeated polling."
        ),
    )
    async def _wait_agents(team_id: Optional[str] = None, agent_ids: Optional[List[str]] = None,
                           mode: str = "all", timeout_s: int = 30,
                           include_results: bool = True) -> Dict[str, Any]:
        return await asyncio.to_thread(
            _log, audit_logger, "wait_agents",
            lambda: wait_agents(settings, team_id=team_id, agent_ids=agent_ids,
                                mode=mode, timeout_s=timeout_s, include_results=include_results),
        )

    @mcp.tool(
        name="list_agents",
        description="List delegated agents with compact status/result previews.",
    )
    def _list_agents(status_filter: Optional[str] = None, limit: int = 20,
                     team_id: Optional[str] = None) -> Dict[str, Any]:
        return _log(audit_logger, "list_agents",
                    lambda: list_agents(settings, status_filter=status_filter, limit=limit, team_id=team_id))

    @mcp.tool(
        name="get_agent",
        description="Get one delegated agent status and concise final result. Set include_logs=true only for debugging.",
    )
    def _get_agent(agent_id: str, include_logs: bool = False,
                   tail_lines: int = 40) -> Dict[str, Any]:
        return _log(audit_logger, "get_agent",
                    lambda: get_agent(settings, agent_id=agent_id, include_logs=include_logs,
                                      tail_lines=tail_lines))

    @mcp.tool(
        name="agent_action",
        description=(
            "Control one agent or a whole team. action: cancel, retry, despawn; message is available "
            "for individual resumable agent sessions. Team cancel cascades to all children."
        ),
    )
    def _agent_action(action: str, agent_id: Optional[str] = None, team_id: Optional[str] = None,
                      message: Optional[str] = None, signal: str = "TERM") -> Dict[str, Any]:
        return _log(audit_logger, "agent_action",
                    lambda: agent_action(settings, action=action, agent_id=agent_id, team_id=team_id,
                                         message=message, signal=signal))

    # ── File tools ──────────────────────────────────────────────────────────
    @mcp.tool(name="write_file",
              description="Write content to a file. Creates parent directories if needed.")
    def _write_file(path: str, content: str) -> Dict[str, Any]:
        return _log(audit_logger, "write_file",
                    lambda: write_file(settings, path=path, content=content))

    @mcp.tool(name="write_files_batch",
              description="Write multiple files in one call. Pass a list of objects, each with 'path' and 'content' string fields.")
    def _write_files_batch(files: List[Dict[str, str]], atomic: bool = True) -> Dict[str, Any]:
        return _log(audit_logger, "write_files_batch",
                    lambda: write_files_batch(settings, files=files, atomic=atomic))

    @mcp.tool(name="read_file",
              description="Read a file. offset/length for line-based pagination.")
    def _read_file(path: str, offset: int = 0, length: Optional[int] = None) -> Dict[str, Any]:
        return _log(audit_logger, "read_file",
                    lambda: read_file(settings, path=path, offset=offset, length=length))

    @mcp.tool(name="read_multiple_files",
              description="Read multiple files in one call. Returns contents keyed by path.")
    def _read_multiple_files(paths: List[str]) -> Dict[str, Any]:
        return _log(audit_logger, "read_multiple_files",
                    lambda: read_multiple_files(settings, paths=paths))

    @mcp.tool(name="edit_file",
              description="Find-and-replace in a file. Fails if occurrence count != expected_replacements.")
    def _edit_file(path: str, old_string: str, new_string: str,
                   expected_replacements: int = 1) -> Dict[str, Any]:
        return _log(audit_logger, "edit_file",
                    lambda: edit_file(settings, path=path, old_string=old_string,
                                      new_string=new_string, expected_replacements=expected_replacements))

    @mcp.tool(name="move_file", description="Move or rename a file/directory.")
    def _move_file(source: str, destination: str) -> Dict[str, Any]:
        return _log(audit_logger, "move_file",
                    lambda: move_file(settings, source=source, destination=destination))

    @mcp.tool(name="copy_file", description="Copy a file or directory.")
    def _copy_file(source: str, destination: str) -> Dict[str, Any]:
        return _log(audit_logger, "copy_file",
                    lambda: copy_file(settings, source=source, destination=destination))

    @mcp.tool(name="delete_path",
              description="Delete a file or directory. Set recursive=true for directories.")
    def _delete_path(path: str, recursive: bool = False) -> Dict[str, Any]:
        return _log(audit_logger, "delete_path",
                    lambda: delete_path(settings, path=path, recursive=recursive))

    @mcp.tool(name="list_directory", description="List files and directories in a path.")
    def _list_directory(path: str) -> Dict[str, Any]:
        return _log(audit_logger, "list_directory",
                    lambda: list_directory(settings, path=path))

    @mcp.tool(name="directory_tree",
              description="Show directory structure as a tree. depth controls how deep.")
    def _directory_tree(path: str, depth: int = 3) -> Dict[str, Any]:
        return _log(audit_logger, "directory_tree",
                    lambda: directory_tree(settings, path=path, depth=depth))

    @mcp.tool(name="create_directory", description="Create a directory (and parents).")
    def _create_directory(path: str) -> Dict[str, Any]:
        return _log(audit_logger, "create_directory",
                    lambda: create_directory(settings, path=path))

    @mcp.tool(name="get_file_info",
              description="Get file/directory metadata: size, dates, type, permissions.")
    def _get_file_info(path: str) -> Dict[str, Any]:
        return _log(audit_logger, "get_file_info",
                    lambda: get_file_info(settings, path=path))

    @mcp.tool(name="find_files",
              description="Find files by name glob pattern (e.g. '*.py', 'report*'). file_type: file|dir|any.")
    def _find_files(pattern: str, path: str = str(Path.home()),
                    file_type: str = "any") -> Dict[str, Any]:
        return _log(audit_logger, "find_files",
                    lambda: find_files(settings, pattern=pattern, path=path, file_type=file_type))

    # ── macOS tools ─────────────────────────────────────────────────────────
    @mcp.tool(name="run_applescript",
              description="Run AppleScript on macOS. Control apps, system settings, UI automation.")
    def _run_applescript(script: str, timeout_s: int = 30) -> Dict[str, Any]:
        return _log(audit_logger, "run_applescript",
                    lambda: run_applescript(settings, script=script, timeout_s=timeout_s))

    @mcp.tool(name="send_notification",
              description="Send a macOS notification banner. sound: Pop, Glass, Basso, etc.")
    def _send_notification(title: str, message: str, sound: str = "Pop") -> Dict[str, Any]:
        return _log(audit_logger, "send_notification",
                    lambda: send_notification(settings, title=title, message=message, sound=sound))

    @mcp.tool(name="clipboard_get", description="Read the current Mac clipboard contents.")
    def _clipboard_get() -> Dict[str, Any]:
        return _log(audit_logger, "clipboard_get", lambda: clipboard_get(settings))

    @mcp.tool(name="clipboard_set", description="Write text to the Mac clipboard.")
    def _clipboard_set(content: str) -> Dict[str, Any]:
        return _log(audit_logger, "clipboard_set",
                    lambda: clipboard_set(settings, content=content))

    @mcp.tool(name="open_app",
              description="Open a macOS application by name. e.g. 'Safari', 'Finder', 'Terminal'.")
    def _open_app(app_name: str) -> Dict[str, Any]:
        return _log(audit_logger, "open_app",
                    lambda: open_app(settings, app_name=app_name))

    @mcp.tool(name="open_url", description="Open a URL in the default browser.")
    def _open_url(url: str) -> Dict[str, Any]:
        return _log(audit_logger, "open_url", lambda: open_url(settings, url=url))

    @mcp.tool(name="set_volume", description="Set system volume (0-100).")
    def _set_volume(level: int) -> Dict[str, Any]:
        return _log(audit_logger, "set_volume",
                    lambda: set_volume(settings, level=level))

    @mcp.tool(name="get_volume", description="Get current system volume level.")
    def _get_volume() -> Dict[str, Any]:
        return _log(audit_logger, "get_volume", lambda: get_volume(settings))

    @mcp.tool(name="set_brightness",
              description="Set screen brightness (0-100). Requires 'brew install brightness'.")
    def _set_brightness(level: int) -> Dict[str, Any]:
        return _log(audit_logger, "set_brightness",
                    lambda: set_brightness(settings, level=level))

    @mcp.tool(name="screenshot",
              description="Take a screenshot. path: save location. window=true for interactive window select.")
    def _screenshot(path: str = str(Path.home() / "Desktop" / "screenshot.png"),
                    window: bool = False) -> Dict[str, Any]:
        return _log(audit_logger, "screenshot",
                    lambda: screenshot(settings, path=path, window=window))

    @mcp.tool(name="set_reminder",
              description="Add a reminder to macOS Reminders. due_date format: 'month/day/year HH:MM AM/PM'.")
    def _set_reminder(title: str, notes: str = "",
                      due_date: Optional[str] = None) -> Dict[str, Any]:
        return _log(audit_logger, "set_reminder",
                    lambda: set_reminder(settings, title=title, notes=notes, due_date=due_date))

    @mcp.tool(name="get_running_apps",
              description="Get list of currently running macOS applications (visible apps only).")
    def _get_running_apps() -> Dict[str, Any]:
        return _log(audit_logger, "get_running_apps", lambda: get_running_apps(settings))

    # ── Unified macOS UI tools ──────────────────────────────────────────────
    @mcp.tool(
        name="mac_observe",
        title="Observe macOS UI",
        description=(
            "Read the frontmost or named macOS application's current UI state. "
            "Returns an observation_id, Accessibility tree nodes with element_id, role, "
            "title, value, position, enabled state and supported actions, plus a screen "
            "image when include_screenshot=true. Screenshots are returned as connector-safe "
            "JPEG image content. Use ocr=true only when Accessibility text is insufficient. "
            "Pass the observation_id to mac_act for safe targeting."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        structured_output=False,
    )
    def _mac_observe(
        app: Optional[str] = None,
        window_index: int = 1,
        max_depth: int = 5,
        max_children: int = 30,
        include_screenshot: bool = True,
        ocr: bool = False,
    ) -> Any:
        return _log(
            audit_logger,
            "mac_observe",
            lambda: observe_ui(
                settings,
                app=app,
                window_index=window_index,
                max_depth=max_depth,
                max_children=max_children,
                include_screenshot=include_screenshot,
                ocr=ocr,
            ),
        )

    @mcp.tool(
        name="mac_act",
        title="Act on macOS UI",
        description=(
            "Perform one or more bounded macOS UI actions using element_id values from "
            "mac_observe, then return a fresh state by default. Supported action types: "
            "click/double_click, scroll, type, paste, key/shortcut, drag, and "
            "accessibility_action/menu. Use observation_id to prevent stale element paths. "
            "Potentially consequential clicks require allow_risky=true explicitly. "
            "The complete action batch has a 60-second safety budget."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
        structured_output=False,
    )
    def _mac_act(
        actions: List[Dict[str, Any]],
        observation_id: Optional[str] = None,
        app: Optional[str] = None,
        return_state: bool = True,
        allow_risky: bool = False,
    ) -> Any:
        return _log(
            audit_logger,
            "mac_act",
            lambda: act_ui(
                settings,
                actions=actions,
                observation_id=observation_id,
                app=app,
                return_state=return_state,
                allow_risky=allow_risky,
            ),
        )

    # ── Search tools ────────────────────────────────────────────────────────
    @mcp.tool(name="search_files",
              description="Search file contents with grep. include_extensions filters by type e.g. ['py','js'].")
    def _search_files(pattern: str, path: str = str(Path.home()),
                      include_extensions: Optional[List[str]] = None,
                      case_sensitive: bool = False) -> Dict[str, Any]:
        return _log(audit_logger, "search_files",
                    lambda: search_files(settings, pattern=pattern, path=path,
                                         include_extensions=include_extensions,
                                         case_sensitive=case_sensitive))

    @mcp.tool(name="spotlight_search",
              description="Search files by name using macOS Spotlight (mdfind) — very fast.")
    def _spotlight_search(query: str, max_results: int = 50) -> Dict[str, Any]:
        return _log(audit_logger, "spotlight_search",
                    lambda: spotlight_search(settings, query=query, max_results=max_results))

    # ── HTTP tool ───────────────────────────────────────────────────────────
    @mcp.tool(name="http_request",
              description="Make HTTP GET/POST/PUT/DELETE requests to external URLs.")
    def _http_request(url: str, method: str = "GET",
                      headers: Optional[Dict[str, str]] = None,
                      body: Optional[str] = None) -> Dict[str, Any]:
        return _log(audit_logger, "http_request",
                    lambda: http_request(settings, url=url, method=method,
                                         headers=headers, body=body))

    # ── Browser tools ────────────────────────────────────────────────────────
    @mcp.tool(name="browser_open_url",
              description=(
                  "Open a URL in Safari or Google Chrome. New tabs open in the background by default and return "
                  "a stable tab_handle; set background=false only when foreground activation is explicitly wanted."
              ))
    def _browser_open_url(browser: str, url: str, new_tab: bool = True,
                          background: bool = True) -> Dict[str, Any]:
        return _log(audit_logger, "browser_open_url",
                    lambda: browser_open_url(settings, browser=browser, url=url,
                                             new_tab=new_tab, background=background))

    @mcp.tool(name="browser_list_tabs",
              description="List all open tabs with stable tab_handle values that survive tab index shifts.")
    def _browser_list_tabs(browser: str) -> Dict[str, Any]:
        return _log(audit_logger, "browser_list_tabs",
                    lambda: browser_list_tabs(settings, browser=browser))

    @mcp.tool(name="browser_activate_tab",
              description=("Select a specific browser tab by stable tab_handle or index without raising the browser by default. "
                           "Set allow_foreground=true only when bringing the browser app to the front is explicitly wanted."))
    def _browser_activate_tab(browser: str, window_index: int = 1, tab_index: int = 1,
                              tab_handle: Optional[str] = None,
                              allow_foreground: bool = False) -> Dict[str, Any]:
        return _log(audit_logger, "browser_activate_tab",
                    lambda: browser_activate_tab(settings, browser=browser, window_index=window_index,
                                                tab_index=tab_index, tab_handle=tab_handle,
                                                allow_foreground=allow_foreground))

    @mcp.tool(name="browser_close_tab",
              description="Close a tab by window_index and tab_index.")
    def _browser_close_tab(browser: str, window_index: int = 1, tab_index: int = 1,
                           tab_handle: Optional[str] = None) -> Dict[str, Any]:
        return _log(audit_logger, "browser_close_tab",
                    lambda: browser_close_tab(settings, browser=browser, window_index=window_index,
                                             tab_index=tab_index, tab_handle=tab_handle))

    @mcp.tool(
        name="browser_observe",
        title="Observe browser tab",
        description=(
            "High-level browser observation. Returns compact DOM with stable e1/e2 IDs and optional JPEG visual. "
            "scope: interactive, visible, content, or leaf; visual: none, viewport, element, or full_page. "
            "Visual capture is rendered inside the target tab DOM and returned as MCP image content without "
            "activating Safari/Chrome, switching tabs, scrolling the page, or leaving screenshot files on disk."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        structured_output=False,
    )
    def _browser_observe(browser: str, window_index: int = 1, tab_index: Optional[int] = None,
                         tab_handle: Optional[str] = None,
                         scope: str = "interactive", max_elements: int = 120,
                         visual: str = "none", element_id: Optional[str] = None) -> Any:
        return _log(
            audit_logger, "browser_observe",
            lambda: browser_observe(settings, browser=browser, window_index=window_index,
                                    tab_index=tab_index, tab_handle=tab_handle,
                                    scope=scope, max_elements=max_elements,
                                    visual=visual, element_id=element_id),
        )

    @mcp.tool(
        name="browser_find",
        description=(
            "Find a rendered browser element with exact-first ranking and hard role/text constraints. "
            "Set actionable_only=false to include labels/cards; use best_match with browser_act."
        ),
    )
    async def _browser_find(browser: str, query: str, role: Optional[str] = None,
                            text: Optional[str] = None, window_index: int = 1,
                            tab_index: Optional[int] = None, tab_handle: Optional[str] = None,
                            max_results: int = 5,
                            actionable_only: bool = False) -> Dict[str, Any]:
        return await asyncio.to_thread(
            _log, audit_logger, "browser_find",
            lambda: browser_find(settings, browser=browser, query=query, role=role, text=text,
                                  window_index=window_index, tab_index=tab_index, tab_handle=tab_handle,
                                  max_results=max_results,
                                  actionable_only=actionable_only),
        )

    @mcp.tool(
        name="browser_act",
        description=(
            "Perform up to 20 browser actions in one MCP call. Actions can target stable element_id or semantic "
            "query/text_match/role. Supports click, type, async custom select, scroll, key and waits; "
            "return_state: none, compact, or full."
        ),
    )
    async def _browser_act(browser: str, actions: List[Dict[str, Any]],
                           observation_id: Optional[str] = None, window_index: int = 1,
                           tab_index: Optional[int] = None, tab_handle: Optional[str] = None,
                           return_state: str = "compact", allow_foreground: bool = False) -> Dict[str, Any]:
        return await asyncio.to_thread(
            _log, audit_logger, "browser_act",
            lambda: browser_act(settings, browser=browser, actions=actions,
                                 observation_id=observation_id, window_index=window_index,
                                 tab_index=tab_index, tab_handle=tab_handle,
                                 return_state=return_state, allow_foreground=allow_foreground),
        )

    @mcp.tool(name="browser_execute_js",
              description="Execute JavaScript in a browser tab and return the result.")
    def _browser_execute_js(browser: str, js: str, window_index: int = 1,
                             tab_index: Optional[int] = None,
                             tab_handle: Optional[str] = None) -> Dict[str, Any]:
        return _log(audit_logger, "browser_execute_js",
                    lambda: browser_execute_js(settings, browser=browser, js=js,
                                               window_index=window_index, tab_index=tab_index,
                                               tab_handle=tab_handle))

    @mcp.tool(name="browser_click_selector",
              description="Click an element by CSS selector in a browser tab.")
    def _browser_click_selector(browser: str, css_selector: str, window_index: int = 1,
                                 tab_index: Optional[int] = None,
                                 tab_handle: Optional[str] = None) -> Dict[str, Any]:
        return _log(audit_logger, "browser_click_selector",
                    lambda: browser_click_selector(settings, browser=browser, css_selector=css_selector,
                                                   window_index=window_index, tab_index=tab_index,
                                                   tab_handle=tab_handle))

    @mcp.tool(name="browser_type_selector",
              description="Type text into an element by CSS selector. clear=true clears first.")
    def _browser_type_selector(browser: str, css_selector: str, text: str, clear: bool = True,
                                window_index: int = 1, tab_index: Optional[int] = None,
                                tab_handle: Optional[str] = None) -> Dict[str, Any]:
        return _log(audit_logger, "browser_type_selector",
                    lambda: browser_type_selector(settings, browser=browser, css_selector=css_selector,
                                                  text=text, clear=clear, window_index=window_index,
                                                  tab_index=tab_index, tab_handle=tab_handle))

    @mcp.tool(name="browser_wait_for_selector",
              description="Wait until a CSS selector appears in the page. Returns found=true/false.")
    def _browser_wait_for_selector(browser: str, css_selector: str, timeout_s: int = 20,
                                    window_index: int = 1, tab_index: Optional[int] = None,
                                    tab_handle: Optional[str] = None) -> Dict[str, Any]:
        return _log(audit_logger, "browser_wait_for_selector",
                    lambda: browser_wait_for_selector(settings, browser=browser, css_selector=css_selector,
                                                      timeout_s=timeout_s, window_index=window_index,
                                                      tab_index=tab_index, tab_handle=tab_handle))

    @mcp.tool(name="browser_get_html",
              description="Get the full HTML of the current page in a browser tab.")
    def _browser_get_html(browser: str, max_chars: Optional[int] = None,
                          window_index: int = 1, tab_index: Optional[int] = None,
                          tab_handle: Optional[str] = None) -> Dict[str, Any]:
        return _log(audit_logger, "browser_get_html",
                    lambda: browser_get_html(settings, browser=browser, max_chars=max_chars,
                                             window_index=window_index, tab_index=tab_index,
                                             tab_handle=tab_handle))

    @mcp.tool(name="browser_wait_for_download",
              description="Wait for a new file to appear in ~/Downloads. filename_contains filters by name.")
    def _browser_wait_for_download(filename_contains: Optional[str] = None,
                                    timeout_s: int = 60) -> Dict[str, Any]:
        return _log(audit_logger, "browser_wait_for_download",
                    lambda: browser_wait_for_download(settings, filename_contains=filename_contains, timeout_s=timeout_s))

    @mcp.tool(name="browser_screenshot",
              description=(
                  "Legacy raw browser-window pixel capture. It does not target a specific background tab and may be "
                  "unreliable when the window is obscured. For AI visual grounding, background tabs, or full-page "
                  "capture, prefer browser_observe with visual='viewport', 'element', or 'full_page'."
              ))
    def _browser_screenshot(browser: str, path: Optional[str] = None,
                             window_index: int = 1, return_base64: bool = True) -> Dict[str, Any]:
        return _log(audit_logger, "browser_screenshot",
                    lambda: browser_screenshot(settings, browser=browser, path=path,
                                               window_index=window_index, return_base64=return_base64))

    @mcp.tool(name="browser_scroll",
              description=(
                  "Scrolls the page. If selector is provided, scrolls that element. "
                  "If selector is not provided, scrolls by dx and dy pixels. "
                  "Example: dy=500 scrolls down, dy=-500 scrolls up."
              ))
    def _browser_scroll(browser: str, dx: int = 0, dy: int = 300,
                        selector: Optional[str] = None, window_index: int = 1,
                        tab_index: Optional[int] = None,
                        tab_handle: Optional[str] = None) -> Dict[str, Any]:
        return _log(audit_logger, "browser_scroll",
                    lambda: browser_scroll(settings, browser=browser, dx=dx, dy=dy,
                                           selector=selector, window_index=window_index,
                                           tab_index=tab_index, tab_handle=tab_handle))

    @mcp.tool(name="browser_press_key",
              description=(
                  "Sends a keyboard key to the browser. "
                  "Key examples: 'return', 'escape', 'tab', 'space', 'delete', 'up', 'down', 'left', 'right', "
                  "'f5', 'a', 'A'. "
                  "modifiers listesi: ['cmd'], ['shift'], ['cmd','shift'] gibi. "
                  "Example: key='a', modifiers=['cmd'] sends Cmd+A."
              ))
    def _browser_press_key(browser: str, key: str, modifiers: Optional[List[str]] = None,
                            window_index: int = 1,
                            allow_foreground: bool = False) -> Dict[str, Any]:
        return _log(audit_logger, "browser_press_key",
                    lambda: browser_press_key(settings, browser=browser, key=key,
                                              modifiers=modifiers, window_index=window_index,
                                              allow_foreground=allow_foreground))

    @mcp.tool(name="browser_coordinate_click",
              description=(
                  "Clicks an absolute X/Y screen coordinate. "
                  "This is a foreground fallback and refuses to steal focus unless allow_foreground=true. "
                  "Prefer browser_act or selector-based clicks."
              ))
    def _browser_coordinate_click(browser: str, x: int, y: int,
                                   double_click: bool = False,
                                   window_index: int = 1,
                                   allow_foreground: bool = False) -> Dict[str, Any]:
        return _log(audit_logger, "browser_coordinate_click",
                    lambda: browser_coordinate_click(settings, browser=browser, x=x, y=y,
                                                     double_click=double_click, window_index=window_index,
                                                     allow_foreground=allow_foreground))

    @mcp.tool(name="browser_get_snapshot",
              description=(
                  "Returns the visible DOM tree. Each element includes tag, text, id, class, and "
                  "screen coordinates (rect.x, rect.y, rect.w, rect.h). "
                  "Use these coordinates with browser_coordinate_click. "
                  "Use max_depth and max_children to limit traversal."
              ))
    def _browser_get_snapshot(browser: str, window_index: int = 1,
                               tab_index: Optional[int] = None,
                               tab_handle: Optional[str] = None,
                               max_depth: int = 6, max_children: int = 25) -> Dict[str, Any]:
        return _log(audit_logger, "browser_get_snapshot",
                    lambda: browser_get_snapshot(settings, browser=browser, window_index=window_index,
                                                 tab_index=tab_index, tab_handle=tab_handle,
                                                 max_depth=max_depth,
                                                 max_children=max_children))

    @mcp.tool(
        name="mac_mcp_update",
        description=(
            "Check for or start a safe commit-based Mac MCP update. check_only=true only fetches and compares "
            "the deployed commit with origin/main. check_only=false starts a detached updater that preserves "
            "runtime customizations, backs up managed files, restarts Mac MCP, and rolls the runtime back if health fails."
        ),
    )
    async def _mac_mcp_update(check_only: bool = True, branch: str = "main") -> Dict[str, Any]:
        return await asyncio.to_thread(
            _log, audit_logger, "mac_mcp_update",
            lambda: mac_mcp_update(check_only=check_only, branch=branch),
        )

    # ── Memory tools ──────────────────────────────────────────────────────────
    @mcp.tool(
        name="memory_add",
        description=(
            "Append a timestamped memory to today's Europe/Istanbul Markdown journal. "
            "The server creates ~/.mac-mcp/memory/YYYY/MM/YYYY-MM-DD.md automatically and updates the SQLite search index."
        ),
    )
    async def _memory_add(content: str, tags: Optional[List[str]] = None,
                          importance: str = "normal", source: Optional[str] = None) -> Dict[str, Any]:
        return await asyncio.to_thread(
            _log, audit_logger, "memory_add",
            lambda: memory_add(content=content, tags=tags, importance=importance, source=source),
        )

    @mcp.tool(
        name="memory_search",
        description=(
            "Search or list Mac MCP memories. query enables hybrid SQLite FTS5 + local vector search; "
            "date/date_from/date_to filter time ranges. Query can be omitted to list memories chronologically."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
    )
    async def _memory_search(query: Optional[str] = None, date: Optional[str] = None,
                             date_from: Optional[str] = None, date_to: Optional[str] = None,
                             tags: Optional[List[str]] = None, importance: Optional[str] = None,
                             sort: str = "relevance", limit: int = 20) -> Dict[str, Any]:
        return await asyncio.to_thread(
            _log, audit_logger, "memory_search",
            lambda: memory_search(query=query, date=date, date_from=date_from, date_to=date_to,
                                  tags=tags, importance=importance, sort=sort, limit=limit),
        )

    @mcp.tool(
        name="memory_get",
        description="Get one exact memory by stable memory_id without running semantic search.",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
    )
    async def _memory_get(memory_id: str) -> Dict[str, Any]:
        return await asyncio.to_thread(_log, audit_logger, "memory_get", lambda: memory_get(memory_id))

    @mcp.tool(
        name="memory_update",
        description=(
            "Update an exact memory by memory_id. Without memory_id, use date/date_from/date_to to list timestamped "
            "candidate memories for selection without changing anything."
        ),
    )
    async def _memory_update(memory_id: Optional[str] = None, content: Optional[str] = None,
                             tags: Optional[List[str]] = None, importance: Optional[str] = None,
                             source: Optional[str] = None, date: Optional[str] = None,
                             date_from: Optional[str] = None, date_to: Optional[str] = None,
                             limit: int = 50) -> Dict[str, Any]:
        return await asyncio.to_thread(
            _log, audit_logger, "memory_update",
            lambda: memory_update(memory_id=memory_id, content=content, tags=tags, importance=importance,
                                  source=source, date=date, date_from=date_from, date_to=date_to, limit=limit),
        )

    @mcp.tool(
        name="memory_delete",
        description=(
            "Delete a memory by memory_id only when confirm=true. Without memory_id, date/date_from/date_to lists "
            "timestamped candidates for selection and does not delete anything."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False),
    )
    async def _memory_delete(memory_id: Optional[str] = None, confirm: bool = False,
                             date: Optional[str] = None, date_from: Optional[str] = None,
                             date_to: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
        return await asyncio.to_thread(
            _log, audit_logger, "memory_delete",
            lambda: memory_delete(memory_id=memory_id, confirm=confirm, date=date,
                                  date_from=date_from, date_to=date_to, limit=limit),
        )

    # ── Agent Skills tools ───────────────────────────────────────────────────
    @mcp.tool(
        name="skill_list",
        description=(
            "List indexed Agent Skills without loading full SKILL.md bodies. Returns name, description, and location "
            "for progressive disclosure."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
    )
    async def _skill_list(limit: int = 100) -> Dict[str, Any]:
        return await asyncio.to_thread(_log, audit_logger, "skill_list", lambda: skill_list(limit=limit))

    @mcp.tool(
        name="skill_search",
        description=(
            "Search Agent Skills with hybrid SQLite FTS5 + the same shared multilingual embedding worker used by memory_search. "
            "Returns skill metadata and SKILL.md paths; call skill_get to activate one."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
    )
    async def _skill_search(query: str, limit: int = 10) -> Dict[str, Any]:
        return await asyncio.to_thread(_log, audit_logger, "skill_search", lambda: skill_search(query=query, limit=limit))

    @mcp.tool(
        name="skill_get",
        description=(
            "Load one Agent Skill by name or SKILL.md path. Returns full SKILL.md content, skill directory, and bundled "
            "scripts/references/assets paths without eagerly loading resource contents."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
    )
    async def _skill_get(name: Optional[str] = None, path: Optional[str] = None, resource_limit: int = 200) -> Dict[str, Any]:
        return await asyncio.to_thread(
            _log, audit_logger, "skill_get",
            lambda: skill_get(name=name, path=path, resource_limit=resource_limit),
        )

    @mcp.tool(
        name="skill_register",
        description=(
            "Validate and register an existing Agent Skill directory or SKILL.md path. Managed skills under "
            "~/.mac-mcp/skills are discovered automatically; external skill paths can be registered explicitly."
        ),
    )
    async def _skill_register(path: str) -> Dict[str, Any]:
        return await asyncio.to_thread(_log, audit_logger, "skill_register", lambda: skill_register(path=path))

    @mcp.tool(
        name="skill_update_index",
        description="Rescan managed and registered SKILL.md files and rebuild changed skill index entries without starting FastEmbed.",
    )
    async def _skill_update_index() -> Dict[str, Any]:
        return await asyncio.to_thread(_log, audit_logger, "skill_update_index", skill_update_index)

    # ── Interactive tools ─────────────────────────────────────────────────────
    @mcp.tool(
        name="ask_user",
        description=(
            "Ask the local user an interactive question or request guidance. "
            "A native macOS dialog opens with your question/message at the top, "
            "and an input field for the user's answer. "
            "When the user sends an answer, the response is returned to you. "
            "Skip or timeout returns response=null. "
            "Use this to get approval, preferences, or missing information without stopping an autonomous task."
        ),
    )
    async def _ask_user(
        question: str,
        sender: str = "AI",
        timeout_s: int = 60,
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(
            _log,
            audit_logger,
            "ask_user",
            lambda: ask_user(settings, question=question, sender=sender, timeout_s=timeout_s),
        )

    @mcp.tool(
        name="ask_user_voice",
        title="Ask the user by voice",
        description=(
            "Speak a short natural question aloud on the local Mac, listen for the user's spoken answer, "
            "transcribe it, and return the response without opening a text dialog. "
            "Prefer one concise conversational sentence (roughly 15 words or fewer). "
            "The default Turkish neural voice is tr-TR-AhmetNeural; saying 'atla', 'iptal', 'boşver', or 'vazgeç' skips. "
            "This tool is experimental and the local user can disable it at runtime; if it returns "
            "experimental_tool_disabled, immediately fall back to ask_user. "
            "Use this when hands-free human input is useful during an autonomous task."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        structured_output=False,
    )
    async def _ask_user_voice(
        question: str,
        sender: str = "AI",
        timeout_s: Optional[int] = None,
        voice: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(
            _log,
            audit_logger,
            "ask_user_voice",
            lambda: ask_user_voice(
                settings,
                question=question,
                sender=sender,
                timeout_s=timeout_s,
                voice=voice,
            ),
        )

    @mcp.tool(
        name="ask_choice",
        title="Ask the user to choose",
        description=(
            "Open a native macOS dialog with 2-3 labeled choices and wait for the local user's selection. "
            "Two-choice dialogs show a Cancel button; three-choice dialogs use all three native buttons, "
            "and closing the window still cancels. Returns the selected choice and index. Timeout returns no choice. "
            "Use for preferences and reversible decisions; use ask_confirmation for explicit Yes/No approval."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        structured_output=False,
    )
    async def _ask_choice(
        question: str,
        choices: List[str],
        sender: str = "AI",
        timeout_s: int = 60,
        default_choice: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(
            _log,
            audit_logger,
            "ask_choice",
            lambda: ask_choice(
                settings,
                question=question,
                choices=choices,
                sender=sender,
                timeout_s=timeout_s,
                default_choice=default_choice,
            ),
        )

    @mcp.tool(
        name="ask_confirmation",
        title="Ask the user for confirmation",
        description=(
            "Open a native macOS Yes/No confirmation dialog. "
            "Only an explicit confirm button produces confirmed=true; deny, cancel, close, or timeout is false. "
            "Use before consequential or destructive actions."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        structured_output=False,
    )
    async def _ask_confirmation(
        question: str,
        sender: str = "AI",
        timeout_s: int = 60,
        confirm_label: str = "Yes",
        deny_label: str = "No",
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(
            _log,
            audit_logger,
            "ask_confirmation",
            lambda: ask_confirmation(
                settings,
                question=question,
                sender=sender,
                timeout_s=timeout_s,
                confirm_label=confirm_label,
                deny_label=deny_label,
            ),
        )

    # ── App setup ────────────────────────────────────────────────────────────
    app = mcp.streamable_http_app()
    app.add_middleware(SecurityMiddleware)

    async def health(_: Request) -> Response:
        return JSONResponse({"ok": True, "server": "mac-mcp", "workdir": str(settings.workdir)})

    app.router.routes.append(Route("/health", health, methods=["GET"]))
    app.router.routes.extend(create_dashboard_routes(telemetry, settings))

    # REST API — FastAPI sub-app mounted at /api
    from fastapi import FastAPI
    from .rest_routes import router as rest_router
    rest_app = FastAPI()

    @rest_app.middleware("http")
    async def _capture_rest_telemetry(request: Request, call_next):
        return await rest_telemetry_middleware(request, call_next, telemetry)

    rest_app.include_router(rest_router)
    app.mount("/api", rest_app)

    return app


app = create_app()
