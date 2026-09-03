from __future__ import annotations

import asyncio
from pathlib import Path
import json
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, Mount

from mcp.server.transport_security import TransportSecuritySettings
from .security import RateLimiter, Settings, authenticate, client_ip, load_settings, rate_limit, setup_audit_logger
from .tools_terminal import run_command, process_list, kill_process, get_system_info
from .tools_jobs import (
    start_background_job, get_job_status, get_job_output,
    stop_job, list_jobs, wait_jobs, run_commands_parallel,
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
from .tools_interactive import ask_choice, ask_confirmation, ask_user


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
    settings = load_settings()
    limiter = RateLimiter(settings.rate_limit_per_minute)
    audit_logger = setup_audit_logger()

    mcp = FastMCP(
        name="mac-mcp",
        instructions=(
            "You are connected to the user's local Mac through Mac MCP. "
            "Default home directory is the current user's home. "
            "Use run_command for shell work and the dedicated macOS/browser/UI tools when they fit better. "
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
                    token = authenticate(settings, request.headers.get("authorization"))
                    ip = client_ip(request)
                    rate_limit(limiter, token, ip)
                except HTTPException as exc:
                    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
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
              description="Open a URL in Safari or Google Chrome. browser: 'Safari' or 'Google Chrome'.")
    def _browser_open_url(browser: str, url: str, new_tab: bool = True) -> Dict[str, Any]:
        return _log(audit_logger, "browser_open_url",
                    lambda: browser_open_url(settings, browser=browser, url=url, new_tab=new_tab))

    @mcp.tool(name="browser_list_tabs",
              description="List all open tabs in Safari or Google Chrome.")
    def _browser_list_tabs(browser: str) -> Dict[str, Any]:
        return _log(audit_logger, "browser_list_tabs",
                    lambda: browser_list_tabs(settings, browser=browser))

    @mcp.tool(name="browser_activate_tab",
              description="Switch to a specific tab by window_index and tab_index.")
    def _browser_activate_tab(browser: str, window_index: int = 1, tab_index: int = 1) -> Dict[str, Any]:
        return _log(audit_logger, "browser_activate_tab",
                    lambda: browser_activate_tab(settings, browser=browser, window_index=window_index, tab_index=tab_index))

    @mcp.tool(name="browser_close_tab",
              description="Close a tab by window_index and tab_index.")
    def _browser_close_tab(browser: str, window_index: int = 1, tab_index: int = 1) -> Dict[str, Any]:
        return _log(audit_logger, "browser_close_tab",
                    lambda: browser_close_tab(settings, browser=browser, window_index=window_index, tab_index=tab_index))

    @mcp.tool(name="browser_execute_js",
              description="Execute JavaScript in a browser tab and return the result.")
    def _browser_execute_js(browser: str, js: str, window_index: int = 1,
                             tab_index: Optional[int] = None) -> Dict[str, Any]:
        return _log(audit_logger, "browser_execute_js",
                    lambda: browser_execute_js(settings, browser=browser, js=js,
                                               window_index=window_index, tab_index=tab_index))

    @mcp.tool(name="browser_click_selector",
              description="Click an element by CSS selector in a browser tab.")
    def _browser_click_selector(browser: str, css_selector: str, window_index: int = 1,
                                 tab_index: Optional[int] = None) -> Dict[str, Any]:
        return _log(audit_logger, "browser_click_selector",
                    lambda: browser_click_selector(settings, browser=browser, css_selector=css_selector,
                                                   window_index=window_index, tab_index=tab_index))

    @mcp.tool(name="browser_type_selector",
              description="Type text into an element by CSS selector. clear=true clears first.")
    def _browser_type_selector(browser: str, css_selector: str, text: str, clear: bool = True,
                                window_index: int = 1, tab_index: Optional[int] = None) -> Dict[str, Any]:
        return _log(audit_logger, "browser_type_selector",
                    lambda: browser_type_selector(settings, browser=browser, css_selector=css_selector,
                                                  text=text, clear=clear, window_index=window_index, tab_index=tab_index))

    @mcp.tool(name="browser_wait_for_selector",
              description="Wait until a CSS selector appears in the page. Returns found=true/false.")
    def _browser_wait_for_selector(browser: str, css_selector: str, timeout_s: int = 20,
                                    window_index: int = 1, tab_index: Optional[int] = None) -> Dict[str, Any]:
        return _log(audit_logger, "browser_wait_for_selector",
                    lambda: browser_wait_for_selector(settings, browser=browser, css_selector=css_selector,
                                                      timeout_s=timeout_s, window_index=window_index, tab_index=tab_index))

    @mcp.tool(name="browser_get_html",
              description="Get the full HTML of the current page in a browser tab.")
    def _browser_get_html(browser: str, max_chars: Optional[int] = None,
                          window_index: int = 1, tab_index: Optional[int] = None) -> Dict[str, Any]:
        return _log(audit_logger, "browser_get_html",
                    lambda: browser_get_html(settings, browser=browser, max_chars=max_chars,
                                             window_index=window_index, tab_index=tab_index))

    @mcp.tool(name="browser_wait_for_download",
              description="Wait for a new file to appear in ~/Downloads. filename_contains filters by name.")
    def _browser_wait_for_download(filename_contains: Optional[str] = None,
                                    timeout_s: int = 60) -> Dict[str, Any]:
        return _log(audit_logger, "browser_wait_for_download",
                    lambda: browser_wait_for_download(settings, filename_contains=filename_contains, timeout_s=timeout_s))

    @mcp.tool(name="browser_screenshot",
              description=(
                  "Captures only the browser window, not the full screen. "
                  "Use return_base64=true to return a base64 PNG. Use path to choose the output file."
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
                        tab_index: Optional[int] = None) -> Dict[str, Any]:
        return _log(audit_logger, "browser_scroll",
                    lambda: browser_scroll(settings, browser=browser, dx=dx, dy=dy,
                                           selector=selector, window_index=window_index, tab_index=tab_index))

    @mcp.tool(name="browser_press_key",
              description=(
                  "Sends a keyboard key to the browser. "
                  "Key examples: 'return', 'escape', 'tab', 'space', 'delete', 'up', 'down', 'left', 'right', "
                  "'f5', 'a', 'A'. "
                  "modifiers listesi: ['cmd'], ['shift'], ['cmd','shift'] gibi. "
                  "Example: key='a', modifiers=['cmd'] sends Cmd+A."
              ))
    def _browser_press_key(browser: str, key: str, modifiers: Optional[List[str]] = None,
                            window_index: int = 1) -> Dict[str, Any]:
        return _log(audit_logger, "browser_press_key",
                    lambda: browser_press_key(settings, browser=browser, key=key,
                                              modifiers=modifiers, window_index=window_index))

    @mcp.tool(name="browser_coordinate_click",
              description=(
                  "Clicks an absolute X/Y screen coordinate. "
                  "Use rect.x and rect.y values from browser_get_snapshot. "
                  "Set double_click=true to double-click."
              ))
    def _browser_coordinate_click(browser: str, x: int, y: int,
                                   double_click: bool = False,
                                   window_index: int = 1) -> Dict[str, Any]:
        return _log(audit_logger, "browser_coordinate_click",
                    lambda: browser_coordinate_click(settings, browser=browser, x=x, y=y,
                                                     double_click=double_click, window_index=window_index))

    @mcp.tool(name="browser_get_snapshot",
              description=(
                  "Returns the visible DOM tree. Each element includes tag, text, id, class, and "
                  "screen coordinates (rect.x, rect.y, rect.w, rect.h). "
                  "Use these coordinates with browser_coordinate_click. "
                  "Use max_depth and max_children to limit traversal."
              ))
    def _browser_get_snapshot(browser: str, window_index: int = 1,
                               tab_index: Optional[int] = None,
                               max_depth: int = 6, max_children: int = 25) -> Dict[str, Any]:
        return _log(audit_logger, "browser_get_snapshot",
                    lambda: browser_get_snapshot(settings, browser=browser, window_index=window_index,
                                                 tab_index=tab_index, max_depth=max_depth,
                                                 max_children=max_children))

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
        name="ask_choice",
        title="Ask the user to choose",
        description=(
            "Open a native macOS dialog with 2-6 labeled choices and wait for the local user's selection. "
            "Returns the selected choice and index. Cancel or timeout returns no choice. "
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

    # REST API — FastAPI sub-app mounted at /api
    from fastapi import FastAPI
    from .rest_routes import router as rest_router
    rest_app = FastAPI()
    rest_app.include_router(rest_router)
    app.mount("/api", rest_app)

    return app


app = create_app()
