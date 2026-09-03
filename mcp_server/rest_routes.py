"""
REST API routes for Custom GPT / OpenAPI access.
Mirrors the MCP tools as plain HTTP POST endpoints.
"""
from __future__ import annotations

from pathlib import Path
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from .security import Settings, authenticate, load_settings
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
from .tools_ui import act_ui, observe_ui

_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def require_auth(request: Request) -> str:
    settings = get_settings()
    return authenticate(settings, request.headers.get("authorization"))


router = APIRouter(dependencies=[Depends(require_auth)])


# ── Request models ────────────────────────────────────────────────────────────

class RunCommandRequest(BaseModel):
    command: str
    timeout_s: Optional[int] = None

class ProcessListRequest(BaseModel):
    filter: Optional[str] = None

class KillProcessRequest(BaseModel):
    pid: int
    signal: str = "TERM"

class FilesRequest(BaseModel):
    tool: str
    path: Optional[str] = None
    content: Optional[str] = None
    paths: Optional[List[str]] = None
    files: Optional[List[Dict[str, str]]] = None
    old_string: Optional[str] = None
    new_string: Optional[str] = None
    expected_replacements: Optional[int] = 1
    source: Optional[str] = None
    destination: Optional[str] = None
    recursive: Optional[bool] = False
    pattern: Optional[str] = None
    file_type: Optional[str] = "any"
    depth: Optional[int] = 3
    offset: Optional[int] = 0
    length: Optional[int] = None
    atomic: Optional[bool] = True

class MacOSRequest(BaseModel):
    tool: str
    script: Optional[str] = None
    title: Optional[str] = None
    message: Optional[str] = None
    sound: Optional[str] = "Pop"
    content: Optional[str] = None
    app_name: Optional[str] = None
    url: Optional[str] = None
    level: Optional[int] = None
    path: Optional[str] = None
    window: Optional[bool] = False
    notes: Optional[str] = ""
    due_date: Optional[str] = None
    timeout_s: Optional[int] = 30

class BrowserRequest(BaseModel):
    tool: str
    browser: Optional[str] = None
    url: Optional[str] = None
    new_tab: Optional[bool] = True
    window_index: Optional[int] = 1
    tab_index: Optional[int] = None
    js: Optional[str] = None
    css_selector: Optional[str] = None
    text: Optional[str] = None
    clear: Optional[bool] = True
    timeout_s: Optional[int] = 20
    max_chars: Optional[int] = None
    filename_contains: Optional[str] = None
    # browser_screenshot
    path: Optional[str] = None
    return_base64: Optional[bool] = True
    # browser_scroll
    dx: Optional[int] = 0
    dy: Optional[int] = 300
    selector: Optional[str] = None
    # browser_press_key
    key: Optional[str] = None
    modifiers: Optional[List[str]] = None
    # browser_coordinate_click
    x: Optional[int] = None
    y: Optional[int] = None
    double_click: Optional[bool] = False
    # browser_get_snapshot
    max_depth: Optional[int] = 6
    max_children: Optional[int] = 25

class SearchRequest(BaseModel):
    tool: str
    pattern: Optional[str] = None
    query: Optional[str] = None
    path: Optional[str] = None
    include_extensions: Optional[List[str]] = None
    case_sensitive: Optional[bool] = False
    max_results: Optional[int] = 50

class HttpRequest(BaseModel):
    url: str
    method: Optional[str] = "GET"
    headers: Optional[Dict[str, str]] = None
    body: Optional[str] = None

class InteractiveRequest(BaseModel):
    question: str
    sender: Optional[str] = "AI"
    timeout_s: Optional[int] = 60

class ChoiceRequest(BaseModel):
    question: str
    choices: List[str] = Field(..., min_length=2, max_length=3)
    sender: Optional[str] = "AI"
    timeout_s: Optional[int] = 60
    default_choice: Optional[str] = None

class ConfirmationRequest(BaseModel):
    question: str
    sender: Optional[str] = "AI"
    timeout_s: Optional[int] = 60
    confirm_label: Optional[str] = "Yes"
    deny_label: Optional[str] = "No"

class StartJobRequest(BaseModel):
    command: str
    cwd: Optional[str] = None
    env: Optional[Dict[str, str]] = None
    timeout_s: Optional[int] = None
    no_output_timeout_s: Optional[int] = None

class JobStatusRequest(BaseModel):
    job_id: str

class JobOutputRequest(BaseModel):
    job_id: str
    tail_lines: Optional[int] = None
    since_offset: Optional[int] = None
    stream: Optional[str] = "both"

class StopJobRequest(BaseModel):
    job_id: str
    signal: Optional[str] = "TERM"

class ListJobsRequest(BaseModel):
    status_filter: Optional[str] = None

class WaitJobsRequest(BaseModel):
    job_ids: List[str]
    timeout_s: Optional[int] = None
    return_output: Optional[bool] = False

class RunParallelRequest(BaseModel):
    commands: List[str]
    cwd: Optional[str] = None
    timeout_s: Optional[int] = None
    return_output: Optional[bool] = True



# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/run", operation_id="run_command")
def api_run(req: RunCommandRequest, settings: Settings = Depends(get_settings)) -> Dict[str, Any]:
    return run_command(settings, command=req.command, timeout_s=req.timeout_s)


@router.post("/system_info", operation_id="get_system_info")
def api_system_info(settings: Settings = Depends(get_settings)) -> Dict[str, Any]:
    return get_system_info(settings)


@router.post("/process_list", operation_id="process_list")
def api_process_list(req: ProcessListRequest, settings: Settings = Depends(get_settings)) -> Dict[str, Any]:
    return process_list(settings, filter=req.filter)


@router.post("/kill_process", operation_id="kill_process")
def api_kill_process(req: KillProcessRequest, settings: Settings = Depends(get_settings)) -> Dict[str, Any]:
    return kill_process(settings, pid=req.pid, signal=req.signal)


@router.post("/jobs/start", operation_id="start_background_job")
def api_jobs_start(req: StartJobRequest, settings: Settings = Depends(get_settings)) -> Dict[str, Any]:
    return start_background_job(
        settings,
        command=req.command,
        cwd=req.cwd,
        env=req.env,
        timeout_s=req.timeout_s,
        no_output_timeout_s=req.no_output_timeout_s,
    )


@router.post("/jobs/status", operation_id="get_job_status")
def api_jobs_status(req: JobStatusRequest, settings: Settings = Depends(get_settings)) -> Dict[str, Any]:
    return get_job_status(settings, job_id=req.job_id)


@router.post("/jobs/output", operation_id="get_job_output")
def api_jobs_output(req: JobOutputRequest, settings: Settings = Depends(get_settings)) -> Dict[str, Any]:
    return get_job_output(
        settings,
        job_id=req.job_id,
        tail_lines=req.tail_lines,
        since_offset=req.since_offset,
        stream=req.stream or "both",
    )


@router.post("/jobs/stop", operation_id="stop_job")
def api_jobs_stop(req: StopJobRequest, settings: Settings = Depends(get_settings)) -> Dict[str, Any]:
    return stop_job(settings, job_id=req.job_id, signal_name=req.signal or "TERM")


@router.post("/jobs/list", operation_id="list_jobs")
def api_jobs_list(req: ListJobsRequest, settings: Settings = Depends(get_settings)) -> Dict[str, Any]:
    return list_jobs(settings, status_filter=req.status_filter)


@router.post("/jobs/wait", operation_id="wait_jobs")
def api_jobs_wait(req: WaitJobsRequest, settings: Settings = Depends(get_settings)) -> Dict[str, Any]:
    return wait_jobs(
        settings,
        job_ids=req.job_ids,
        timeout_s=req.timeout_s,
        return_output=req.return_output or False,
    )


@router.post("/run_parallel", operation_id="run_commands_parallel")
def api_run_parallel(req: RunParallelRequest, settings: Settings = Depends(get_settings)) -> Dict[str, Any]:
    return run_commands_parallel(
        settings,
        commands=req.commands,
        cwd=req.cwd,
        timeout_s=req.timeout_s,
        return_output=req.return_output if req.return_output is not None else True,
    )


@router.post("/files", include_in_schema=False)
def api_files(req: FilesRequest, settings: Settings = Depends(get_settings)) -> Dict[str, Any]:
    t = req.tool
    if t == "read_file":
        return read_file(settings, path=req.path, offset=req.offset or 0, length=req.length)
    elif t == "read_multiple_files":
        return read_multiple_files(settings, paths=req.paths or [])
    elif t == "write_file":
        return write_file(settings, path=req.path, content=req.content or "")
    elif t == "write_files_batch":
        return write_files_batch(settings, files=req.files or [], atomic=req.atomic)
    elif t == "edit_file":
        return edit_file(settings, path=req.path, old_string=req.old_string,
                         new_string=req.new_string, expected_replacements=req.expected_replacements or 1)
    elif t == "move_file":
        return move_file(settings, source=req.source, destination=req.destination)
    elif t == "copy_file":
        return copy_file(settings, source=req.source, destination=req.destination)
    elif t == "delete_path":
        return delete_path(settings, path=req.path, recursive=req.recursive or False)
    elif t == "list_directory":
        return list_directory(settings, path=req.path)
    elif t == "directory_tree":
        return directory_tree(settings, path=req.path, depth=req.depth or 3)
    elif t == "create_directory":
        return create_directory(settings, path=req.path)
    elif t == "get_file_info":
        return get_file_info(settings, path=req.path)
    elif t == "find_files":
        return find_files(settings, pattern=req.pattern, path=req.path or str(Path.home()),
                          file_type=req.file_type or "any")
    else:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown file tool: {t}")


@router.post("/macos", include_in_schema=False)
def api_macos(req: MacOSRequest, settings: Settings = Depends(get_settings)) -> Dict[str, Any]:
    t = req.tool
    if t == "run_applescript":
        return run_applescript(settings, script=req.script, timeout_s=req.timeout_s or 30)
    elif t == "send_notification":
        return send_notification(settings, title=req.title, message=req.message, sound=req.sound or "Pop")
    elif t == "clipboard_get":
        return clipboard_get(settings)
    elif t == "clipboard_set":
        return clipboard_set(settings, content=req.content or "")
    elif t == "open_app":
        return open_app(settings, app_name=req.app_name)
    elif t == "open_url":
        return open_url(settings, url=req.url)
    elif t == "set_volume":
        return set_volume(settings, level=req.level)
    elif t == "get_volume":
        return get_volume(settings)
    elif t == "set_brightness":
        return set_brightness(settings, level=req.level)
    elif t == "screenshot":
        return screenshot(settings, path=req.path or str(Path.home() / "Desktop" / "screenshot.png"),
                          window=req.window or False)
    elif t == "set_reminder":
        return set_reminder(settings, title=req.title, notes=req.notes or "", due_date=req.due_date)
    elif t == "get_running_apps":
        return get_running_apps(settings)
    else:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown macOS tool: {t}")


@router.post("/browser", include_in_schema=False)
def api_browser(req: BrowserRequest, settings: Settings = Depends(get_settings)) -> Dict[str, Any]:
    t = req.tool
    if t == "browser_open_url":
        return browser_open_url(settings, browser=req.browser, url=req.url, new_tab=req.new_tab)
    elif t == "browser_list_tabs":
        return browser_list_tabs(settings, browser=req.browser)
    elif t == "browser_activate_tab":
        return browser_activate_tab(settings, browser=req.browser,
                                    window_index=req.window_index or 1, tab_index=req.tab_index or 1)
    elif t == "browser_close_tab":
        return browser_close_tab(settings, browser=req.browser,
                                 window_index=req.window_index or 1, tab_index=req.tab_index or 1)
    elif t == "browser_execute_js":
        return browser_execute_js(settings, browser=req.browser, js=req.js,
                                  window_index=req.window_index or 1, tab_index=req.tab_index)
    elif t == "browser_click_selector":
        return browser_click_selector(settings, browser=req.browser, css_selector=req.css_selector,
                                      window_index=req.window_index or 1, tab_index=req.tab_index)
    elif t == "browser_type_selector":
        return browser_type_selector(settings, browser=req.browser, css_selector=req.css_selector,
                                     text=req.text, clear=req.clear, window_index=req.window_index or 1,
                                     tab_index=req.tab_index)
    elif t == "browser_wait_for_selector":
        return browser_wait_for_selector(settings, browser=req.browser, css_selector=req.css_selector,
                                         timeout_s=req.timeout_s or 20, window_index=req.window_index or 1,
                                         tab_index=req.tab_index)
    elif t == "browser_get_html":
        return browser_get_html(settings, browser=req.browser, max_chars=req.max_chars,
                                window_index=req.window_index or 1, tab_index=req.tab_index)
    elif t == "browser_wait_for_download":
        return browser_wait_for_download(settings, filename_contains=req.filename_contains,
                                         timeout_s=req.timeout_s or 60)
    elif t == "browser_screenshot":
        return browser_screenshot(settings, browser=req.browser, path=req.path,
                                  window_index=req.window_index or 1,
                                  return_base64=req.return_base64 if req.return_base64 is not None else True)
    elif t == "browser_scroll":
        return browser_scroll(settings, browser=req.browser, dx=req.dx or 0, dy=req.dy or 300,
                              selector=req.selector, window_index=req.window_index or 1,
                              tab_index=req.tab_index)
    elif t == "browser_press_key":
        return browser_press_key(settings, browser=req.browser, key=req.key,
                                 modifiers=req.modifiers, window_index=req.window_index or 1)
    elif t == "browser_coordinate_click":
        return browser_coordinate_click(settings, browser=req.browser, x=req.x, y=req.y,
                                        double_click=req.double_click or False,
                                        window_index=req.window_index or 1)
    elif t == "browser_get_snapshot":
        return browser_get_snapshot(settings, browser=req.browser, window_index=req.window_index or 1,
                                    tab_index=req.tab_index, max_depth=req.max_depth or 6,
                                    max_children=req.max_children or 25)
    else:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown browser tool: {t}")


@router.post("/search", include_in_schema=False)
def api_search(req: SearchRequest, settings: Settings = Depends(get_settings)) -> Dict[str, Any]:
    t = req.tool
    if t == "search_files":
        return search_files(settings, pattern=req.pattern, path=req.path or str(Path.home()),
                            include_extensions=req.include_extensions, case_sensitive=req.case_sensitive)
    elif t == "spotlight_search":
        return spotlight_search(settings, query=req.query, max_results=req.max_results or 50)
    else:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown search tool: {t}")


@router.post("/http", operation_id="http_request")
def api_http(req: HttpRequest, settings: Settings = Depends(get_settings)) -> Dict[str, Any]:
    return http_request(settings, url=req.url, method=req.method or "GET",
                        headers=req.headers, body=req.body)


@router.post("/interactive", operation_id="ask_user")
def api_interactive(req: InteractiveRequest, settings: Settings = Depends(get_settings)) -> Dict[str, Any]:
    return ask_user(
        settings,
        question=req.question,
        sender=req.sender or "AI",
        timeout_s=req.timeout_s or 60,
    )


@router.post("/interactive/choice", operation_id="ask_choice")
def api_interactive_choice(req: ChoiceRequest, settings: Settings = Depends(get_settings)) -> Dict[str, Any]:
    return ask_choice(
        settings,
        question=req.question,
        choices=req.choices,
        sender=req.sender or "AI",
        timeout_s=req.timeout_s or 60,
        default_choice=req.default_choice,
    )


@router.post("/interactive/confirmation", operation_id="ask_confirmation")
def api_interactive_confirmation(req: ConfirmationRequest, settings: Settings = Depends(get_settings)) -> Dict[str, Any]:
    return ask_confirmation(
        settings,
        question=req.question,
        sender=req.sender or "AI",
        timeout_s=req.timeout_s or 60,
        confirm_label=req.confirm_label or "Yes",
        deny_label=req.deny_label or "No",
    )


# ── One-to-one OpenAPI aliases ───────────────────────────────────────────────
# Keep the original grouped endpoints above for compatibility, while exposing
# one stable REST operation per MCP tool to Custom GPT Actions.

_FILE_ALIAS_TOOLS = (
    "write_file", "write_files_batch", "read_file", "read_multiple_files",
    "edit_file", "move_file", "copy_file", "delete_path", "list_directory",
    "directory_tree", "create_directory", "get_file_info", "find_files",
)
_MACOS_ALIAS_TOOLS = (
    "run_applescript", "send_notification", "clipboard_get", "clipboard_set",
    "open_app", "open_url", "set_volume", "get_volume", "set_brightness",
    "screenshot", "set_reminder", "get_running_apps",
)
_BROWSER_ALIAS_TOOLS = (
    "browser_open_url", "browser_list_tabs", "browser_activate_tab",
    "browser_close_tab", "browser_execute_js", "browser_click_selector",
    "browser_type_selector", "browser_wait_for_selector", "browser_get_html",
    "browser_wait_for_download", "browser_screenshot", "browser_scroll",
    "browser_press_key", "browser_coordinate_click", "browser_get_snapshot",
)
_SEARCH_ALIAS_TOOLS = ("search_files", "spotlight_search")


def _request_payload(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return dict(payload or {})


def _make_group_alias(handler: Any, request_model: Any, tool_name: str) -> Any:
    def endpoint(
        payload: Optional[Dict[str, Any]] = Body(default=None),
        settings: Settings = Depends(get_settings),
    ) -> Dict[str, Any]:
        data = _request_payload(payload)
        data["tool"] = tool_name
        return handler(request_model(**data), settings)

    endpoint.__name__ = f"api_{tool_name}"
    endpoint.__qualname__ = endpoint.__name__
    return endpoint


def _payload_value(data: Dict[str, Any], key: str, default: Any) -> Any:
    value = data.get(key)
    return default if value is None else value


def _api_mac_observe_alias(
    payload: Optional[Dict[str, Any]] = Body(default=None),
    settings: Settings = Depends(get_settings),
) -> Any:
    data = _request_payload(payload)
    return observe_ui(
        settings,
        app=data.get("app"),
        window_index=_payload_value(data, "window_index", 1),
        max_depth=_payload_value(data, "max_depth", 5),
        max_children=_payload_value(data, "max_children", 30),
        include_screenshot=_payload_value(data, "include_screenshot", True),
        ocr=_payload_value(data, "ocr", False),
    )


def _api_mac_act_alias(
    payload: Optional[Dict[str, Any]] = Body(default=None),
    settings: Settings = Depends(get_settings),
) -> Any:
    data = _request_payload(payload)
    actions = data.get("actions")
    if not isinstance(actions, list):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "mac_act requires an 'actions' array")
    return act_ui(
        settings,
        actions=actions,
        observation_id=data.get("observation_id"),
        app=data.get("app"),
        return_state=_payload_value(data, "return_state", True),
        allow_risky=_payload_value(data, "allow_risky", False),
    )


for _tool_name in _FILE_ALIAS_TOOLS:
    router.add_api_route(
        f"/{_tool_name}",
        _make_group_alias(api_files, FilesRequest, _tool_name),
        methods=["POST"],
        name=_tool_name,
        operation_id=_tool_name,
    )

for _tool_name in _MACOS_ALIAS_TOOLS:
    router.add_api_route(
        f"/{_tool_name}",
        _make_group_alias(api_macos, MacOSRequest, _tool_name),
        methods=["POST"],
        name=_tool_name,
        operation_id=_tool_name,
    )

for _tool_name in _BROWSER_ALIAS_TOOLS:
    router.add_api_route(
        f"/{_tool_name}",
        _make_group_alias(api_browser, BrowserRequest, _tool_name),
        methods=["POST"],
        name=_tool_name,
        operation_id=_tool_name,
    )

for _tool_name in _SEARCH_ALIAS_TOOLS:
    router.add_api_route(
        f"/{_tool_name}",
        _make_group_alias(api_search, SearchRequest, _tool_name),
        methods=["POST"],
        name=_tool_name,
        operation_id=_tool_name,
    )

router.add_api_route(
    "/mac_observe",
    _api_mac_observe_alias,
    methods=["POST"],
    name="mac_observe",
    operation_id="mac_observe",
)
router.add_api_route(
    "/mac_act",
    _api_mac_act_alias,
    methods=["POST"],
    name="mac_act",
    operation_id="mac_act",
)
