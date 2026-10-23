from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
from pathlib import Path
import hashlib
import json
import os
import re
import threading
import time
import uuid
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import urlencode, urlsplit

from fastapi import HTTPException, status
from mcp.types import ToolAnnotations
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, Mount

from mcp.server.transport_security import TransportSecuritySettings
from .request_client import client_address
from .workflow_checkpoints import clear_not_executed
from .log_retention import start_log_rotation
from . import privacy_inventory, privacy_policy, recipes, run_evidence
from . import scheduled_task_runner, scheduled_tasks
from . import external_mcp, external_mcp_routes
from .host_guard import HostGuard, extra_hosts_from_env
from .public_endpoint import resolve_public_endpoint
from .security import BASE_DIR, AuthFailureLimiter, _effective_bind_host, _loopback_host, RateLimiter, Settings, auth_failure_response_detail, authenticate, ensure_dashboard_token, load_settings, rate_limit, request_authorization, setup_audit_logger, validate_bootstrap_security
from .observability import ObservedFastMCP, TelemetryManager, _tool_can_mutate, current_run_id, current_security_session
from . import error_contract
from .tool_summaries import ADVANCED_BROWSER_TOOLS, CORE_TOOL_NAMES, CORE_TOOL_SUMMARIES
from . import tool_discovery
from . import iphone
from .browser_schemas import (
    ActReturnState, BrowserActions, DoReturnState, ObserveScope, OptionalBrowserActions, VisualMode,
)
from mcp.server.fastmcp.exceptions import ToolError
from .policy import (
    PROFILES, Capability, current_policy_context, declared_risk, environment_policy_context, reset_policy_context,
    set_policy_context, strip_client_prefix,
)
from .policy_scope import ScopeRequest, evaluate_scope
from .scoped_auth import resolve_request_identity
from .dashboard_routes import create_dashboard_routes, rest_telemetry_middleware
from .mobile_routes import create_mobile_routes
from .chatgpt_panel import register_chatgpt_panel
from .browser_sign_in import browser_sign_in
from .chrome_background_bridge import create_chrome_background_bridge_routes
from .tools_terminal import run_command, process_list, kill_process, get_system_info
from .tools_jobs import (
    start_background_job, get_job_status, get_job_output,
    stop_job, list_jobs, delete_job, wait_jobs, run_commands_parallel,
)
from .tools_agents import (
    inherited_provenance_for,
    AGENTS_DIR, agent_catalog, spawn_agent, spawn_agents, wait_agents,
    list_agents, get_agent, agent_action, start_agent_history_expiry,
)
from .file_transactions import prune_transactions
from .private_storage import secure_content_stores
from .tools_files import (
    write_file, write_files_batch, read_file, read_multiple_files,
    edit_file, move_file, copy_file, delete_path, file_transaction_batch, undo_file_transaction,
    list_directory, directory_tree, create_directory, get_file_info, find_files,
)
from .tools_macos import (
    run_applescript, send_notification, clipboard_get, clipboard_set,
    open_app, open_url, set_volume, get_volume, set_brightness,
    screenshot, set_reminder, get_running_apps,
)
from .tools_ui import observe_ui, act_ui
from .artifact_pipeline import artifact_pipeline
from .context_handoff import context_handoff
from .tools_snapshot import unified_read_snapshot
from .computer_plan import ComputerPlanError, _unwrap_tool_result, derive_computer_plan_resources, execute_computer_plan
from .app_adapters import mac_app
from . import agent_message_bridge, tools_team_messages
from . import fleet, fleet_client
from .fleet_routes import create_fleet_routes
from .tools_search import search_files, spotlight_search
from .tools_http import http_request
from .tools_browser import (
    browser_open_url, browser_list_tabs, browser_activate_tab, browser_close_tab,
    browser_execute_js, browser_click_selector, browser_type_selector,
    browser_wait_for_selector, browser_get_html, browser_wait_for_download, browser_upload_artifact,
    browser_screenshot, browser_scroll, browser_press_key,
    browser_coordinate_click, browser_get_snapshot,
)
from .tools_browser_agent import (
    _run_json_js, browser_observe, browser_find, browser_act, normalize_act_actions, semantic_extract_fields,
)
from .tools_browser import _norm_browser
from . import browser_checkpoint, browser_tabs
from .tool_cancellation import cancellable_sleep
from .tools_interactive import ask_confirmation, ask_user, ask_user_mcp_result
from .tools_voice import ask_user_voice, voice_capture
from . import storage_review as storage_review_tool
from . import workspaces as workspace_tool
from . import personal_search as personal_search_tool
from . import daily_briefing as daily_briefing_tool
from . import applescript_host, ax_native, ax_watch
from .tools_update import mac_mcp_update
from .tools_memory import memory_add, memory_search, memory_get, memory_update, memory_delete
from . import knowledge
from .tools_lessons import (
    lesson_consolidate, lesson_delete, lesson_export, lesson_feedback, lesson_record, lesson_search,
)
from .tools_skills import skill_list, skill_search, skill_get, skill_register, skill_update_index
from .menu_app_bootstrap import bootstrap_menu_app_and_legacy_state
from .runtime_settings import tool_activity_setting
from .cli_bootstrap import ensure_cli_launcher
from .post_update_health import get_or_start_post_update_health_gate, pending_update_context
from .data_guard import format_security_approval_question
from .security_context import SecurityContextManager
from .agent_admission import AdmissionError, normalize_claims as normalize_admission_claims


MCP_AGENT_INSTRUCTIONS = (
    "You are connected to the user's local Mac through Mac MCP. "
    "Default home directory is the current user's home. "
    "Routing order: dedicated semantic tool first, then shell/file API, then browser DOM, with native UI only as a fallback. "
    "Use open_app to launch apps. "
    "Shell mode: run_command for a short command you wait on; start_background_job for long builds, servers or watchers, "
    "then get_job_output/wait_jobs and stop_job; run_commands_parallel only "
    "for independent commands. "
    "If a dedicated capability is unavailable or policy-denied, do not reproduce the same side effect through Terminal, AppleScript, or generic UI; policy denial is not a fallback reason. "
    "Which tool for what (tool_discover finds any tool not in your list): "
    "questions about the owner's own documents (contracts, invoices, notes, PDFs) -> knowledge_search (their chosen "
    "folders; cites file and page); answer from its snippets, read_file for more, and when it reports "
    "no folders, tell the owner they can add one in Mac MCP Settings > Privacy. It limits nothing else: search_files and "
    "read_file still work anywhere. "
    "Mail, Notes, Calendar, Reminders, Contacts, Finder, Messages, Shortcuts -> mac_app adapters before UI automation "
    "(action=capabilities lists them; sending asks the person first); one search across them and those folders -> "
    "personal_search; today at a glance -> daily_briefing. "
    "Memory: memory_search before asking the user about earlier preferences, facts or decisions. Learn as you work, "
    "without being asked: when the user states or shows a lasting preference, corrects you, decides something, or you "
    "learn a stable fact about their work, people, projects, accounts or setup that will matter later, memory_search "
    "for it and then memory_add one short self-contained sentence with tags, or memory_update the memory it changes. "
    "Work silently: do not ask first or say you saved or recalled anything; use it naturally. Never save secrets, "
    "passwords or codes, one-off task details, guesses, or other people's private details; if private mode refuses "
    "it, go on without it. "
    "Before an unfamiliar or previously failed workflow -> lesson_search, then lesson_feedback on whether it helped; "
    "after solving something non-obvious or being corrected on how to do a task, lesson_record it so the next run "
    "starts from it; "
    "skill_search and skill_get for documented procedures. "
    "A multi-step UI job -> one computer_plan; a job worth repeating -> computer_plan(save_as_recipe=...) and later "
    "recipe(action='run'); work at a set time -> scheduled_tasks (tasks you create wait for the owner's approval). "
    "Long or parallel work -> spawn_agents, then wait_agents. Agents working at the same time talk through "
    "team_messages: you and the team you start (you are its lead) can message each other while it runs, to pass "
    "on a plan change without a restart, and Mac MCP sessions can join a named room; their messages "
    "reach you on their own after your tool calls (how: skill_get name='mac-mcp-team-coordination'). "
    "The owner's other Macs -> fleet; their iPhone -> iphone. "
    "Missing information or a decision mid-task -> ask_user instead of stopping; go on with the answer or your "
    "judgment; a sign-in, 2FA or captcha step -> browser_checkpoint. "
    "Tools named <server>__<tool> come from MCP servers the owner added and follow the same rules. "
    "An error saying observation is paused or private mode is on is the owner's choice: stop that part and say so, "
    "do not work around it. "
    "Perception ladder: start with mac_snapshot or semantic browser/native observation; reuse previous_observation_id "
    "for delta/not_modified reads; escalate to targeted element/window visual only when semantic state is insufficient; "
    "use OCR or full-page/full-screen visual last. For browser visual grounding, prefer visual='element' or 'viewport' "
    "before visual='full_page'. All visual paths can target background resources without focusing them. "
    "For form filling and repetitive browser interactions, batch independent actions; never field-by-field unless dependencies require it. "
    "A whole form is one browser_act action {type:'fill', values:{'<field label>': '<value>', ...}} (text boxes, selects, "
    "checkboxes as true/false); browser_observe hands back a ready fill_template when it sees form fields. "
    "Typing into a field replaces ALL of its text, even when only part is selected (clear:false appends at the end); "
    "partial edits are refused, so to change one word type the field's full new text. "
    "A browser action's ok means it ran, not that its result happened: check outcome, and add expect (e.g. "
    "{text:'Pending review'}, {gone:'Choose an image'}) to clicks whose effect matters (copy link, insert image, "
    "submit) instead of reporting page_reacted as success. "
    "Split browser action groups when an earlier action materially changes later controls, stale-target or human-takeover risk requires re-observation, "
    "or a consequential step needs a separate verification boundary. "
    "Browser routes: interactive work on a page is browser_observe -> one batched browser_act -> verify; a bounded one-shot "
    "workflow (open a URL, optionally act, extract fields, optionally close) is a single browser_do. Selector and coordinate "
    "browser tools are advanced compatibility tools reached through tool_discover; use them only when browser_act cannot. "
    "Browser tabs: pass the tab_handle from browser_list_tabs or browser_do on every browser call. "
    "At a login form, call browser_sign_in (via tool_invoke) instead of asking for or typing a password; it fills the owner's "
    "saved login for that site without revealing it. "
    "browser_act and browser_do resolve query/role/within targets themselves; use browser_find only to read, never as a step before acting. Each browser_act "
    "returns changes (url/title change, elements that appeared with ids you can act on next, messages, invalid fields, an "
    "open modal), so continue or verify from it instead of observing again; a missed target returns near_matches. "
    "When every item repeats the same controls (a Reply under each comment, a button on each card), do not browser_find each control: "
    "send one browser_act whose actions all carry within='a phrase that appears only in that item', e.g. click Reply -> type into role=textbox "
    "-> click the submit control with role=button -> wait for:text with the posted text. "
    "Posting a comment or reply is one action, {type:'post', text, within?}: it finds the comment box (within one item for "
    "a reply, opening it with that item's Reply button when closed), types, presses that box's own submit button and confirms the text is on the page; posted=null means check "
    "before posting again. browser_observe lists what is on screen to act on. To read a page (an article, a product "
    "page with its reviews, docs, a thread), one browser_do {url, actions:[{type:'read'}]} returns all its text in one "
    "call, content that loads on scroll included; a longer page continues with offset=next_offset. Do not page through "
    "it with extract or observe; read puts the page back where it was. To move the page and work there, browser_act "
    "{type:'scroll', dy:<px>} (or a target to bring into view) leaves it scrolled. For a repeating list whose items "
    "you need one by one, browser_act {type:'scan'} with item_selector collects them. "
    "Prefer the smallest number of tool calls and smallest bounded context that safely completes and verifies the task."
)

BROWSER_OBSERVE_DESCRIPTION = (
    "BATCH-FIRST HINT: multiple independent controls -> one browser_act for all follow-ups -> verify from its changes. "
    "Re-observe only for dependency/rerender, stale/takeover risk, or consequential verification. "
    "High-level browser observation. Returns compact DOM with stable e1/e2 IDs; optional JPEG visuals keep the DOM list in the same response. "
    "When multiple independent actionable form controls are present or discoverable, follow this observation with one browser_act "
    "containing all independent interactions instead of repeated field-by-field observe/action calls. Re-observe between action groups only when an "
    "earlier action materially changes later controls, stale-target/takeover risk requires it, or a consequential step needs separate verification. "
    "It reads what is on screen: scope interactive (controls, default), visible (controls and text), content or leaf "
    "(text and controls in reading order). To read a long page or thread, use one browser_act {type:'scan'} (scrolls and "
    "collects items) instead of scroll and observe rounds. When it sees form fields it adds fill_template, a ready "
    "{type:'fill'} action: put in the values and send it as one browser_act. visual: none, viewport, element, or full_page. "
    "Start semantic-only (visual='none'); pass previous_observation_id on repeated reads so unchanged DOM returns compact not_modified instead of resending the element list. "
    "Escalate to visual='element' or 'viewport' only when semantic state is insufficient; reserve full_page for true full-page grounding. Visual capture is rendered "
    "inside the target tab DOM and returned as MCP image content without activating Safari/Chrome, switching tabs, scrolling the page, or leaving screenshot files on disk."
)

BROWSER_ACT_DESCRIPTION = (
    "BATCH-FIRST: observe once -> one browser_act with independent type/select/click/scroll -> verify from its changes. "
    "Custom dropdowns: select. Targets: element_id or query/role/text_match. Split only for dependencies. "
    "Prefer one browser_act call containing all independent actions instead of one call per field. "
    "Recommended workflow: one browser_observe -> one batched browser_act -> verify from its returned changes (url/title change, "
    "new controls with ids, messages, invalid fields, an open modal); observe again only when changes do not answer it. "
    "Off-screen targets are scrolled into view; a missed target returns near_matches. "
    "Combine independent type/select/click/scroll actions in the same actions list; custom dropdowns can use select. "
    "Targets may use stable element_id or semantic query/role/text_match, so element IDs are not always required. "
    "Split into separate action groups only when an earlier action materially changes later controls, stale-target or human-takeover risk requires re-observation, "
    "or a consequential step needs separate verification. Perform up to 20 browser actions in one MCP call. Supports click, type, async custom select, scroll, key and waits. "
    "Editing text: type replaces ALL existing text in the field, even when only part of it is selected; clear:false appends "
    "at the end; partial edits are refused (a selected word, a range), never turned "
    "into replace-all. To edit part of a draft, read the field and type its full new text. "
    "Each action reports outcome: confirmed (its result was checked), page_reacted (the page changed, but nothing "
    "checked the intended result: a menu closing is not a link copied, a request is not a submission), unconfirmed, "
    "failed or blocked. Give an action expect={text:'Pending review'} / {gone:'Choose an image'} / {selector} / "
    "{url_change:true} (timeout_s, default 5) to confirm its result, or expect={download:true} on a click that downloads "
    "a file (it returns the file as an artifact); an unmet expect is expectation_not_met with "
    "outcome_unknown=true, never retried automatically. "
    "Key actions (Enter, Escape, Tab, arrows, characters) run as background DOM keyboard events by default and Enter submits the input's form, "
    "so type then key Enter commits search boxes and date fields in the same batch. After an earlier action changes the page, a missing target is awaited "
    "briefly (set wait_s per action to change it), so picker confirm buttons and autocomplete options can follow in the same batch. "
    "Click actions default to background-safe synthetic DOM input; input_mode='trusted' is an explicit Chrome Background Companion-only pointer path and fails closed on Safari "
    "without foreground/coordinate fallback. No-effect mutations are never automatically replayed. return_state: none, compact, or full. "
    "When labels repeat (two Continue buttons), add an optional per-action intent such as 'Continue in the Billing section'; "
    "it is used only to break such ties and never carries typed values. "
    "When every item repeats the same controls (a Reply under each comment), give each action within='a phrase unique to that item' "
    "(or within_element_id): matches are limited to that item, nearest first, and a phrase found in several items fails as "
    "WITHIN_ANCHOR_AMBIGUOUS instead of guessing. Reply flows fit one call: click Reply within X -> type into role=textbox within X "
    "-> click the submit control within X with role=button (a same-named link may be a bookmark) -> wait for the posted text. "
    "Shorter: {type:'post', text, within?} is that whole flow as one action: it finds the comment box (with within, the "
    "item's own reply box, opened with its Reply button when closed; field='label words' picks another), types, presses the box's own "
    "submit button (submit='button text' overrides) and confirms the text appears outside the box (timeout_s, default 10). "
    "posted=true is confirmed; POST_NOT_CONFIRMED (posted=null) means the button was pressed: check the page before posting again. "
    "Forms: {type:'fill', values:{label: value}} fills every field in one action: text is typed, native and custom selects "
    "pick the option, checkboxes and radios take true/false; fields go in the given order (one revealed by an earlier choice "
    "is awaited), and a field that is not found is listed in failed_fields while the others are still filled. "
    "A click that opens a popup or target=_blank tab: follow it with wait for='new_tab' (optional url_contains); it returns "
    "the new tab_handle without activating it, or ambiguous_new_tabs / NO_NEW_TAB. "
    "Reading a page: {type:'read'} returns all the text the page shows in one action (content that loads on scroll "
    "included; offset=next_offset continues a longer page, load_more:false reads it as it is). It scrolls to load and "
    "then the page is put back where it was. Moving the page yourself: {type:'scroll', dy:<px>} (negative is up, dx "
    "sideways; with element_id/query it brings that element into view instead); the page stays where it was scrolled. "
    "Long or virtualized lists (feeds, results, virtual tables): one {type:'scan'} action scrolls the list's container "
    "(element_id/query, or the largest scrollable area) in bounded steps and returns de-duplicated items "
    "(key_field text|href, item_selector optional) with stopped=end_of_list|no_new_items|max_items|max_steps|timeout|payload_budget. "
    "Chrome: hover moves the real pointer onto a target (CSS :hover menus); drag presses, moves and releases from the "
    "target to to_element_id / to_query / dx,dy. A native alert/confirm/prompt surfaces as browser_dialog_open with its "
    "text; answer it only with {type:'dialog', decision:'accept'|'dismiss'} (prompt_text for prompt()). "
    "Canvas apps (observe reports canvas.canvas_heavy): click/double_click/hover with x,y in viewport CSS px from a "
    "visual='viewport' observation (pass its observation_id; a scrolled or resized page is refused as stale_coordinates), "
    "and key with input_mode='trusted' for real key events (Chrome). "
    "Cross-origin frames (observe lists them under frames): pass frame='<part of the frame URL>' to browser_observe, "
    "browser_find and browser_act to work inside that frame with its own element ids (Chrome); results carry frame."
)

_BROWSER_DO_OUTPUT_BUDGET_BYTES = 8_192


def _browser_json_bytes(payload: Any) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))


def _clip_browser_value(value: Any, *, max_string: int, max_items: int, depth: int = 0) -> Any:
    if depth >= 4:
        return str(value)[:max_string]
    if isinstance(value, str):
        return value if len(value) <= max_string else value[: max(0, max_string - 1)] + "…"
    if isinstance(value, list):
        return [
            _clip_browser_value(item, max_string=max_string, max_items=max_items, depth=depth + 1)
            for item in value[:max_items]
        ]
    if isinstance(value, dict):
        return {
            str(key): _clip_browser_value(item, max_string=max_string, max_items=max_items, depth=depth + 1)
            for key, item in value.items()
        }
    return value


def _fit_extraction_page(payload: Dict[str, Any], budget: int) -> Dict[str, Any]:
    """Pack exact prefixes, moving continuation only for content actually returned."""
    out = dict(payload)
    if "state" in out:
        out.pop("state")
        out["state_truncated"] = True
    for key in ("url", "title"):
        if isinstance(out.get(key), str):
            out[key] = out[key][:500]
    if "errors" in out:
        out["errors"] = _clip_browser_value(out["errors"], max_string=200, max_items=2)
    out.update(data={}, fields={}, complete=False, truncated=True, output_truncated=True)
    source_fields = payload["fields"]
    for name, meta in source_fields.items():
        start = int(meta.get("offset", 0))
        more = start < int(meta.get("eligible_count", 0))
        out["fields"][name] = {**meta, "returned_count": 0, "has_more": more,
            "continuation": {"offset": start, "text_offset": int(meta.get("text_offset", 0))} if more else None}
        out["data"][name] = [] if isinstance(payload.get("data", {}).get(name), list) else None
    # A caller can ask for fewer fields if metadata alone exceeds the budget.
    # Return an explicit failure, never a successful apparently complete page.
    if _browser_json_bytes(out) > budget:
        return {"ok": False, "error": "extraction_metadata_budget", "complete": False,
                "truncated": True, "output_truncated": True, "tab_handle": payload.get("tab_handle"),
                "hint": "Repeat extraction with fewer fields or use browser_act."}
    exhausted = False
    for name, meta in source_fields.items():
        raw = payload.get("data", {}).get(name)
        values = raw if isinstance(raw, list) else [] if raw is None else [raw]
        for index, value in enumerate(values):
            value = str(value)
            def put(length: int) -> None:
                chunks = values[:index] + [value[:length]]
                out["data"][name] = chunks if isinstance(raw, list) else chunks[0]
                if length < len(value):
                    continuation = {"offset": int(meta.get("offset", 0)) + index,
                                    "text_offset": (int(meta.get("text_offset", 0)) if index == 0 else 0) + len(value[:length].encode("utf-16-le")) // 2}
                elif index + 1 < len(values):
                    continuation = {"offset": int(meta.get("offset", 0)) + index + 1, "text_offset": 0}
                else:
                    continuation = meta.get("continuation")
                out["fields"][name] = {**meta, "returned_count": len(chunks),
                    "has_more": continuation is not None, "continuation": continuation}
            put(len(value))
            if _browser_json_bytes(out) <= budget:
                continue
            low, high = 0, len(value)
            while low < high:
                mid = (low + high + 1) // 2
                put(mid)
                if _browser_json_bytes(out) <= budget:
                    low = mid
                else:
                    high = mid - 1
            if low:
                put(low)
            else:
                chunks = values[:index]
                out["data"][name] = chunks if isinstance(raw, list) else None
                continuation = {"offset": int(meta.get("offset", 0)) + index,
                                "text_offset": int(meta.get("text_offset", 0)) if index == 0 else 0}
                out["fields"][name] = {**meta, "returned_count": index, "has_more": True, "continuation": continuation}
            exhausted = True
            break
        if exhausted:
            break
    out["truncated"] = any(meta.get("has_more") for meta in out["fields"].values())
    out["complete"] = not out["truncated"]
    return out


# browser_do gives each scan this share of its answer; the scan reads no more than that.
_BROWSER_DO_SCAN_BYTES = _BROWSER_DO_OUTPUT_BUDGET_BYTES // 2


def _fit_browser_do_output(payload: Dict[str, Any], budget_bytes: int = _BROWSER_DO_OUTPUT_BUDGET_BYTES) -> Dict[str, Any]:
    """Keep normal browser_do responses bounded; debug mode bypasses this helper.

    Scan records are never cut here: the page's cursor has already moved past them, so a
    cut record would be lost. The scan itself was capped to fit (_BROWSER_DO_SCAN_BYTES).
    A read is the page text asked for; it is bounded by its own max_chars and next_offset."""
    scans = {key: payload[key] for key in ("scan", "scans", "read") if key in payload}
    if scans:
        rest = {key: value for key, value in payload.items() if key not in scans}
        room = max(1_024, int(budget_bytes) - _browser_json_bytes(scans))
        return {**_fit_browser_do_output(rest, room), **scans}
    budget = max(1_024, min(int(budget_bytes), 64_000))
    if _browser_json_bytes(payload) <= budget:
        return payload
    if payload.get("fields") and isinstance(payload.get("data"), dict):
        return _fit_extraction_page(payload, budget)

    out = dict(payload)
    out["output_truncated"] = True

    state = out.get("state")
    if isinstance(state, dict):
        light_state = {
            key: state.get(key)
            for key in ("ok", "url", "title", "scroll", "dom_revision", "active_element")
            if state.get(key) is not None
        }
        out["state"] = _clip_browser_value(light_state, max_string=700, max_items=3)
        out["state_truncated"] = True

    if isinstance(out.get("errors"), list):
        out["errors"] = _clip_browser_value(out["errors"], max_string=500, max_items=3)
    if isinstance(out.get("data"), dict):
        out["data"] = _clip_browser_value(out["data"], max_string=1_000, max_items=4)

    for max_string, max_items in ((600, 3), (320, 2), (180, 1)):
        if _browser_json_bytes(out) <= budget:
            break
        if isinstance(out.get("data"), dict):
            out["data"] = _clip_browser_value(out["data"], max_string=max_string, max_items=max_items)
        if isinstance(out.get("state"), dict):
            out["state"] = _clip_browser_value(out["state"], max_string=max_string, max_items=max_items)

    if _browser_json_bytes(out) > budget:
        out.pop("state", None)
        out["state_truncated"] = True
    if _browser_json_bytes(out) > budget:
        out.pop("errors", None)
    if _browser_json_bytes(out) > budget and isinstance(out.get("data"), dict):
        out["data"] = {
            str(key): _clip_browser_value(value, max_string=120, max_items=1)
            for key, value in list(out["data"].items())[:12]
        }
    if _browser_json_bytes(out) > budget:
        out = _clip_browser_value(out, max_string=160, max_items=2)
        out["output_truncated"] = True
    if _browser_json_bytes(out) > budget:
        core_data = out.get("data")
        if isinstance(core_data, dict):
            core_data = {
                str(key): _clip_browser_value(value, max_string=100, max_items=1)
                for key, value in list(core_data.items())[:8]
            }
        core: Dict[str, Any] = {
            key: out.get(key)
            for key in ("ok", "url", "title", "tab_handle", "closed")
            if key in out
        }
        if core_data is not None:
            core["data"] = core_data
        core = _clip_browser_value(core, max_string=100, max_items=1)
        core["output_truncated"] = True
        out = core
    return out


def _with_privacy_source(result: Any, source: Dict[str, Any]) -> Any:
    """Add privacy_source to a dict result or to compact JSON text (alone or before an image)."""
    if isinstance(result, dict):
        result.setdefault("privacy_source", source)
        return result
    if isinstance(result, str) and result.startswith("{"):
        try:
            payload = json.loads(result)
        except ValueError:
            return result
        if isinstance(payload, dict) and "privacy_source" not in payload:
            payload["privacy_source"] = source
            return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return result
    if isinstance(result, list) and result and isinstance(result[0], str):
        return [_with_privacy_source(result[0], source), *result[1:]]
    return result


_AUDIT_ERROR_CODE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_]{2,47})\s*:")


def _audit_outcome(status_code: int, detail: Any) -> str:
    """The audit log keeps the status and, when the message starts with one, its error code; never the
    message itself, which can hold paths, arguments or other caller text."""
    match = _AUDIT_ERROR_CODE.match(str(detail or ""))
    return f"error:{status_code}:{match.group(1)}" if match else f"error:{status_code}"


def _log(audit_logger, tool: str, fn):
    start = time.perf_counter()
    outcome = "ok"
    try:
        # The owner's pause and private mode (Settings > Privacy) apply to every caller.
        privacy_policy.check_tool(tool)
        source_token = privacy_policy.begin_source()
        try:
            result = fn()
        finally:
            source = privacy_policy.end_source(source_token)
        if source:
            # What this call read and the rule that let it, so the owner can see why.
            result = _with_privacy_source(result, source)
        return result
    except HTTPException as exc:
        outcome = _audit_outcome(exc.status_code, exc.detail)
        raise
    except Exception as exc:
        outcome = f"error:500:{type(exc).__name__}"
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Server error: {exc}") from exc
    finally:
        ms = int((time.perf_counter() - start) * 1000)
        audit_logger.info(json.dumps({"tool": tool, "outcome": outcome, "duration_ms": ms}))


def _unwrap_tool_invoke_result(result: Any) -> Any:
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict):
        return result[1].get("result", result[1])
    if isinstance(result, dict):
        return result.get("result", result)
    if isinstance(result, (list, tuple)):
        converted = []
        for item in result:
            if hasattr(item, "model_dump"):
                converted.append(item.model_dump(mode="json"))
            elif isinstance(item, dict):
                converted.append(item)
            else:
                converted.append(str(item))
        if len(converted) == 1 and isinstance(converted[0], dict) and converted[0].get("type") == "text":
            text_value = converted[0].get("text")
            if isinstance(text_value, str):
                try:
                    return json.loads(text_value)
                except json.JSONDecodeError:
                    return converted
        return converted
    if hasattr(result, "model_dump"):
        return result.model_dump(mode="json")
    return result


def _tool_payload_ok(payload: Any) -> bool:
    if isinstance(payload, dict) and isinstance(payload.get("ok"), bool):
        return bool(payload["ok"])
    return True


def _tool_input_schema(info: Any) -> Dict[str, Any]:
    schema = getattr(info, "inputSchema", None) or getattr(info, "parameters", None) or {}
    return schema if isinstance(schema, dict) else {}


def _split_images(result: Any) -> tuple[list, Any]:
    """A tool's pictures (a screenshot) and the rest of its result: pictures stay pictures through tool_invoke
    instead of turning into base64 text, which costs far more and can overflow the client's limit."""
    content = result[0] if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], (list, tuple)) else result
    if not isinstance(content, (list, tuple)):
        return [], result
    images = [block for block in content if getattr(block, "type", None) == "image"]
    if not images:
        return [], result
    return images, [block for block in content if getattr(block, "type", None) != "image"]


async def _invoke_registered_tool(mcp: ObservedFastMCP, tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
    target = strip_client_prefix(str(tool_name or "").strip(), lambda name: mcp._tool_manager.get_tool(name) is not None)
    if not target or target in {"tool_discover", "tool_invoke"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A non-fallback target tool_name is required.")
    if mcp._tool_manager.get_tool(target) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown tool: {target}")
    result = await mcp.call_tool(target, arguments or {})
    images, rest = _split_images(result)
    payload = _unwrap_tool_invoke_result(rest)
    tool_ok = _tool_payload_ok(payload)
    wrapper = {
        "ok": tool_ok,
        "invocation_ok": True,
        "tool_ok": tool_ok,
        "tool": target,
        "result": payload,
    }
    if images:
        return [json.dumps(wrapper, ensure_ascii=False, separators=(",", ":")), *images]
    return wrapper


DEFAULT_SESSION_IDLE_TIMEOUT_S = 1800.0


def session_idle_timeout_s() -> float:
    """How long an MCP session may sit without a request before the server forgets it.

    Only requests count: an open event stream does not. `mac-mcp stdio` sets a forgotten session up
    again on its own; MAC_MCP_SESSION_IDLE_TIMEOUT_S (60 s to 1 day) helps clients that do not.
    """
    raw = os.getenv("MAC_MCP_SESSION_IDLE_TIMEOUT_S", "").strip()
    try:
        value = float(raw) if raw else DEFAULT_SESSION_IDLE_TIMEOUT_S
    except ValueError:
        value = DEFAULT_SESSION_IDLE_TIMEOUT_S
    return max(60.0, min(value, 86400.0))


def _caller_may_write() -> bool:
    """Whether this call's profile allows writing on this Mac at all (a read-only caller writes nothing)."""
    profile = PROFILES.get(current_policy_context().profile)
    return profile is not None and (profile.allowed_capabilities is None
                                    or Capability.LOCAL_WRITE in profile.allowed_capabilities)


def create_app():
    bootstrap_menu_app_and_legacy_state()
    ensure_cli_launcher(strict=False)
    prune_transactions()
    settings = load_settings()
    validate_bootstrap_security(settings)
    limiter = RateLimiter(settings.rate_limit_per_minute)
    auth_failures = AuthFailureLimiter()
    audit_logger = setup_audit_logger()
    telemetry = TelemetryManager()
    dashboard_token = ensure_dashboard_token()
    # Stateful Streamable HTTP sessions are additionally bound to the credential
    # identity resolved by our custom security middleware. FastMCP's built-in
    # session-owner binding only applies when its own auth middleware is used.
    mcp_session_owners: Dict[str, tuple[str, float]] = {}
    head_probe_sessions: Dict[str, tuple[str, float]] = {}
    mcp_session_owner_ttl_s = 3600.0

    def security_approval(payload: Dict[str, Any]) -> Dict[str, Any]:
        return ask_confirmation(
            settings, question=format_security_approval_question(payload),
            sender="Mac MCP Security", timeout_s=60,
            confirm_label="Allow Once", deny_label="Block",
        )

    security_context = SecurityContextManager()
    security_context.set_child_seed_loader(inherited_provenance_for)
    mcp = ObservedFastMCP(
        telemetry=telemetry,
        security_context=security_context,
        name="mac-mcp",
        instructions=MCP_AGENT_INSTRUCTIONS,
        streamable_http_path="/mcp",
        stateless_http=False,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        security_approval_provider=security_approval,
        intent_descriptions_provider=lambda: bool(
            tool_activity_setting("require_descriptions", False)
        ),
    )

    def current_provenance_class() -> str:
        pair = current_security_session()
        if pair is None:
            return "local"
        state = mcp.security_context.state_for_public_session(pair[1])
        return str((state or {}).get("provenance_class") or "local")

    class SecurityMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            if request.url.path.startswith("/mcp"):
                query_api_keys = request.query_params.getlist("ApiKey")

                # Scrub URL credentials before any early return so local access
                # logs never retain the API key. Preserve all unrelated params.
                if query_api_keys:
                    clean_pairs = [
                        (key, value)
                        for key, value in request.query_params.multi_items()
                        if key != "ApiKey"
                    ]
                    request.scope["query_string"] = urlencode(clean_pairs, doseq=True).encode("utf-8")

                # Verified peer address: forwarding headers count only from the
                # configured tunnel, so spoofed X-Forwarded-For cannot split buckets.
                ip = client_address(request)
                if auth_failures.blocked(ip):
                    return JSONResponse(
                        {"detail": auth_failure_response_detail()}, status_code=429, headers={"Retry-After": "60"},
                    )
                try:
                    authorization = request_authorization(
                        settings,
                        request.headers.get("authorization"),
                        query_api_keys,
                    )
                    rate_key, policy_context = resolve_request_identity(settings, authorization)
                except HTTPException as exc:
                    if exc.status_code == 401:
                        auth_failures.record_failure(ip)
                    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
                try:
                    rate_limit(limiter, rate_key, ip)
                except HTTPException as exc:
                    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

                owner_key = hashlib.sha256(str(rate_key).encode("utf-8")).hexdigest()
                now = time.monotonic()
                for probe_id, (_owner, expires_at) in list(head_probe_sessions.items()):
                    if expires_at <= now:
                        head_probe_sessions.pop(probe_id, None)
                for session_id, (_owner, last_seen) in list(mcp_session_owners.items()):
                    if now - last_seen > mcp_session_owner_ttl_s:
                        mcp_session_owners.pop(session_id, None)

                if request.method == "HEAD" and request.url.path == "/mcp":
                    # Preserve the historical connector probe response, but mark
                    # this ID as a one-shot probe rather than a real MCP session.
                    probe_id = uuid.uuid4().hex
                    head_probe_sessions[probe_id] = (owner_key, now + 60.0)
                    return Response(status_code=200, headers={
                        "content-type": "text/event-stream; charset=utf-8",
                        "mcp-session-id": probe_id,
                    })

                request_session_id = request.headers.get("mcp-session-id")
                if request_session_id:
                    probe = head_probe_sessions.get(request_session_id)
                    if probe is not None:
                        probe_owner, _expires_at = probe
                        if probe_owner != owner_key:
                            return JSONResponse({"detail": "Session not found"}, status_code=404)
                        # Some connector probes replay the HEAD session header on
                        # their first POST. Strip that synthetic ID so FastMCP can
                        # create a real stateful session and return its own ID.
                        request.scope["headers"] = [
                            (key, value) for key, value in request.scope.get("headers", [])
                            if key.lower() != b"mcp-session-id"
                        ]
                        head_probe_sessions.pop(request_session_id, None)
                        request_session_id = None
                    else:
                        owner_record = mcp_session_owners.get(request_session_id)
                        if owner_record is not None:
                            expected_owner, _last_seen = owner_record
                            if expected_owner != owner_key:
                                return JSONResponse({"detail": "Session not found"}, status_code=404)
                            mcp_session_owners[request_session_id] = (owner_key, now)

                context_token = set_policy_context(policy_context)
                try:
                    response = await call_next(request)
                finally:
                    reset_policy_context(context_token)

                response_session_id = response.headers.get("mcp-session-id")
                if response_session_id:
                    mcp_session_owners[response_session_id] = (owner_key, now)
                if request.method == "DELETE" and request_session_id:
                    mcp_session_owners.pop(request_session_id, None)
                elif response.status_code == 404 and request_session_id:
                    mcp_session_owners.pop(request_session_id, None)
                return response
            return await call_next(request)

    # ── Terminal tools ──────────────────────────────────────────────────────
    @mcp.tool(
        name="run_command",
        description=(
            "Run any shell command in zsh on the local Mac. Full access by default. "
            "Set reversible=true to capture bounded filesystem side effects under reversible_root (defaults to the command workspace). "
            "Git workspaces use commit-backed clean-file preimages plus snapshots for dirty/untracked files; non-Git workspaces use bounded snapshots. "
            "join_transaction_ids can combine earlier committed file-tool transactions with this shell run into one compound undo receipt. "
            "require_full_reversibility=true fails before command execution when known policy exclusions or capture limits prevent full scoped coverage. "
            "Generated/ignored/symlink-escape or otherwise unsupported paths are reported explicitly and never silently claimed reversible."
        ),
    )
    def _run_command(
        command: str,
        timeout_s: Optional[int] = None,
        reversible: bool = False,
        reversible_root: Optional[str] = None,
        join_transaction_ids: Optional[List[str]] = None,
        require_full_reversibility: bool = False,
    ) -> Dict[str, Any]:
        return _log(
            audit_logger,
            "run_command",
            lambda: run_command(
                settings,
                command=command,
                timeout_s=timeout_s,
                reversible=reversible,
                reversible_root=reversible_root,
                join_transaction_ids=join_transaction_ids,
                require_full_reversibility=require_full_reversibility,
            ),
        )

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

    @mcp.tool(
        name="start_background_job",
        description=(
            "Start a shell command and return immediately with job_id. "
            "Default timeout is 60 seconds; set timeout_s explicitly (up to 600) for longer npm installs, builds, downloads, dev servers, docker, or tests. "
            "Set reversible=true for durable bounded filesystem capture; the watcher finalizes the transaction on exit and get_job_status can recover/finalize a capture after bridge restart when the PID has ended. "
            "reversible_root, join_transaction_ids and require_full_reversibility use the same semantics as run_command."
        ),
    )
    def _start_background_job(
        command: str,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        timeout_s: Optional[int] = None,
        no_output_timeout_s: Optional[int] = None,
        reversible: bool = False,
        reversible_root: Optional[str] = None,
        join_transaction_ids: Optional[List[str]] = None,
        require_full_reversibility: bool = False,
    ) -> Dict[str, Any]:
        return _log(
            audit_logger,
            "start_background_job",
            lambda: start_background_job(
                settings,
                command=command,
                cwd=cwd,
                env=env,
                timeout_s=timeout_s,
                no_output_timeout_s=no_output_timeout_s,
                reversible=reversible,
                reversible_root=reversible_root,
                join_transaction_ids=join_transaction_ids,
                require_full_reversibility=require_full_reversibility,
            ),
        )

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
              description=(
                  "List background jobs, newest first (limit, default 50; total says how many matched). "
                  "status_filter can be running, stalled, completed, failed, timeout, killed. "
                  "page.has_more + page.next_cursor continue the list. "
                  "Finished jobs expire by age, count and size (see retention)."
              ))
    def _list_jobs(status_filter: Optional[str] = None, limit: int = 50,
                   cursor: Optional[str] = None) -> Dict[str, Any]:
        return _log(audit_logger, "list_jobs",
                    lambda: list_jobs(settings, status_filter=status_filter, limit=limit, cursor=cursor))

    @mcp.tool(name="delete_job",
              description="Delete a finished background job: its metadata and both output streams. Stop a running job first.",
              annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False))
    def _delete_job(job_id: str) -> Dict[str, Any]:
        return _log(audit_logger, "delete_job", lambda: delete_job(settings, job_id=job_id))

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
        description="List available OpenCode, Codex, Claude Code, Kimi Code, Grok Build, Antigravity, pi, Cline, Kilo Code, Goose and GitHub Copilot providers, models, reasoning options and supported access modes without starting an agent.",
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
            "Delegate one task to OpenCode, Codex, Claude Code (provider=claude), Kimi Code (kimi), Grok Build (grok), Antigravity (antigravity, full access only), "
            "pi, Cline, Kilo Code (kilo), Goose or GitHub Copilot (copilot; model openrouter/<model> runs on that provider's key) in a non-blocking background process. "
            "When provider/model/reasoning are omitted, Mac MCP resolves the saved Settings > Subagents Default Agent preset; "
            "explicit fields always override saved values and model/reasoning never cross provider boundaries. "
            "Claude Code takes a model alias (fable, opus, sonnet, haiku) or a claude-... id and reasoning=low|medium|high|xhigh|max. "
            "Supports idle timeout and bounded retries. workspace_write Git tasks default to git_isolation=auto, which uses an "
            "ephemeral per-agent worktree when it can be scope-confined; git_isolation=required fails closed if isolation cannot be "
            "enforced, and off preserves the original cwd. Optional role=coder|reviewer|orchestrator injects only relevant, approved "
            "role lessons with bounded context; candidates never auto-activate. "
            "read_only and workspace_write run every provider inside a macOS Seatbelt filesystem boundary; full is unrestricted."
        ),
    )
    def _spawn_agent(prompt: str, provider: Optional[str] = None, model: Optional[str] = None,
                     reasoning: Optional[str] = None, cwd: Optional[str] = None,
                     timeout_s: Optional[int] = None, title: Optional[str] = None,
                     result_style: Literal["concise", "detailed"] = "concise", access_mode: str = "workspace_write",
                     idle_timeout_s: Optional[int] = None, retries: int = 0,
                     scope: Optional[Dict[str, Any]] = None,
                     capability_profile: Optional[str] = None,
                     role: Optional[str] = None,
                     git_isolation: str = "auto") -> Dict[str, Any]:
        context = current_policy_context()
        return _log(audit_logger, "spawn_agent",
                    lambda: privacy_inventory.with_provider_notice(spawn_agent(settings, provider=provider, prompt=prompt, model=model,
                                        reasoning=reasoning, cwd=cwd, timeout_s=timeout_s,
                                        title=title, result_style=result_style, access_mode=access_mode,
                                        idle_timeout_s=idle_timeout_s, retries=retries, scope=scope,
                                        parent_scope=context.scope, parent_profile=context.profile,
                                        capability_profile=capability_profile, role=role,
                                        provenance_class=current_provenance_class(), git_isolation=git_isolation)))

    @mcp.tool(
        name="spawn_agents",
        description=(
            "Spawn 1-10 background agent tasks as one team. When provider/model/reasoning are omitted, the saved "
            "Settings > Subagents Default Agent preset is resolved once for the team; explicit fields override it and "
            "saved model/reasoning never cross provider boundaries. Optional task.id + depends_on create a bounded DAG; "
            "max_parallel limits concurrent nodes. A reviewer task may set review_of=<task id> and must end with "
            "QUALITY_GATE: PASS or FAIL; FAIL can trigger up to max_revisions bounded revisions. Team-level admission controls include "
            "team_timeout_s, max_team_retries, admission_tool_call_budget and admission_token_budget; max_parallel is the concurrency budget. "
            "These usage budgets are admission thresholds, not hard runtime caps: once observed usage reaches a threshold, no new DAG node "
            "or retry is admitted, while already-running agents may finish and overshoot. max_total_tool_calls/max_total_tokens remain deprecated "
            "aliases for compatibility and have the same admission-only semantics. "
            "Retries are adaptive and only transient/retry-safe failures are replayed. Children inherit provider, model and reasoning "
            "unless a task sets its own (e.g. a reviewer on another provider); all share access_mode and git_isolation. workspace_write Git children default to separate ephemeral worktrees; task revisions reuse the "
            "same isolated worktree while sibling tasks remain separated. A task may add resources=[{kind,id,mode,expected_revision?}] "
            "for global cross-team admission of workspace/path/file, browser_tab, native_app/window, process or clipboard resources; "
            "path/browser claims are scope-checked and file expected_revision fails closed before provider start. "
            "Optional team role or task.role enables bounded role-learning context per child. "
            "Each child produces a versioned typed result envelope; valid structured output is preserved, plain legacy text is "
            "adapted with explicit legacy_fallback status, and malformed marked envelopes fail closed (a read_only non-reviewer child keeps its plain report with contract_status=invalid and no structured claims). Dependency fan-in is deterministic, "
            "provenance-aware, deduplicates evidence/artifacts, flags keyed claim contradictions, and never injects raw provider logs. "
            "conflict_policy=advisory (default) keeps success but reports unresolved contradictions; review or fail withholds "
            "team success until a reviewer settles them with resolutions. A child reporting success with errors counts as partial_failure. "
            "Returns immediately with parent-visible budget and global admission/queue state."
        ),
    )
    def _spawn_agents(tasks: List[Dict[str, Any]], provider: Optional[str] = None, model: Optional[str] = None,
                      reasoning: Optional[str] = None, cwd: Optional[str] = None,
                      timeout_s: Optional[int] = None, idle_timeout_s: Optional[int] = None,
                      retries: int = 1, result_style: Literal["concise", "detailed"] = "concise",
                      access_mode: str = "read_only", title: Optional[str] = None,
                      scope: Optional[Dict[str, Any]] = None,
                      capability_profile: Optional[str] = None,
                      role: Optional[str] = None,
                      max_parallel: Optional[int] = None, max_revisions: int = 1,
                      team_timeout_s: Optional[int] = None, max_team_retries: Optional[int] = None,
                      admission_tool_call_budget: Optional[int] = None,
                      admission_token_budget: Optional[int] = None,
                      max_total_tool_calls: Optional[int] = None, max_total_tokens: Optional[int] = None,
                      git_isolation: str = "auto", conflict_policy: str = "advisory") -> Dict[str, Any]:
        context = current_policy_context()
        return _log(audit_logger, "spawn_agents",
                    lambda: privacy_inventory.with_provider_notice(spawn_agents(settings, tasks=tasks, provider=provider, model=model,
                                         reasoning=reasoning, cwd=cwd, timeout_s=timeout_s,
                                         idle_timeout_s=idle_timeout_s, retries=retries,
                                         result_style=result_style, access_mode=access_mode, title=title,
                                         scope=scope, parent_scope=context.scope, parent_profile=context.profile,
                                         capability_profile=capability_profile, role=role,
                                         provenance_class=current_provenance_class(), max_parallel=max_parallel,
                                         max_revisions=max_revisions, team_timeout_s=team_timeout_s,
                                         max_team_retries=max_team_retries,
                                         admission_tool_call_budget=admission_tool_call_budget,
                                         admission_token_budget=admission_token_budget,
                                         max_total_tool_calls=max_total_tool_calls,
                                         max_total_tokens=max_total_tokens, git_isolation=git_isolation,
                                         conflict_policy=conflict_policy)))

    @mcp.tool(
        name="wait_agents",
        description=(
            "Bounded wait for a team or explicit agent_ids. mode: all, any, majority. "
            "any/majority quorum counts only successful completed work; all preserves completion semantics while "
            "success/outcome separately report failures. timed_out refers only to the waiter deadline. "
            "Returns concise typed result envelopes for agents that finished, including explicit truncation/omission metadata when "
            "bounded output is reduced; raw provider logs are excluded unless separately requested through get_agent(include_logs=true)."
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

    @mcp.tool(name="team_messages", description=tools_team_messages.DESCRIPTION)
    async def _team_messages(action: str = "inbox", body: Optional[str] = None, to: Optional[str] = None,
                             kind: str = "update", reply_to: Optional[str] = None, room: Optional[str] = None,
                             team_id: Optional[str] = None, unread_only: bool = True, limit: int = 10,
                             dedupe_key: Optional[str] = None, after: int = 0) -> Dict[str, Any]:
        return await asyncio.to_thread(
            _log, audit_logger, "team_messages",
            lambda: tools_team_messages.team_messages(
                action=action, body=body, to=to, kind=kind, reply_to=reply_to, room=room, team_id=team_id,
                unread_only=unread_only, limit=limit, dedupe_key=dedupe_key, after=after,
            ),
        )

    @mcp.tool(
        name="fleet",
        description=(
            "Work on the owner's other Macs, paired in Settings > Devices (#147), without connecting to each one. "
            "action=list: each Mac's name, model, whether it is online, busy or offline, and what this Mac may do "
            "there (status, read_only or full, never more than that Mac's own permission setting; refresh=true asks "
            "each one now). With read_only or full access: action=tools (mac = its name or peer_id; optional query "
            "searches its tools like tool_discover) lists only the tools this Mac may use there, and action=call "
            "(mac, tool, arguments) runs one of them there, e.g. run_command, read_file, browser_do, mac_app. "
            "action=recipes and action=run_recipe (recipe_id, values; full access) use its saved recipes. Everything "
            "runs under that Mac's own permission profile and approvals (its approval panels show on that Mac); a "
            "Busy Mac takes nothing until its owner turns it back on; an unanswered call may or may not have run, so "
            "check before repeating it. Pairing and access are the owner's, in Settings."
        ),
    )
    async def _fleet(action: str = "list", mac: Optional[str] = None, tool: Optional[str] = None,
                     arguments: Optional[Dict[str, Any]] = None, query: Optional[str] = None,
                     recipe_id: Optional[str] = None, values: Optional[Dict[str, Any]] = None,
                     refresh: bool = False) -> Dict[str, Any]:
        verb = str(action or "list").strip().lower()
        try:
            if verb == "list":
                peers = await fleet_client.refresh_all(force=bool(refresh))
                return {"ok": True, "this_mac": fleet.identity()["name"], "count": len(peers), "macs": peers}
            if not mac:
                raise fleet.FleetError("FLEET_ARGUMENT_INVALID", f"action={verb} needs mac (a name or peer_id).")
            if verb == "tools":
                return await fleet_client.tools(mac, query)
            if verb == "call":
                return await fleet_client.call(mac, str(tool or ""), arguments)
            if verb == "recipes":
                return await fleet_client.recipes(mac)
            if verb == "run_recipe":
                if not recipe_id:
                    raise fleet.FleetError("FLEET_ARGUMENT_INVALID", "action=run_recipe needs recipe_id.")
                return await fleet_client.run_recipe(mac, recipe_id, values)
            raise fleet.FleetError("FLEET_ARGUMENT_INVALID", "action is list, tools, call, recipes or run_recipe.")
        except fleet.FleetError as exc:
            return {"ok": False, "error": exc.code.lower(), "reason_code": exc.code, "message": str(exc)}

    @mcp.tool(
        name="iphone",
        description=(
            "The owner's iPhone, through iPhone Mirroring on this Mac (Settings > Devices > iPhone). action=see returns "
            "the screen (pass previous_hash from the last result: an unchanged screen comes back as unchanged, with no "
            "image); read returns its text with positions (Vision OCR); find (text, or texts for several) gives where "
            "they are. region limits reading to part of the screen, faster and fewer words: header, top, middle, bottom, "
            "footer or {top,bottom,left,right}; a picture costs less than the text of a busy screen, so read a region "
            "rather than all of it. Coordinates are phone points, origin top left, in the size see returns. With full "
            "access, do a whole "
            "job in one call with action=act and steps, run back to back with the screen after them (and where a step "
            "failed): tap/double_tap/long_press {x,y}, tap_text {text, index?, region?} (taps text read on screen); "
            "tap and tap_text take open:true for list rows that select on the first tap and open on the next (Files): it "
            "taps again only if the screen did not change, so a button is never pressed twice. double_tap also zooms "
            "photos, maps and pages in at that point and back out (there is no pinch). swipe (quick by default: a slow drag "
            "is taken as a tap) "
            "{x,y,to_x,to_y} for pages and sliders, scroll {dy, x?, y?} for lists (negative dy scrolls down), type "
            "{text, submit?} into the focused field, key {key: return|escape|delete|tab|space|arrows or one character, "
            "modifiers?: [cmd, shift, option, ctrl]} (apps' keyboard shortcuts, e.g. ⌘T new tab in Safari; ⌘W/⌘Q/⌘H "
            "stay with iPhone Mirroring and are refused), back (taps the app's own '<' at the top left; stops if there "
            "is none), close_app {app} (throws its card off the app switcher; only a card labelled with that name), home, "
            "app_switcher, spotlight, open_app {app}, wait {ms}, wait_for_text {text, timeout_ms?, region?}, expect_text "
            "{text, region?, absent?} (stops the job unless the screen shows it now). Put checks inside the job instead "
            "of separate calls, e.g. a message in one call: [{type:'open_app',app:'WhatsApp'},{type:'tap_text',"
            "text:'Search'},{type:'type',text:'Ayşe'},{type:'tap_text',text:'Ayşe',region:{top:120}},{type:"
            "'expect_text',text:'Ayşe',region:'header'},<tap the message box>,{type:'type',text:'Merhaba'},{type:"
            "'expect_text',text:'Merhaba',region:'footer'},<tap send>]. The single actions (tap, type, open_app, ...) are one-step act. connect resumes after the owner "
            "used the phone (it must be locked again). A closed iPhone Mirroring is opened (without taking focus) and "
            "waited for; when it asks for the Mac's password, only the owner can enter it. image=false skips the "
            "picture, ocr=true adds the screen's text. "
            "Input brings iPhone Mirroring forward for the moment it takes and puts the owner's app and pointer back; "
            "the agent cursor shows the owner where the iPhone is touched. "
            "Access is the owner's: off, read_only (default) or full, never more than this Mac's permission profile; "
            "action=status shows it and whether iPhone Mirroring is open and connected."
        ),
    )
    def _iphone(action: str = "see", steps: Optional[List[Dict[str, Any]]] = None, x: Optional[float] = None,
                y: Optional[float] = None, to_x: Optional[float] = None, to_y: Optional[float] = None,
                text: Optional[str] = None, key: Optional[str] = None, app: Optional[str] = None,
                dx: Optional[float] = None, dy: Optional[float] = None, duration_ms: Optional[int] = None,
                ocr: bool = False, image: bool = True, previous_hash: Optional[str] = None,
                region: Optional[Any] = None, texts: Optional[List[str]] = None) -> Any:
        return _log(audit_logger, "iphone", lambda: iphone.run(
            action, steps=steps, x=x, y=y, to_x=to_x, to_y=to_y, text=text, key=key, app=app, dx=dx, dy=dy,
            duration_ms=duration_ms, ocr=ocr, image=image, previous_hash=previous_hash, region=region, texts=texts))

    @mcp.tool(
        name="list_agents",
        description="List delegated agents with compact status/result previews. Delegated callers see only themselves and descendants in their persisted control-plane lineage.",
    )
    def _list_agents(status_filter: Optional[str] = None, limit: int = 20,
                     team_id: Optional[str] = None) -> Dict[str, Any]:
        return _log(audit_logger, "list_agents",
                    lambda: list_agents(settings, status_filter=status_filter, limit=limit, team_id=team_id))

    @mcp.tool(
        name="get_agent",
        description="Get one delegated agent status and typed final result envelope. result_mode='full' pages the complete final report via result_full (result_offset/result_limit). The compatibility result field is the structured summary, not raw provider output; bounded envelopes expose truncation.omitted explicitly. Delegated callers may access only themselves or descendants in their persisted lineage; include_logs=true follows the same boundary.",
    )
    def _get_agent(agent_id: str, include_logs: bool = False,
                   tail_lines: int = 40, result_mode: str = "summary",
                   result_offset: int = 0, result_limit: int = 20000) -> Dict[str, Any]:
        return _log(audit_logger, "get_agent",
                    lambda: get_agent(settings, agent_id=agent_id, include_logs=include_logs,
                                      tail_lines=tail_lines, result_mode=result_mode,
                                      result_offset=result_offset, result_limit=result_limit))

    @mcp.tool(
        name="agent_action",
        description=(
            "Control one agent or a whole team. action: cancel, retry, resume, add_task, despawn; individual isolated Git agents also support "
            "apply (local/root-only, base/touched-file/CAS checked copy into the source working tree) and discard (explicitly remove the isolated worktree). "
            "message is available for individual resumable agent sessions. Delegated callers may control only themselves/descendants or "
            "teams they own within their persisted lineage; siblings and unrelated lineages fail closed. retry is refused after a verified "
            "side-effect or uncertain crash boundary; resume preserves the provider session, worktree and durable receipts. Team resume "
            "(a stopped team) keeps finished tasks, continues interrupted ones from their checkpoints, restarts only those with no side "
            "effect yet and lists the rest. Team add_task adds one task to a running or finished team: task={id, prompt, depends_on?, "
            "title?, role?, scope?, resources?, provider?, model?, reasoning?}; it starts once its depends_on complete and gets their "
            "results; pass expected_revision (the team's plan_revision) to get a conflict instead of racing another change. "
            "Team cancel cascades to all children; team despawn refuses unapplied isolated changes."
        ),
    )
    def _agent_action(action: str, agent_id: Optional[str] = None, team_id: Optional[str] = None,
                      message: Optional[str] = None, signal: str = "TERM", task: Optional[Dict[str, Any]] = None,
                      expected_revision: Optional[int] = None) -> Dict[str, Any]:
        return _log(audit_logger, "agent_action",
                    lambda: agent_action(settings, action=action, agent_id=agent_id, team_id=team_id,
                                         message=message, signal=signal, task=task,
                                         expected_revision=expected_revision))

    # ── File tools ──────────────────────────────────────────────────────────
    @mcp.tool(name="write_file",
              description="Write content to a file with a bounded reversible transaction journal. Creates parent directories if needed.")
    def _write_file(path: str, content: str) -> Dict[str, Any]:
        return _log(audit_logger, "write_file",
                    lambda: write_file(settings, path=path, content=content))

    @mcp.tool(name="write_files_batch",
              description="Write multiple files in one call with transaction journaling. atomic=true rolls back the whole batch if any write fails.")
    def _write_files_batch(files: List[Dict[str, str]], atomic: bool = True) -> Dict[str, Any]:
        return _log(audit_logger, "write_files_batch",
                    lambda: write_files_batch(settings, files=files, atomic=atomic))

    @mcp.tool(name="read_file",
              description=(
                  "Read a file (200,000 chars max). offset/length page by lines; when the text is cut or a page is "
                  "requested, next_offset is the line to continue from (null at the end) and total_lines the line count."
              ))
    def _read_file(path: str, offset: int = 0, length: Optional[int] = None) -> Dict[str, Any]:
        return _log(audit_logger, "read_file",
                    lambda: read_file(settings, path=path, offset=offset, length=length))

    @mcp.tool(name="read_multiple_files",
              description=(
                  "Read several text files in one call. Returns files: a list of {path, status, content, truncated} records in "
                  "request order (50,000 chars max each, max_total_chars for all, default 120,000); a missing file yields "
                  "status=error without failing the rest. Files past the total budget come back status=skipped and are listed in "
                  "not_read; a cut file carries continue={tool: read_file, offset}."
              ))
    def _read_multiple_files(paths: List[str], max_total_chars: int = 120_000) -> Dict[str, Any]:
        return _log(audit_logger, "read_multiple_files",
                    lambda: read_multiple_files(settings, paths=paths, max_total_chars=max_total_chars))

    @mcp.tool(name="edit_file",
              description="Find-and-replace in a file with reversible transaction journaling. Fails if occurrence count != expected_replacements.")
    def _edit_file(path: str, old_string: str, new_string: str,
                   expected_replacements: int = 1) -> Dict[str, Any]:
        return _log(audit_logger, "edit_file",
                    lambda: edit_file(settings, path=path, old_string=old_string,
                                      new_string=new_string, expected_replacements=expected_replacements))

    @mcp.tool(name="move_file", description="Move or rename a file/directory with a bounded reversible transaction journal.")
    def _move_file(source: str, destination: str) -> Dict[str, Any]:
        return _log(audit_logger, "move_file",
                    lambda: move_file(settings, source=source, destination=destination))

    @mcp.tool(name="copy_file", description="Copy a file or directory.")
    def _copy_file(source: str, destination: str) -> Dict[str, Any]:
        return _log(audit_logger, "copy_file",
                    lambda: copy_file(settings, source=source, destination=destination))

    @mcp.tool(name="delete_path",
              description="Delete a file or directory. Set recursive=true for directories. Returns a bounded undo transaction when reversible.")
    def _delete_path(path: str, recursive: bool = False) -> Dict[str, Any]:
        return _log(audit_logger, "delete_path",
                    lambda: delete_path(settings, path=path, recursive=recursive))

    @mcp.tool(
        name="storage_review",
        title="Find what takes room in a folder",
        description=(
            "Free up room: action=scan (read-only) lists large files, files not changed for a long time and exact "
            "duplicates in path (default ~/Downloads), each with why; min_size_mb (100), older_than_days (180), "
            "duplicates, limit. Show the owner the list; move_to_trash takes scan_id + the item_ids they chose and "
            "moves only files unchanged since the scan to the Trash (Finder can Put Back). undo with receipt_id "
            "moves them back when Mac MCP may open the Trash, otherwise names what to Put Back in Finder. "
            "Skips links, hidden files, app bundles, iCloud placeholders; refuses macOS folders and ~/Library."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False),
    )
    def _storage_review(
        action: str = "scan",
        path: Optional[str] = None,
        min_size_mb: Optional[float] = None,
        older_than_days: Optional[int] = None,
        duplicates: bool = True,
        limit: int = 50,
        scan_id: Optional[str] = None,
        item_ids: Optional[List[str]] = None,
        receipt_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        def run() -> Dict[str, Any]:
            chosen = str(action or "scan").strip().lower()
            if chosen == "scan":
                return storage_review_tool.scan(path, min_size_mb=min_size_mb, older_than_days=older_than_days,
                                                duplicates=duplicates, limit=limit)
            if chosen == "move_to_trash":
                return storage_review_tool.move_to_trash(scan_id or "", item_ids or [])
            if chosen == "undo":
                return storage_review_tool.undo(receipt_id or "")
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "action is scan, move_to_trash or undo.")

        return _log(audit_logger, "storage_review", run)

    @mcp.tool(
        name="file_transaction_batch",
        description=(
            "Execute up to 50 write/move/delete actions as one reversible all-or-nothing filesystem transaction. "
            "If any action fails, the whole batch is restored byte-for-byte from the prepared journal snapshots."
        ),
    )
    def _file_transaction_batch(actions: List[Dict[str, Any]]) -> Dict[str, Any]:
        return _log(audit_logger, "file_transaction_batch",
                    lambda: file_transaction_batch(settings, actions=actions))

    @mcp.tool(
        name="file_transaction_undo",
        description=(
            "Undo a recent reversible filesystem transaction by transaction_id, including direct file-tool, reversible shell/job, and compound receipts. "
            "By default refuses to overwrite filesystem changes made after the original transaction; force=true overrides that conflict check. "
            "Incomplete capture journals never claim a full undo."
        ),
    )
    def _file_transaction_undo(transaction_id: str, force: bool = False) -> Dict[str, Any]:
        return _log(audit_logger, "file_transaction_undo",
                    lambda: undo_file_transaction(settings, transaction_id=transaction_id, force=force))

    @mcp.tool(name="list_directory",
              description=(
                  "List files and directories in a path, by name (limit default 200, max 1000; total gives the full count). "
                  "page.has_more + page.next_cursor continue the listing."
              ))
    def _list_directory(path: str, limit: int = 200, cursor: Optional[str] = None) -> Dict[str, Any]:
        return _log(audit_logger, "list_directory",
                    lambda: list_directory(settings, path=path, limit=limit, cursor=cursor))

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
              description=(
                  "Find files by name glob pattern (e.g. '*.py', 'report*'). file_type: file|dir|any. "
                  "limit default 200, max 1000; page.has_more + page.next_cursor continue the search."
              ))
    def _find_files(pattern: str, path: str = str(Path.home()),
                    file_type: str = "any", limit: int = 200, cursor: Optional[str] = None) -> Dict[str, Any]:
        return _log(audit_logger, "find_files",
                    lambda: find_files(settings, pattern=pattern, path=path, file_type=file_type,
                                       limit=limit, cursor=cursor))

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

    @mcp.tool(
        name="artifact_pipeline",
        title="Artifact Pipeline",
        description=(
            "Register and verify explicit local file artifacts by stable handle + SHA-256, open a verified artifact in Preview, "
            "or run a verified Preview Save As. action: register|inspect|open_preview|preview_save_as. "
            "Pass both artifact_id and matching path for operations that consume an existing artifact; stale/replaced files fail closed."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False),
    )
    def _artifact_pipeline(
        action: str,
        path: Optional[str] = None,
        artifact_id: Optional[str] = None,
        destination: Optional[str] = None,
        overwrite: bool = False,
        preserve_focus: bool = True,
        timeout_s: float = 15.0,
    ) -> Dict[str, Any]:
        return _log(
            audit_logger, "artifact_pipeline",
            lambda: artifact_pipeline(
                action=action, path=path, artifact_id=artifact_id, destination=destination,
                overwrite=overwrite, preserve_focus=preserve_focus, timeout_s=timeout_s,
            ),
        )

    @mcp.tool(
        name="context_handoff",
        title="Create Cross-App Context Handoff",
        description=(
            "Create or inspect a typed, integrity-sealed, single-use handoff between browser/native/file contexts. "
            "Use action=create_browser_text for selected browser text+URL bound to a specific mac_observe target, "
            "or action=create_artifact for Finder/browser/local artifacts bound to a native file dialog, a verified Mail draft attachment target, or browser file input. "
            "This tool does not perform the write/upload itself; consume handoff_id with mac_act (including verified Mail handoff actions) or browser_upload_artifact so existing policy, verification, focus-safety and egress gates remain enforced."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=False),
    )
    def _context_handoff(
        action: str,
        handoff_id: Optional[str] = None,
        browser: Optional[str] = None,
        tab_handle: Optional[str] = None,
        source_type: Optional[str] = None,
        source_browser: Optional[str] = None,
        source_tab_handle: Optional[str] = None,
        path: Optional[str] = None,
        artifact_id: Optional[str] = None,
        target_kind: Optional[str] = None,
        target_app: Optional[str] = None,
        target_app_handle: Optional[str] = None,
        target_window_handle: Optional[str] = None,
        target_observation_id: Optional[str] = None,
        target_element_id: Optional[str] = None,
        target_browser: Optional[str] = None,
        target_tab_handle: Optional[str] = None,
        target_css_selector: Optional[str] = None,
        include_url: bool = True,
        clear: bool = False,
    ) -> Dict[str, Any]:
        return _log(
            audit_logger, "context_handoff",
            lambda: context_handoff(
                settings, action=action, handoff_id=handoff_id, browser=browser, tab_handle=tab_handle,
                source_type=source_type, source_browser=source_browser, source_tab_handle=source_tab_handle,
                path=path, artifact_id=artifact_id, target_kind=target_kind, target_app=target_app,
                target_app_handle=target_app_handle, target_window_handle=target_window_handle,
                target_observation_id=target_observation_id, target_element_id=target_element_id,
                target_browser=target_browser, target_tab_handle=target_tab_handle,
                target_css_selector=target_css_selector, include_url=include_url, clear=clear,
            ),
        )

    @mcp.tool(
        name="workspace",
        title="Save and reopen a named workspace",
        description=(
            "Named workspaces such as 'Research' or 'Writing'. action=save stores what is open now (apps with a "
            "window, Safari/Chrome tabs, the Finder folder and selection) or the apps, urls and finder_paths given; "
            "include picks the parts (apps, urls, finder), exclude_apps leaves apps out, desktop saves a Mission "
            "Control space id to open onto. list, inspect and delete manage them. restore opens what is missing in "
            "the background without taking focus or moving windows (apps already running stay as they are) and "
            "reports what is not installed or no longer there. Apps and sites excluded in Privacy are never saved. "
            "Kept only on this Mac."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True),
    )
    def _workspace(
        action: str = "list",
        name: Optional[str] = None,
        include: Optional[List[str]] = None,
        apps: Optional[List[str]] = None,
        urls: Optional[List[str]] = None,
        finder_paths: Optional[List[str]] = None,
        exclude_apps: Optional[List[str]] = None,
        desktop: Optional[int] = None,
    ) -> Dict[str, Any]:
        def run() -> Dict[str, Any]:
            chosen = str(action or "list").strip().lower()
            if chosen == "list":
                return workspace_tool.list_workspaces()
            if chosen == "save":
                return workspace_tool.save(settings, name or "", include=include, apps=apps, urls=urls,
                                           finder_paths=finder_paths, exclude_apps=exclude_apps, desktop=desktop)
            if chosen == "inspect":
                return workspace_tool.inspect(name or "")
            if chosen == "restore":
                return workspace_tool.restore(settings, name or "", desktop=desktop)
            if chosen == "delete":
                return workspace_tool.delete(name or "")
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "action is list, save, inspect, restore or delete.")

        return _log(audit_logger, "workspace", run)

    @mcp.tool(
        name="mac_snapshot",
        title="Read Mac Snapshot",
        description=(
            "First rung of the low-context perception pipeline: collect a compact read-only Mac context snapshot in parallel. "
            "By default returns six bounded sections: "
            "visible apps, frontmost native windows, Finder selected paths/context, Safari/Chrome tabs, clipboard metadata "
            "without clipboard contents, and basic system health. Use sections to request a subset and limits to bound output. "
            "Independent section failures are reported as partial results instead of failing the whole snapshot."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def _mac_snapshot(
        sections: Optional[List[str]] = None,
        app_limit: int = 20,
        window_limit: int = 12,
        browser_tab_limit: int = 12,
        selected_file_limit: int = 10,
        max_output_bytes: int = 16384,
    ) -> Dict[str, Any]:
        return _log(
            audit_logger,
            "mac_snapshot",
            lambda: unified_read_snapshot(
                settings,
                sections=sections,
                app_limit=app_limit,
                window_limit=window_limit,
                browser_tab_limit=browser_tab_limit,
                selected_file_limit=selected_file_limit,
                max_output_bytes=max_output_bytes,
            ),
        )

    # ── Unified macOS UI tools ──────────────────────────────────────────────
    @mcp.tool(
        name="mac_observe",
        title="Observe macOS UI",
        description=(
            "Read the frontmost or named macOS application's current UI state. "
            "Returns an observation_id, Accessibility tree nodes with element_id, role, "
            "title, value, position, enabled state and supported actions, plus a targeted "
            "native-window image when include_screenshot=true and one stable window is selected. "
            "Default to semantic-only observation and reuse previous_observation_id before requesting visuals. "
            "window_index=0 retains whole-screen capture. Screenshots are returned as connector-safe "
            "JPEG image content. OCR is last-resort: when ocr=true but Accessibility already contains semantic text, "
            "OCR/capture is skipped automatically. "
            "Returns process-bound app_handle and stable window_handle values when the window "
            "can be uniquely identified. Pass observation_id to mac_act; handles are re-resolved "
            "before each action so window reordering cannot silently retarget an element. "
            "Pass previous_observation_id for conditional observe: unchanged state returns compact "
            "not_modified without a full AX traversal; small changes return delta and structural changes full refresh. "
            "query='New chat | text area' reads the whole window (web and Electron apps too) and returns only the "
            "elements whose label, value or role has those words, with element_ids for mac_act."
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
        app_handle: Optional[str] = None,
        window_handle: Optional[str] = None,
        window_index: int = 1,
        max_depth: int = 5,
        max_children: int = 30,
        include_screenshot: bool = False,
        ocr: bool = False,
        previous_observation_id: Optional[str] = None,
        query: Optional[str] = None,
    ) -> Any:
        return _log(
            audit_logger,
            "mac_observe",
            lambda: observe_ui(
                settings,
                app=app,
                app_handle=app_handle,
                window_handle=window_handle,
                window_index=window_index,
                max_depth=max_depth,
                max_children=max_children,
                include_screenshot=include_screenshot,
                ocr=ocr,
                previous_observation_id=previous_observation_id,
                query=query,
            ),
        )

    @mcp.tool(
        name="mac_act",
        title="Act on macOS UI",
        description=(
            "Mutating native UI actions require a stable process-bound target from mac_observe; implicit frontmost-app targeting is rejected. "
            "Pass observation_id plus app/app_handle/window_handle, and target_bundle_id from mac_observe when available, so target and effect are explicit. "
            "Perform one or more bounded macOS UI actions using element_id values from mac_observe. Post-action state_mode defaults to 'delta', returning only changed UI state; "
            "use 'none' for no post-state or 'full' for a complete Accessibility refresh. Supported action types: "
            "click/double_click, scroll, type, paste, key/shortcut, drag, and "
            "accessibility_action/menu. Use observation_id to prevent stale element paths. "
            "Optional app_handle/window_handle values pin execution to a previously observed native target. "
            "Potentially consequential clicks require allow_risky=true explicitly. "
            "Text input is background-first: type/type_text uses AXValue for exact replacement and AXSelectedText for insertion; paste uses AXSelectedText, without keyboard or clipboard focus stealing when supported. "
            "Foreground-required input (keyboard shortcuts, drag, double-click/global pointer, file dialogs, or input_mode='foreground') fails closed for normal MCP/model calls; only a trusted local-user foreground capability can authorize it. "
            "preserve_focus controls restoration after an already-authorized foreground action and cannot grant foreground access. Background actions remain focus-guarded even when preserve_focus=false. "
            "Pointer x/y are desktop points; set coordinate_space='image' to give pixels of the screenshot returned with observation_id "
            "(see its coordinate_frame). Points outside that image or on no display fail before any input. "
            "Screenshots are omitted after actions unless include_screenshot=true. The legacy return_state boolean remains supported. "
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
        app_handle: Optional[str] = None,
        window_handle: Optional[str] = None,
        target_bundle_id: Optional[str] = None,
        state_mode: Optional[str] = None,
        include_screenshot: bool = False,
        return_state: Optional[bool] = None,
        allow_risky: bool = False,
        preserve_focus: bool = True,
    ) -> Any:
        return _log(
            audit_logger,
            "mac_act",
            lambda: act_ui(
                settings,
                actions=actions,
                observation_id=observation_id,
                app=app,
                app_handle=app_handle,
                window_handle=window_handle,
                target_bundle_id=target_bundle_id,
                state_mode=state_mode,
                include_screenshot=include_screenshot,
                return_state=return_state,
                allow_risky=allow_risky,
                preserve_focus=preserve_focus,
            ),
        )

    @mcp.tool(
        name="mac_app",
        title="Use semantic first-party macOS app adapter",
        description=(
            "Use typed semantic adapters for Finder, Notes, Mail, Calendar, Reminders, Contacts, Shortcuts, Preview, System Settings, Messages, Xcode and Mission Control. "
            "Common action=capabilities reports app-specific actions; app='all' lists every app's support with the "
            "reliable path per task (Slack and VS Code use generic AX). Messages: find_chats (query matches chat name, "
            "person or handle; with Messages closed it lists chats from its database, newest first, by number or "
            "address instead of contact name)|read_messages (item_id = a chat_id from find_chats, or query = a chat name, number or "
            "person; newest first, cursor = next_cursor for older ones, optional date_from/date_to)|search_messages "
            "(query = words in the text, optional item_id to stay in one chat, date_from/date_to, cursor); both read "
            "only, every message carries chat_id, sender and date, and need Full Disk Access for Mac MCP Messages "
            "Reader (MESSAGES_FULL_DISK_ACCESS_REQUIRED: the owner turns it on, do not work around it)|send_message "
            "(body, plus item_id = a chat_id from find_chats or a query matching exactly one chat). Xcode: list_workspaces (projects, schemes, targets). "
            "These read actions never launch the app; a closed app returns APP_NOT_RUNNING. "
            "Finder: selection|select_file. "
            "Notes: find_notes (query; search_in = title (default), body or both: body matches return a short masked snippet; matching ignores case and accents, Turkish letters included)|open_note|create_note (title, optional body/folder/account)|append_to_note (item_id or "
            "query matching one note, body added at its end)|update_note (item_id only; replaces the title and/or body). "
            "Mail: find_messages|open_message|create_draft (title as subject, optional body, to/cc as comma-separated "
            "addresses or contact names that lead to one address, account name or address; required when several "
            "accounts are enabled; optional attachments as file paths). create_draft only saves to Drafts and never "
            "sends; send_mail (same fields except attachments, to required) sends; update_message (item_id, mailbox "
            "where it is now; read and/or flagged true/false, move_to a mailbox name or 'archive'). "
            "send_mail and send_message reach other people: by default the person confirms each one in a native panel "
            "showing recipients and text (Settings > Apps can turn this off). SEND_CANCELLED means they declined; do not "
            "retry unless asked. Sends are never retried automatically. "
            "Calendar: find_events|open_event|create_event (title, start, optional end/calendar/location/notes; "
            "start as YYYY-MM-DD makes an all-day event)|update_event (item_id uid plus fields to change)|"
            "find_free_time (date_from/date_to, default the next 7 days; duration_min default 30; hours default "
            "'09:00-18:00'; include_weekends; optional calendar; all-day events do not count as busy; repeating events "
            "it cannot expand make the result approximate=true; buffer_min keeps minutes free around events)|"
            "plan_day (date_from = the day, default today: events, free time and reminders due, read-only)|"
            "schedule_reminders (item_id = reminder ids from list_reminders, comma-separated, id=45 for its own "
            "minutes; duration_min default 30; date_from/date_to, hours, include_weekends, buffer_min as find_free_time; "
            "optional calendar for the blocks: each reminder, in the order given, gets the first free time that fits "
            "and does not overlap the blocks before it, its id in the block's notes; reminders stay as they are; a reminder that already has a block "
            "is listed as already_scheduled, ones with no time as unscheduled, each block has its own undo). "
            "Reminders: list_reminders (query, list_name, include_completed)|create_reminder (title, optional "
            "list_name, start as the due date or datetime, notes)|update_reminder (item_id; title, start = new due "
            "date to defer it, notes)|complete_reminder (item_id)|delete_reminder (item_id). "
            "Contacts: find_contacts (query matches name or organization, or an email address or phone number; "
            "returns emails and phones)|create_contact (title = the person's name, optional email, phone, "
            "organization, notes; the last word is the family name)|update_contact (item_id from find_contacts, or a "
            "query matching one person; title, email, phone, organization and/or notes: email or phone replaces the "
            "person's only one or adds the first, and with several it changes nothing and says so). "
            "Photos (read through Mac MCP Photos; the owner allows it once in Privacy & Security › Photos, "
            "PHOTOS_ACCESS_REQUIRED until then: do not work around it; the library is never changed): list_albums|"
            "find_photos (date_from/date_to, optional album = id or name from list_albums, media_type photo or video, "
            "favorite true/false, query = part of the file name; newest first; items say has_location, never where)|"
            "export_photos (item_id = ids from find_photos, comma-separated, at most 50; optional path = folder, "
            "default ~/Downloads/Mac MCP Photos/<time>; originals copied, each file read back with its sha256). "
            "Shortcuts: list_shortcuts (optional query)|run_shortcut (item_id from list_shortcuts or title = exact name, "
            "optional body as its text input; returns its text output). A shortcut can do anything its actions do, so "
            "run one only when asked; it needs running commands to be allowed (or the shortcut on the owner's list in "
            "Settings > Apps), and a failed run says outcome_unknown. "
            "Data actions return the item's stable id and a read-back verification; a replayed create returns the "
            "existing item instead of a duplicate, and an uncertain result says outcome_unknown and must not be retried blindly. "
            "Undo (Notes, Reminders, Calendar, Mail, Contacts): a change returns undo={undo_id} when it can be undone; "
            "action=undo, item_id=undo_id (same app) puts the old state back, removes an item Mac MCP made or makes a "
            "deleted reminder again (new id). UNDO_CONFLICT means the item changed since: nothing was overwritten "
            "(force=true only when the owner asks). Sends and shortcut runs say undoable=false. "
            "Preview: list_documents|open_document. System Settings: list_panes|open_pane. "
            "Mission Control (desktops): only when the owner asks you to work on a separate desktop or in your own "
            "workspace; otherwise work alongside them as usual. list_desktops (each display's desktops with the "
            "apps on each, which one is on screen, which are empty, which display is active; pick an empty desktop "
            "that is not on screen, on the active display; its note says when Safari can open there, else use "
            "Google Chrome)|"
            "create_desktop (item_id = display id)|remove_desktop (item_id = desktop id; only an empty desktop not "
            "on screen)|open_on_desktop (item_id = desktop id, target_app = app name or bundle id, optional path or "
            "url: the app's new window opens straight on that desktop without showing on the owner's screens or "
            "taking focus, and the owner's own windows of the app stay where they are; "
            "APP_IN_FRONT_WOULD_SWITCH_DESKTOP means the owner is using that app and desktop switching is on, "
            "TARGET_ON_OTHER_DISPLAY that the desktop is not on the owner's active display: retry later, pick a "
            "desktop on the active display, or use Chrome; Chrome needs its Mac MCP companion)|"
            "move_to_desktop (item_id, window_ids). "
            "Unsupported apps/actions return an explicit mac_observe/mac_act fallback; no generic AX action runs automatically. "
            "Mutating/open adapter actions preserve the user's current focus by default. preserve_focus=false is foreground intent only and fails closed for normal MCP/model calls; only a trusted local-user foreground capability can authorize it. "
            "Use item_id returned by find/list actions for deterministic open actions when available."
        ),
        structured_output=False,
    )
    def _mac_app(
        app: str,
        action: str = "capabilities",
        query: Optional[str] = None,
        item_id: Optional[str] = None,
        path: Optional[str] = None,
        mailbox: Optional[str] = None,
        sender: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        limit: int = 10,
        exact: bool = False,
        preserve_focus: bool = True,
        timeout_s: float = 10.0,
        title: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        calendar: Optional[str] = None,
        location: Optional[str] = None,
        notes: Optional[str] = None,
        list_name: Optional[str] = None,
        include_completed: bool = False,
        body: Optional[str] = None,
        folder: Optional[str] = None,
        account: Optional[str] = None,
        to: Optional[str] = None,
        cc: Optional[str] = None,
        duration_min: Optional[int] = None,
        hours: Optional[str] = None,
        include_weekends: bool = False,
        buffer_min: Optional[int] = None,
        read: Optional[bool] = None,
        flagged: Optional[bool] = None,
        move_to: Optional[str] = None,
        attachments: Optional[List[str]] = None,
        target_app: Optional[str] = None,
        url: Optional[str] = None,
        window_ids: Optional[List[int]] = None,
        cursor: Optional[str] = None,
        force: bool = False,
        search_in: Optional[str] = None,
        email: Optional[str] = None,
        phone: Optional[str] = None,
        organization: Optional[str] = None,
        album: Optional[str] = None,
        media_type: Optional[str] = None,
        favorite: Optional[bool] = None,
    ) -> Dict[str, Any]:
        return _log(
            audit_logger,
            "mac_app",
            lambda: mac_app(
                settings,
                app=app,
                action=action,
                query=query,
                item_id=item_id,
                path=path,
                mailbox=mailbox,
                sender=sender,
                date_from=date_from,
                date_to=date_to,
                limit=limit,
                exact=exact,
                preserve_focus=preserve_focus,
                timeout_s=timeout_s,
                title=title,
                start=start,
                end=end,
                calendar=calendar,
                location=location,
                notes=notes,
                list_name=list_name,
                include_completed=include_completed,
                body=body,
                folder=folder,
                account=account,
                to=to,
                cc=cc,
                duration_min=duration_min,
                hours=hours,
                include_weekends=include_weekends,
                buffer_min=buffer_min,
                read=read,
                flagged=flagged,
                move_to=move_to,
                attachments=attachments,
                target_app=target_app,
                url=url,
                window_ids=window_ids,
                cursor=cursor,
                force=force,
                search_in=search_in,
                email=email,
                phone=phone,
                organization=organization,
                album=album,
                media_type=media_type,
                favorite=favorite,
            ),
        )

    def _computer_plan_resources(steps: List[Dict[str, Any]], resources: Optional[List[Dict[str, Any]]]) -> List[Dict[str, str]]:
        # Model-supplied resources may add constraints, but cannot remove static
        # safety claims derived from the actual plan steps (for example clipboard).
        raw = [*derive_computer_plan_resources(steps), *(resources or [])]
        try:
            normalized = normalize_admission_claims(raw)
        except AdmissionError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, {"error": exc.code, "message": str(exc)}) from exc
        scope = current_policy_context().scope
        if scope is None:
            return normalized
        for claim in normalized:
            kind = claim.get("kind")
            identifier = claim.get("id")
            decision = None
            if kind in {"workspace", "path", "file"}:
                decision = evaluate_scope(scope, ScopeRequest(path=identifier))
            elif kind == "browser_tab":
                decision = evaluate_scope(scope, ScopeRequest(browser_tab=identifier))
            if decision is not None and not decision.allowed:
                raise HTTPException(status.HTTP_403_FORBIDDEN, {
                    "error": "computer_plan_resource_scope_denied",
                    "kind": kind, "id": identifier, "reasons": list(decision.reasons),
                })
        return normalized

    @mcp.tool(
        name="computer_plan",
        title="Run bounded computer-use plan",
        description=(
            "Execute a bounded closed-loop macOS/browser plan in one model tool call. plan_version=2 supports "
            "wait_until, conditional branch, bounded retry/fallback, fresh observe + semantic target rebind and "
            "resource preflight. Mutating recovery is fail-closed: ACTION_NO_EFFECT, policy deny, outcome_unknown "
            "or any ambiguous side-effect is never automatically replayed. Every nested step still passes through "
            "Mac MCP policy, scope, telemetry, leases and action verification. Use {'$ref':'step_id.path'} to reuse outputs. "
            "A step is {'id': 'open_note', 'tool': 'mac_app', 'arguments': {'app': 'Notes', 'action': 'create_note', "
            "'title': 'Plan'}}; kind 'wait_until' (read-only tool) and 'branch' add control flow. "
            "Every run gets a run_id (dashboard timeline). A step may set evidence: before|after|both to keep a checkpoint "
            "screenshot of its tab or window when the owner turned run evidence on; never kept while a password field shows."
        ),
        structured_output=False,
    )
    async def _computer_plan(
        steps: List[Dict[str, Any]],
        max_seconds: float = 45.0,
        plan_version: int = 2,
        max_recoveries: int = 4,
        max_recovery_seconds: float = 12.0,
        max_action_units: int = 24,
        resources: Optional[List[Dict[str, Any]]] = None,
        save_as_recipe: Optional[str] = None,
    ) -> Dict[str, Any]:
        run_id = current_run_id()
        run_started = time.time()
        try:
            normalized_resources = _computer_plan_resources(steps, resources)
            result = await execute_computer_plan(
                mcp.call_tool,
                steps=steps,
                max_seconds=max_seconds,
                plan_version=plan_version,
                max_recoveries=max_recoveries,
                max_recovery_seconds=max_recovery_seconds,
                max_action_units=max_action_units,
                resources=normalized_resources,
                admission_root=AGENTS_DIR,
                evidence_capture=run_evidence.capture_for_run(run_id, mcp.call_tool),
            )
        except ComputerPlanError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        if run_evidence.record_run(run_id, kind="plan", name=save_as_recipe, started_at=run_started, result=result):
            result["run_id"] = run_id
        if save_as_recipe and str(save_as_recipe).strip():
            # Opt-in capture: only a successful plan is saved, and only as a draft
            # that must be reviewed and activated before it can run.
            if not result.get("ok"):
                result["recipe_draft"] = {"saved": False, "reason": "The plan did not finish successfully."}
            elif not _caller_may_write():
                # A caller that may only read (a read-only profile, another Mac with read-only access) writes
                # nothing here, a draft included (#79).
                result["recipe_draft"] = {"saved": False, "reason_code": "read_only_caller",
                                          "reason": "This caller may only read here, so no recipe draft is saved."}
            else:
                try:
                    result["recipe_draft"] = recipes.capture_draft(
                        str(save_as_recipe), steps=steps, plan_version=plan_version,
                        budgets={"max_seconds": max_seconds, "max_recoveries": max_recoveries,
                                 "max_recovery_seconds": max_recovery_seconds, "max_action_units": max_action_units},
                        plan_result=result,
                    )
                except recipes.RecipeError as exc:
                    result["recipe_draft"] = {"saved": False, "reason_code": exc.code, "reason": str(exc)}
        return result

    @mcp.tool(
        name="recipe",
        title="Saved computer_plan recipes",
        description=(
            "Reuse a successful computer_plan as a reviewed, parameterized recipe. Capture with "
            "computer_plan(save_as_recipe='name'), which saves a draft. action=list|inspect|update|activate|run|pause|"
            "resume|delete. update: name, summary (what the recipe does), parameters {name: {type: string|integer|number|boolean|date|"
            "datetime, required, default, enum, max_length}} and parameterize [{literal, param}] to turn literal "
            "values into {{param}} placeholders; edits return the recipe to draft. activate needs confirm=true and "
            "refuses secret-like literals. run takes values {param: value}; it always executes through computer_plan "
            "with normal policy and verification. delete needs confirm=true."
        ),
        structured_output=False,
    )
    async def _recipe(
        action: str = "list",
        recipe_id: Optional[str] = None,
        name: Optional[str] = None,
        summary: Optional[str] = None,
        parameters: Optional[Dict[str, Any]] = None,
        parameterize: Optional[List[Dict[str, Any]]] = None,
        values: Optional[Dict[str, Any]] = None,
        confirm: bool = False,
        include_drafts: bool = True,
    ) -> Dict[str, Any]:
        verb = str(action or "list").strip().lower()
        try:
            if verb == "list":
                return recipes.list_recipes(include_drafts=include_drafts)
            if not recipe_id:
                raise recipes.RecipeError("RECIPE_ARGUMENT_INVALID", f"action={verb} requires recipe_id.")
            if verb == "inspect":
                return recipes.inspect_recipe(recipe_id)
            if verb == "update":
                return recipes.update_recipe(recipe_id, name=name, description=summary,
                                             parameters=parameters, parameterize=parameterize)
            if verb in {"activate", "resume"}:
                return recipes.set_status(recipe_id, "active", confirm=confirm)
            if verb == "pause":
                return recipes.set_status(recipe_id, "paused")
            if verb == "delete":
                return recipes.delete_recipe(recipe_id, confirm=confirm)
            if verb == "run":
                prepared = recipes.prepare_run(recipe_id, values)
                budgets = prepared["budgets"]
                run_id = current_run_id()
                run_started = time.time()
                try:
                    result = await execute_computer_plan(
                        mcp.call_tool,
                        steps=prepared["steps"],
                        plan_version=prepared["plan_version"],
                        resources=_computer_plan_resources(prepared["steps"], None),
                        admission_root=AGENTS_DIR,
                        evidence_capture=run_evidence.capture_for_run(run_id, mcp.call_tool),
                        **{key: budgets[key] for key in ("max_seconds", "max_recoveries",
                                                          "max_recovery_seconds", "max_action_units") if key in budgets},
                    )
                except ComputerPlanError as exc:
                    raise recipes.RecipeError("RECIPE_PLAN_INVALID", str(exc)) from exc
                if run_evidence.record_run(run_id, kind="recipe", name=prepared["recipe"].get("name"),
                                           started_at=run_started, result=result, recipe_id=recipe_id):
                    result["run_id"] = run_id
                result["recipe"] = {"recipe_id": recipe_id, "name": prepared["recipe"].get("name"),
                                    "values": prepared["values"], "last_run": recipes.record_run(recipe_id, result)}
                return result
            raise recipes.RecipeError("RECIPE_ARGUMENT_INVALID",
                                      "action must be list, inspect, update, activate, run, pause, resume or delete.")
        except recipes.RecipeError as exc:
            return {"ok": False, "error": exc.code.lower(), "reason_code": exc.code, "message": str(exc), **exc.extra}

    def _calling_app() -> str:
        """Who is asking, in words for the owner's Review sheet."""
        return mcp.client_label(current_policy_context()) or "An AI app"

    @mcp.tool(
        name="scheduled_tasks",
        title="Scheduled Tasks",
        description=(
            "The owner's Scheduled Tasks: AI tasks (a prompt a delegated agent runs), scripts and saved recipes that run "
            "at a set time, on a recurrence or when a file arrives. action=list|get|get_run|create|update|delete|run_now. "
            "list returns limit tasks (default 50, at most 200) from cursor, with total, counts by state and "
            "next_cursor while has_more; brief=true gives one line of facts a task instead of its whole definition. "
            "get gives a task and its last 10 runs in short; get_run with run_id gives one run's full summary, "
            "output and details. "
            "create/update take name, summary (what the task is for), kind ('ai', 'script' or 'recipe'), schedule; for "
            "recipe: recipe_id (a saved recipe, see the recipe tool) and values {parameter: value} (a value may hold {file} "
            "or a date word); it replays the recipe's steps with no model call and fails clearly while the recipe is a draft "
            "or paused. For ai: prompt, agent (codex|claude|opencode|kimi|grok|antigravity|pi|cline|kilo|goose|copilot), "
            "model, folder, and optional data_step {script or command, arguments} that runs first and hands its output to "
            "the agent; for script: script (a file path) or command, arguments, interpreter, folder. Arguments and "
            "commands may use {today}, {yesterday}, {last_week_start}, {last_week_end}, {last_month_start}, "
            "{last_month_end} or {days_ago:N}. Optional expected_files [{path, min_bytes (default 1), "
            "written_this_run (default true)}] (at most 5; a relative path is inside the folder): after a run that "
            "finished, each file is checked by size and time (never opened) and the run is marked verified or "
            "check failed. An item may instead be {url, contains?, status? (default 200)} for a page (GET, no headers) "
            "or {tool: '<server>__<tool>', arguments, contains?} for one of the owner's read-only MCP tools (a tool that "
            "may change anything is never called); one that cannot look leaves the run unverified, never verified. "
            "[] removes the checks. Optional missed='run_once' (timed tasks only): a run missed while the Mac "
            "slept or Mac MCP was off runs once when it is back (default 'skip'). Optional busy='run_after' (timed "
            "tasks only): a run due while the previous one is still going runs once after it, however many came due "
            "meanwhile (default 'skip'). "
            "Optional usage_limit {max_tokens, max_cost_usd} for an ai task stops "
            "its agent at that usage; only agents that report usage while they work can keep one (tokens: claude, "
            "opencode, kilo, pi; cost: opencode, kilo), others are refused; {} removes it. schedule is "
            "{type:'once',at:'2026-10-20T09:00'} | {type:'daily',time:'09:00'} | {type:'weekly',time:'09:00',"
            "weekdays:[0..6] (0=Monday)} | {type:'monthly',time:'09:00',day:1-31} | {type:'hours',every:N,"
            "start:'08:00'} | {type:'cron',expr:'0 9 * * 1'} in the Mac's local time, or {type:'folder',folder:"
            "'~/Downloads',match:'pdf, Invoice*'} to run once for each new file there ({file} in prompt, "
            "arguments or command is that file; existing files don't run it); batch:true there runs once for the "
            "files that arrive together (the prompt lists them; a script's {file} and $MAC_MCP_TRIGGER_FILES are a "
            "JSON list of their paths). Tasks you create or change "
            "wait as 'Needs your approval' and never run until the owner approves them in Settings › Scheduled "
            "Tasks; tell the owner to review them there. You cannot approve. run_now runs an approved task once. "
            "Runs missed while the Mac slept are recorded as skipped, not run late."
        ),
        structured_output=False,
    )
    async def _scheduled_tasks(
        action: str = "list",
        task_id: Optional[str] = None,
        name: Optional[str] = None,
        summary: Optional[str] = None,
        kind: Optional[str] = None,
        schedule: Optional[Dict[str, Any]] = None,
        prompt: Optional[str] = None,
        agent: Optional[str] = None,
        model: Optional[str] = None,
        folder: Optional[str] = None,
        script: Optional[str] = None,
        command: Optional[str] = None,
        arguments: Optional[str] = None,
        interpreter: Optional[str] = None,
        notify: Optional[bool] = None,
        time_limit_minutes: Optional[int] = None,
        data_step: Optional[Dict[str, Any]] = None,
        expected_files: Optional[List[Dict[str, Any]]] = None,
        usage_limit: Optional[Dict[str, Any]] = None,
        missed: Optional[str] = None,
        recipe_id: Optional[str] = None,
        values: Optional[Dict[str, Any]] = None,
        run_id: Optional[str] = None,
        limit: int = 50,
        cursor: Optional[str] = None,
        brief: bool = False,
    ) -> Dict[str, Any]:
        verb = str(action or "list").strip().lower()
        fields = {"name": name, "description": summary, "kind": kind, "schedule": schedule, "prompt": prompt,
                  "provider": agent, "model": model, "cwd": folder, "script": script, "command": command,
                  "args": arguments, "interpreter": interpreter, "notify": notify, "timeout_min": time_limit_minutes,
                  "data_step": None if data_step is None else {
                      "script": data_step.get("script"), "command": data_step.get("command"),
                      "args": data_step.get("arguments", data_step.get("args")),
                      "interpreter": data_step.get("interpreter")},
                  "verify": expected_files, "usage_limit": usage_limit, "missed": missed,
                  "recipe_id": recipe_id, "values": values}
        given = {key: value for key, value in fields.items() if value is not None}
        try:
            if verb == "list":
                tasks = await asyncio.to_thread(scheduled_tasks.list_tasks)
                return scheduled_tasks.page_of(tasks, limit=limit, cursor=cursor, brief=brief)
            if verb == "get_run":
                if not run_id:
                    raise scheduled_tasks.TaskError("run_id_required", "action=get_run needs run_id (see action=get).")
                return {"ok": True, "run": await asyncio.to_thread(scheduled_tasks.get_run, run_id)}
            if verb == "create":
                task = await asyncio.to_thread(scheduled_tasks.create_task, given, by=_calling_app())
                return {"ok": True, "task": task, "status": "needs_approval",
                        "message": "Saved as a suggestion. It will not run until the owner approves it in "
                                   "Mac MCP Settings › Scheduled Tasks (macmcp://settings/scheduled-tasks)."}
            if not task_id:
                raise scheduled_tasks.TaskError("task_id_required", f"action={verb} needs task_id.")
            if verb == "get":
                task = await asyncio.to_thread(scheduled_tasks.get_task, task_id)
                runs = await asyncio.to_thread(scheduled_tasks.list_runs, task_id, 10)
                return {"ok": True, "task": task, "runs": runs}
            if verb == "update":
                task = await asyncio.to_thread(scheduled_tasks.update_task, task_id, given, by=_calling_app())
                return {"ok": True, "task": task, "status": "needs_approval",
                        "message": "Saved as a suggested change. The task keeps running as before until the owner "
                                   "approves the change in Mac MCP Settings › Scheduled Tasks."}
            if verb == "delete":
                return await asyncio.to_thread(scheduled_tasks.delete_task, task_id)
            if verb == "run_now":
                started = await asyncio.to_thread(scheduled_task_runner.scheduler(settings).run_now, task_id)
                return {"ok": True, **started, "message": "Started. Use action=get to see the result when it finishes."}
            raise scheduled_tasks.TaskError("action_invalid",
                                            "action must be list, get, get_run, create, update, delete or run_now.")
        except scheduled_tasks.TaskError as exc:
            return exc.to_dict()

    # ── Search tools ────────────────────────────────────────────────────────
    @mcp.tool(name="search_files",
              description="Search file contents with grep; include_extensions filters by type e.g. ['py','js']. "
                          "About 50,000 characters of matching lines a call; total_matches counts all; if has_more, "
                          "call again with offset=next_offset.")
    def _search_files(pattern: str, path: str = str(Path.home()),
                      include_extensions: Optional[List[str]] = None,
                      case_sensitive: bool = False, offset: int = 0) -> Dict[str, Any]:
        return _log(audit_logger, "search_files",
                    lambda: search_files(settings, pattern=pattern, path=path,
                                         include_extensions=include_extensions,
                                         case_sensitive=case_sensitive, offset=offset))

    @mcp.tool(name="spotlight_search",
              description="Search files by name using macOS Spotlight (mdfind) — very fast. max_results is at most "
                          "500; total counts every match, and when has_more is true call again with offset=next_offset.")
    def _spotlight_search(query: str, max_results: int = 50, offset: int = 0) -> Dict[str, Any]:
        return _log(audit_logger, "spotlight_search",
                    lambda: spotlight_search(settings, query=query, max_results=max_results, offset=offset))

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
                  "a stable tab_handle. Model/agent automation must keep background=true; foreground activation requires an internal explicit local-user UI grant."
              ))
    def _browser_open_url(browser: str, url: str, new_tab: bool = True,
                          background: bool = True, window_index: int = 1,
                          tab_index: Optional[int] = None, tab_handle: Optional[str] = None) -> Dict[str, Any]:
        return _log(audit_logger, "browser_open_url",
                    lambda: browser_open_url(settings, browser=browser, url=url,
                                             new_tab=new_tab, background=background,
                                             window_index=window_index, tab_index=tab_index, tab_handle=tab_handle))

    @mcp.tool(name="browser_list_tabs",
              description=(
                  "List all open tabs with title, URL, indices, and stable tab_handle values that survive tab index shifts. "
                  "For Google Chrome, transport says whether background automation works now and how to fix it."
              ))
    def _browser_list_tabs(browser: str) -> Dict[str, Any]:
        return _log(audit_logger, "browser_list_tabs",
                    lambda: browser_list_tabs(settings, browser=browser))

    @mcp.tool(name="browser_activate_tab",
              description=("User-visible tab selection by stable tab_handle or index. Normal MCP/agent calls are fail-closed even if allow_foreground=true, "
                           "because selecting current/active tab can interrupt the user. Use tab_handle directly with observe/find/act; the local Show Tab UI is the authorized foreground path."))
    def _browser_activate_tab(browser: str, window_index: int = 1, tab_index: int = 1,
                              tab_handle: Optional[str] = None,
                              allow_foreground: bool = False) -> Dict[str, Any]:
        return _log(audit_logger, "browser_activate_tab",
                    lambda: browser_activate_tab(settings, browser=browser, window_index=window_index,
                                                tab_index=tab_index, tab_handle=tab_handle,
                                                allow_foreground=allow_foreground))

    @mcp.tool(name="browser_close_tab",
              description=(
                  "Close one or multiple browser tabs. For safe selection, call browser_list_tabs first and choose tabs by "
                  "their title/tab_handle. Use tab_handles for multiple stable handles; existing tab_handle or "
                  "window_index+tab_index single-tab behavior remains supported."
              ))
    def _browser_close_tab(browser: str, window_index: int = 1, tab_index: int = 1,
                           tab_handle: Optional[str] = None,
                           tab_handles: Optional[List[str]] = None) -> Dict[str, Any]:
        return _log(audit_logger, "browser_close_tab",
                    lambda: browser_close_tab(settings, browser=browser, window_index=window_index,
                                             tab_index=tab_index, tab_handle=tab_handle,
                                             tab_handles=tab_handles))

    @mcp.tool(
        name="browser_observe",
        title="Observe browser tab",
        description=BROWSER_OBSERVE_DESCRIPTION,
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
                         scope: ObserveScope = "interactive", max_elements: int = 40,
                         visual: VisualMode = "none", element_id: Optional[str] = None,
                         previous_observation_id: Optional[str] = None,
                         frame: Optional[str] = None) -> Any:
        return _log(
            audit_logger, "browser_observe",
            lambda: browser_observe(settings, browser=browser, window_index=window_index,
                                    tab_index=tab_index, tab_handle=tab_handle,
                                    scope=scope, max_elements=max_elements,
                                    visual=visual, element_id=element_id,
                                    previous_observation_id=previous_observation_id, frame=frame),
        )

    @mcp.tool(
        name="browser_checkpoint",
        description=(
            "Hand a sign-in, 2FA, passkey, push-approval or captcha step to the person without collecting secrets. "
            "action=create (tab_handle; category optional, detected from the page) returns awaiting_human + checkpoint_id "
            "and notifies the user; action=wait (wait_s<=300) or status reports resolved once that tab no longer shows "
            "the challenge, tab_closed if it is gone; cancel drops it. Never type passwords or codes; after resolved, "
            "browser_observe before acting."
        ),
    )
    async def _browser_checkpoint(browser: str, action: Literal["create", "wait", "status", "cancel"] = "create",
                                  tab_handle: Optional[str] = None, checkpoint_id: Optional[str] = None,
                                  category: Optional[Literal["password", "otp", "passkey", "push", "captcha", "other"]] = None,
                                  message: Optional[str] = None, completed_actions: Optional[int] = None,
                                  wait_s: float = 0, notify: bool = True) -> Dict[str, Any]:
        b = _norm_browser(browser)

        def work() -> Dict[str, Any]:
            if action == "cancel":
                return browser_checkpoint.cancel(checkpoint_id or "")
            # The person signing in may be sent to another site, and Safari then gives the tab a new process:
            # keep the guarded rebind open while the checkpoint lives, so the tab is not taken for closed (#30).
            keep_tab = (lambda handle: browser_tabs.expect_safari_navigation(
                handle, ttl_s=browser_checkpoint.CHECKPOINT_TTL_S)) if b == "Safari" else (lambda handle: None)
            if action == "create":
                keep_tab(tab_handle)
                return browser_checkpoint.create(
                    b, tab_handle or "",
                    probe=lambda: _run_json_js(settings, b, browser_checkpoint.CHALLENGE_JS, tab_handle=tab_handle),
                    notify=(lambda title, text: send_notification(settings, title=title, message=text)) if notify else None,
                    category=category, message=message, completed_actions=completed_actions,
                )
            record_handle = browser_checkpoint.handle_for(checkpoint_id or "")
            keep_tab(record_handle)
            return browser_checkpoint.check(
                checkpoint_id or "",
                probe=lambda: _run_json_js(settings, b, browser_checkpoint.CHALLENGE_JS, tab_handle=record_handle),
                tab_exists=lambda: any(row.get("tab_handle") == record_handle for row in browser_tabs.list_tabs(b)),
                wait_s=wait_s if action == "wait" else 0,
                sleep=cancellable_sleep,
            )

        return await asyncio.to_thread(_log, audit_logger, "browser_checkpoint", work)

    @mcp.tool(
        name="browser_find",
        description=(
            "Find a rendered browser element with exact-first ranking and hard role/text constraints. Queries also match input values; role-only lookup is supported. "
            "Use it to read or inspect a page. To act, do not find first: pass the same query/role/within straight to browser_act, "
            "which resolves the target itself. Set actionable_only=false to include labels/cards. "
            "wait_timeout_s>0 uses the event-driven DOM waiter before the final targeted scan. "
            "within='text unique to one item' (or within_element_id) limits matches to that item, e.g. one comment, "
            "nearest first; within_levels (default 6) sets how far above the anchor the item may extend."
        ),
    )
    async def _browser_find(browser: str, query: str = "", role: Optional[str] = None,
                            text: Optional[str] = None, window_index: int = 1,
                            tab_index: Optional[int] = None, tab_handle: Optional[str] = None,
                            max_results: int = 5,
                            actionable_only: bool = False,
                            wait_timeout_s: float = 0.0,
                            within: Optional[str] = None,
                            within_element_id: Optional[str] = None,
                            within_levels: Optional[int] = None, frame: Optional[str] = None) -> Dict[str, Any]:
        return await asyncio.to_thread(
            _log, audit_logger, "browser_find",
            lambda: browser_find(settings, browser=browser, query=query, role=role, text=text,
                                  window_index=window_index, tab_index=tab_index, tab_handle=tab_handle,
                                  max_results=max_results,
                                  actionable_only=actionable_only,
                                  wait_timeout_s=wait_timeout_s,
                                  within=within, within_element_id=within_element_id,
                                  within_levels=within_levels, frame=frame),
        )

    @mcp.tool(
        name="browser_act",
        description=BROWSER_ACT_DESCRIPTION,
    )
    async def _browser_act(browser: str, actions: BrowserActions,
                           observation_id: Optional[str] = None, window_index: int = 1,
                           tab_index: Optional[int] = None, tab_handle: Optional[str] = None,
                           return_state: ActReturnState = "compact", allow_foreground: bool = False,
                           frame: Optional[str] = None) -> Dict[str, Any]:
        return await asyncio.to_thread(
            _log, audit_logger, "browser_act",
            lambda: browser_act(settings, browser=browser, actions=actions,
                                 observation_id=observation_id, window_index=window_index,
                                 tab_index=tab_index, tab_handle=tab_handle,
                                 return_state=return_state, allow_foreground=allow_foreground, frame=frame),
        )

    @mcp.tool(
        name="browser_sign_in",
        description=(
            "Sign in on the tab's login form with a login the owner saved in Mac MCP (Settings > Saved Logins). "
            "The password is typed straight into the page and never returned; the result only says which account "
            "was used and what was filled. The login is chosen from the page's own site, so look-alike sites get nothing. "
            "Pass account when several are saved; submit=true also submits the form. Works on username-first pages too. "
            "A login form inside a child frame (Chrome): pass frame=part of that frame's URL; the login is then "
            "chosen for the frame's own site."
        ),
    )
    async def _browser_sign_in(browser: str, tab_handle: Optional[str] = None, account: Optional[str] = None,
                               submit: bool = False, window_index: int = 1,
                               tab_index: Optional[int] = None, frame: Optional[str] = None) -> Dict[str, Any]:
        return await asyncio.to_thread(
            _log, audit_logger, "browser_sign_in",
            lambda: browser_sign_in(settings, browser=browser, tab_handle=tab_handle, account=account,
                                    submit=submit, window_index=window_index, tab_index=tab_index, frame=frame),
        )

    @mcp.tool(
        name="browser_do",
        description=(
            "One-shot browser workflow in one call: open a URL (or reuse tab_handle) -> wait -> optional actions -> extract -> "
            "optional close_after. To read a whole page use actions=[{type:'read'}]: all of its text in one call (content that "
            "loads on scroll included), under read with total_chars; continue a longer page with offset=next_offset. "
            "For research use extract=['price','cancellation','parking','rating'] for compact semantic reads. "
            "For interactive, multi-step work on a page use browser_observe -> batched browser_act -> verify instead. "
            "Leave return_state='none' normally; debug=true can expose raw/full state. "
            "For selector reads use actions=[{type:'extract',fields:[{name:'reviews',selector:'.review',all:true}]}]. "
            "fields in the result reports counts and exact continuation offsets; partial output never means a full-page read."
        ),
    )
    async def _browser_do(browser: str, url: Optional[str] = None,
                          actions: OptionalBrowserActions = None,
                          new_tab: bool = True, background: bool = True,
                          window_index: int = 1, tab_index: Optional[int] = None,
                          tab_handle: Optional[str] = None, wait_after_open: bool = True,
                          return_state: DoReturnState = "none", allow_foreground: bool = False,
                          close_after: bool = False, debug: bool = False,
                          extract: Optional[List[str]] = None) -> Dict[str, Any]:
        def work() -> Dict[str, Any]:
            handle = tab_handle
            opened = None
            # Reject a malformed action before the tab is opened or navigated.
            requested_actions = normalize_act_actions(list(actions)) if actions else []
            work_actions = list(requested_actions)
            scan_count = sum(1 for action in work_actions if action.get("type") == "scan")
            if scan_count:
                share = max(1_024, _BROWSER_DO_SCAN_BYTES // scan_count)
                work_actions = [
                    {**action, "max_bytes": min(int(action.get("max_bytes") or share), share)}
                    if action.get("type") == "scan" else action
                    for action in work_actions
                ]
            semantic_fields = semantic_extract_fields(extract) if extract is not None else None
            automatic_actions = (1 if semantic_fields else 0) + (1 if url and wait_after_open else 0)
            if len(work_actions) + automatic_actions > 20:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "browser_do transaction may contain at most 20 actions including automatic wait/extract actions.")
            # close_after closes the tab this call opens: without a url no tab is opened, and finding that out after
            # the actions ran reported a failure for work that was done (roadmap #32).
            if close_after and (not new_tab or not url):
                raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                    "close_after needs url and new_tab=true: it closes the tab browser_do opens.")
            if url:
                opened = browser_open_url(
                    settings, browser=browser, url=url, new_tab=new_tab, background=background,
                    window_index=window_index, tab_index=(tab_index if not new_tab else None),
                    tab_handle=tab_handle,
                )
                if not new_tab and tab_handle and opened.get("tab_handle") != tab_handle:
                    raise HTTPException(
                        status.HTTP_409_CONFLICT,
                        {"ok": False, "error": "browser_do_target_changed", "retryable": True, "tab_handle": tab_handle},
                    )
                handle = opened.get("tab_handle") or handle
                if wait_after_open:
                    work_actions.insert(0, {"type": "wait", "for": "network_idle", "timeout_s": 4, "stable_ms": 300, "required": False})
            if semantic_fields:
                work_actions.append({"type": "extract", "fields": semantic_fields, "max_chars": 6_000})
            elif not requested_actions:
                work_actions.append({
                    "type": "extract",
                    "fields": [
                        {"name": "h1", "selector": "h1", "attr": "text"},
                        {"name": "paragraphs", "selector": "p", "attr": "text", "all": True, "max_items": 20},
                    ],
                    "max_chars": 3000,
                })
            try:
                result = browser_act(
                    settings, browser=browser, actions=work_actions, window_index=window_index,
                    tab_index=tab_index, tab_handle=handle, return_state=return_state,
                    allow_foreground=allow_foreground,
                )
            except HTTPException as exc:
                if opened:
                    # The tab was already opened or navigated, so this call did act.
                    clear_not_executed(exc)
                raise
            if opened:
                result["opened"] = {k: opened.get(k) for k in ("url", "window_index", "tab_index", "tab_handle", "background") if k in opened}
            closed = False
            if close_after:
                if not opened or not handle:
                    raise HTTPException(status.HTTP_400_BAD_REQUEST, "close_after is only allowed for a tab newly opened by browser_do.")
                browser_close_tab(settings, browser=browser, tab_handle=handle)
                closed = True
            if debug:
                if closed:
                    result["closed"] = True
                return result
            data: Dict[str, Any] = {}
            final_url = None
            final_title = None
            extraction_meta: Dict[str, Any] = {}
            errors = []
            scans: List[Dict[str, Any]] = []
            read: Optional[Dict[str, Any]] = None
            for item in result.get("actions") or []:
                if item.get("type") == "read" and item.get("ok") is not False:
                    read = {key: item[key] for key in (
                        "text", "offset", "returned_chars", "total_chars", "next_offset", "complete", "headings",
                        "loaded", "coverage") if key in item}
                    final_url = item.get("url") or final_url
                    final_title = item.get("title") or final_title
                if item.get("type") == "scan" and item.get("ok") is not False:
                    scans.append({key: item[key] for key in (
                        "items", "count", "stopped", "complete", "has_more", "next_cursor", "total_seen",
                        "steps", "key_field", "coverage", "duration_ms") if key in item})
                if item.get("type") == "extract" and isinstance(item.get("data"), dict):
                    data.update(item["data"])
                    for key in ("matched_counts", "fields"):
                        extraction_meta.setdefault(key, {}).update(item.get(key) or {})
                    extraction_meta["truncated"] = bool(extraction_meta.get("truncated") or item.get("truncated"))
                    extraction_meta["complete"] = not extraction_meta["truncated"]
                    extraction_meta["coverage"] = "currently_rendered_matches"
                    final_url = item.get("url") or final_url
                    final_title = item.get("title") or final_title
                if item.get("ok") is False or item.get("matched") is False:
                    errors.append({k: item.get(k) for k in ("type", "error", "for", "timed_out") if item.get(k) is not None})
            progress = result.get("progress") if isinstance(result.get("progress"), dict) else {}
            compact: Dict[str, Any] = {
                "ok": bool(result.get("ok")),
                "data": data,
                "url": final_url or ((result.get("state") or {}).get("url") if isinstance(result.get("state"), dict) else None) or progress.get("url"),
                "title": final_title or ((result.get("state") or {}).get("title") if isinstance(result.get("state"), dict) else None) or progress.get("title"),
                "tab_handle": handle,
                "action_count": result.get("action_count"),
                "internal_js_calls": result.get("internal_js_calls"),
                "duration_ms": result.get("duration_ms"),
                "closed": closed,
                "mutation_dispatched": bool(result.get("mutation_dispatched") or opened),
            }
            compact.update(extraction_meta)
            if read is not None:
                compact["read"] = read
            if len(scans) == 1:
                compact["scan"] = scans[0]
            elif scans:
                compact["scans"] = scans
            if progress:
                compact["progress"] = progress
            if errors:
                compact["errors"] = errors
            if return_state != "none" and result.get("state") is not None:
                compact["state"] = result.get("state")
            return _fit_browser_do_output(compact)
        return await asyncio.to_thread(_log, audit_logger, "browser_do", work)

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
              description=(
                  "Wait for a browser download to reach a stable completed file, then register it as a SHA-256 artifact. "
                  "To click and catch the download in one step, add expect={download: true} (or part of the file name) to the "
                  "browser_act click instead: it also catches downloads that finish before any separate wait could start. "
                  "Here, pass started_after_epoch_ms from before the initiating click when you have it. "
                  "If several downloads finished or are running it returns completed=false with error=ambiguous_download and the "
                  "candidates instead of guessing; narrow with filename_contains."
              ))
    def _browser_wait_for_download(filename_contains: Optional[str] = None,
                                    timeout_s: int = 60,
                                    started_after_epoch_ms: Optional[int] = None,
                                    stable_ms: int = 500) -> Dict[str, Any]:
        return _log(audit_logger, "browser_wait_for_download",
                    lambda: browser_wait_for_download(
                        settings, filename_contains=filename_contains, timeout_s=timeout_s,
                        started_after_epoch_ms=started_after_epoch_ms, stable_ms=stable_ms,
                    ))

    @mcp.tool(
        name="browser_upload_artifact",
        description=(
            "Select an explicitly registered artifact into an HTML input[type=file] and verify browser file metadata. "
            "Requires artifact_id plus the matching path. Chrome uses the background debugger bridge DOM.setFileInputFiles. "
            "Safari picks files only in its native Open panel, which needs Safari in front: the owner is asked first "
            "(a dialog naming the file and the site). On Allow, Safari comes forward for that one upload and the owner's "
            "window goes back; on Don't Allow or no answer the result is owner_declined_upload: nothing was selected, "
            "carry on with the rest of the task and do not ask again unless the owner wants to. "
            "This selects the file only; it does not submit the surrounding form."
        ),
    )
    def _browser_upload_artifact(
        browser: str, css_selector: str, artifact_id: str, path: str,
        window_index: int = 1, tab_index: Optional[int] = None, tab_handle: Optional[str] = None,
        timeout_s: int = 20, preserve_focus: bool = True, handoff_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return _log(audit_logger, "browser_upload_artifact",
                    lambda: browser_upload_artifact(
                        settings, browser=browser, css_selector=css_selector, artifact_id=artifact_id, path=path,
                        window_index=window_index, tab_index=tab_index, tab_handle=tab_handle,
                        timeout_s=timeout_s, preserve_focus=preserve_focus, handoff_id=handoff_id,
                    ))

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
                  "Sends a native keyboard key only to an explicitly pinned active browser tab. "
                  "Pass tab_handle from browser_list_tabs/browser_observe; lease_generation may be supplied from the observed target. "
                  "If that tab is no longer active in the front browser window, the call fails closed without sending a key. "
                  "Key examples: 'return', 'escape', 'tab', 'space', 'delete', 'up', 'down', 'left', 'right', "
                  "'f5', 'a', 'A'. modifiers examples: ['cmd'], ['shift'], ['cmd','shift']."
              ))
    def _browser_press_key(browser: str, key: str, modifiers: Optional[List[str]] = None,
                            window_index: int = 1, tab_handle: Optional[str] = None,
                            lease_generation: Optional[int] = None,
                            allow_foreground: bool = False) -> Dict[str, Any]:
        return _log(audit_logger, "browser_press_key",
                    lambda: browser_press_key(settings, browser=browser, key=key,
                                              modifiers=modifiers, window_index=window_index,
                                              tab_handle=tab_handle, lease_generation=lease_generation,
                                              allow_foreground=allow_foreground))

    @mcp.tool(name="browser_coordinate_click",
              description=(
                  "Clicks an absolute X/Y screen coordinate. "
                  "This is a foreground fallback. Normal MCP/agent calls cannot authorize it by setting allow_foreground=true; "
                  "prefer browser_act/browser_find DOM targeting. Foreground capability is reserved for explicit local-user UI actions."
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
            "Save what the owner will expect you to remember next time: a lasting preference ('from now on...'), a correction, "
            "a decision, or a stable fact about their work, people, projects, accounts or setup. Do it on your own in the "
            "background: do not ask first and do not say you saved it. One short self-contained sentence with tags; "
            "memory_search first and memory_update the memory it changes instead of adding a near-duplicate (an identical one "
            "is kept once). Not for one-off task details or guesses. Passwords, keys and tokens are refused: save where a "
            "secret is kept, never the secret. Private mode refuses saving. Appends to today's Europe/Istanbul journal in "
            "~/.mac-mcp/memory and updates the search index."
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
            "Recall what the owner told you before. Call it when a request touches earlier work, a preference, a decision, "
            "or a person, project or account they may have mentioned ('as before', 'the usual', 'continue X', 'my client Y'), "
            "and before asking them something they may already have told you. Skip it for self-contained tasks. Use what you "
            "find naturally; do not say it came from memory. query searches by meaning in any language; leave it out to list "
            "recent memories; date/date_from/date_to narrow the time."
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
        name="knowledge_search",
        description=(
            "Search the owner's knowledge folders: the folders they chose in Mac MCP (contracts, invoices, notes, "
            "PDFs, Word and text files), with local hybrid full-text + vector search in any language. Each result "
            "names the file, the page for PDFs, a snippet, its SHA-256 and when it changed. Answer from the snippets "
            "and cite file name and page; read_file the file for more. path narrows to one folder. Only folders the "
            "owner added are searched; when there are none, tell them to add one in Settings > Privacy."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
    )
    async def _knowledge_search(query: str, path: Optional[str] = None, limit: int = 8) -> Dict[str, Any]:
        return await asyncio.to_thread(
            _log, audit_logger, "knowledge_search",
            lambda: knowledge.knowledge_search(query=query, path=path, limit=limit),
        )

    @mcp.tool(
        name="personal_search",
        title="Search the owner's own sources at once",
        description=(
            "One search across the owner's own sources: Notes (title and text), Mail (subject, inbox), Calendar "
            "events, open Reminders, Contacts, Messages and the document folders chosen in Settings › Privacy, asked "
            "at the same time. sources picks some (notes, mail, calendar, reminders, contacts, messages, documents); "
            "date_from/date_to (YYYY-MM-DD) apply to mail, calendar, messages and documents. Each result names its "
            "source, title, a short snippet, when, its id and how to open it (mac_app or read_file); sources reports "
            "each one, and one that is excluded in Privacy, closed or failing does not stop the others. limit "
            "(20, max 50) and next_cursor page without repeats. Read-only; nothing is kept."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
    )
    async def _personal_search(
        query: str,
        sources: Optional[List[str]] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        limit: int = 20,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(
            _log, audit_logger, "personal_search",
            lambda: personal_search_tool.personal_search(query, sources=sources, date_from=date_from,
                                                         date_to=date_to, limit=limit, cursor=cursor),
        )

    @mcp.tool(
        name="daily_briefing",
        title="Today at a glance",
        description=(
            "Today at a glance in one short answer: the day's events and free time, open reminders due (overdue "
            "too), mail that came into the inbox (subject, sender, time, unread) and conversations with new messages "
            "(who, when, unread count). No mail or message text is included. sources picks some (calendar, "
            "reminders, mail, messages); date (YYYY-MM-DD, default today); limit items per section (10, max 25). "
            "Each section answers on its own: one excluded in Privacy is not read, one that fails is reported. "
            "notify=true also shows the one-line summary as a notification. Read-only; nothing is kept."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
    )
    async def _daily_briefing(
        date: Optional[str] = None,
        sources: Optional[List[str]] = None,
        limit: int = 10,
        notify: bool = False,
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(
            _log, audit_logger, "daily_briefing",
            lambda: daily_briefing_tool.daily_briefing(settings, date=date, sources=sources, limit=limit, notify=notify),
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
            "Change a memory when the owner corrects or changes something you remembered ('actually...', 'not anymore', "
            "'now I prefer...'): find it with memory_search, then update it by memory_id so the old version does not stay "
            "beside the new one. Do it quietly; do not say you updated it. Without memory_id, date/date_from/date_to list "
            "timestamped candidates and change nothing. Secrets are refused."
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

    # ── Role-learning lessons ────────────────────────────────────────────────
    @mcp.tool(
        name="lesson_search",
        description=(
            "Search structured role-learning lessons for coder/reviewer/orchestrator. Only explicitly approved active lessons "
            "are injected into future workers; candidates remain quarantined until lesson_feedback(outcome='approve')."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
    )
    async def _lesson_search(role: Optional[str] = None, query: Optional[str] = None,
                             state: Optional[str] = None, limit: int = 20) -> Dict[str, Any]:
        return await asyncio.to_thread(
            _log, audit_logger, "lesson_search",
            lambda: lesson_search(role=role, query=query, state=state, limit=limit),
        )

    @mcp.tool(
        name="lesson_record",
        description=(
            "Record a structured role-learning candidate from a verified correction or workflow finding. This never auto-activates; "
            "approve it separately with lesson_feedback. Web-tainted provenance is rejected."
        ),
    )
    async def _lesson_record(role: str, trigger_context: str, mistake_pattern: str, preferred_action: str,
                             evidence_refs: Optional[List[str]] = None, confidence: float = 0.5,
                             source: Optional[str] = None) -> Dict[str, Any]:
        provenance = current_provenance_class()
        return await asyncio.to_thread(
            _log, audit_logger, "lesson_record",
            lambda: lesson_record(role=role, trigger_context=trigger_context, mistake_pattern=mistake_pattern,
                                  preferred_action=preferred_action, evidence_refs=evidence_refs, confidence=confidence,
                                  source=source, provenance_class=provenance),
        )

    @mcp.tool(
        name="lesson_feedback",
        description=(
            "Review or update one lesson. outcome: approve, success, failure, disable, enable. Failure lowers confidence and repeated "
            "failure can auto-disable; web-tainted provenance cannot modify trusted lessons."
        ),
    )
    async def _lesson_feedback(lesson_id: str, outcome: str, evidence_ref: Optional[str] = None,
                               note: Optional[str] = None) -> Dict[str, Any]:
        provenance = current_provenance_class()
        return await asyncio.to_thread(
            _log, audit_logger, "lesson_feedback",
            lambda: lesson_feedback(lesson_id=lesson_id, outcome=outcome, evidence_ref=evidence_ref, note=note,
                                    provenance_class=provenance),
        )

    @mcp.tool(
        name="lesson_consolidate",
        description=(
            "Inspect duplicate/conflicting role lessons. apply=false is a dry report; apply=true only disables stale low-confidence "
            "lessons and never silently chooses a winner between contradictory preferred actions."
        ),
    )
    async def _lesson_consolidate(role: Optional[str] = None, apply: bool = False) -> Dict[str, Any]:
        provenance = current_provenance_class()
        return await asyncio.to_thread(
            _log, audit_logger, "lesson_consolidate",
            lambda: lesson_consolidate(role=role, apply=apply, provenance_class=provenance),
        )

    @mcp.tool(
        name="lesson_delete",
        description=(
            "Permanently delete role-learning lessons: one lesson_id, every lesson of a role, or all_lessons=true. "
            "Without confirm=true it only shows what would be deleted. Deleted lessons never reach a worker prompt again."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False),
    )
    async def _lesson_delete(lesson_id: Optional[str] = None, role: Optional[str] = None,
                             all_lessons: bool = False, confirm: bool = False) -> Dict[str, Any]:
        provenance = current_provenance_class()
        return await asyncio.to_thread(
            _log, audit_logger, "lesson_delete",
            lambda: lesson_delete(lesson_id=lesson_id, role=role, all_lessons=all_lessons,
                                  confirm=confirm, provenance_class=provenance),
        )

    @mcp.tool(
        name="lesson_export",
        description=(
            "Export every stored role lesson (candidates, active and disabled) with its evidence and the retention period, "
            "so the user can review what was learned."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
    )
    async def _lesson_export(role: Optional[str] = None, state: Optional[str] = None) -> Dict[str, Any]:
        return await asyncio.to_thread(
            _log, audit_logger, "lesson_export", lambda: lesson_export(role=role, state=state),
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
            "Find a written procedure before a task that has one: Swift/SwiftUI, Expo/React Native, frontend and design, "
            "Remotion videos, Word/Excel/PowerPoint/PDF files, ads, SEO and marketing copy, Facebook reports, debugging, code "
            "review and other recurring workflows. Searches the owner's Agent Skills by meaning in any language and returns "
            "names, descriptions and SKILL.md paths. Load the best match with skill_get and follow it. Skip it for simple "
            "one-step tasks."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
    )
    async def _skill_search(query: str, limit: int = 10) -> Dict[str, Any]:
        return await asyncio.to_thread(_log, audit_logger, "skill_search", lambda: skill_search(query=query, limit=limit))

    @mcp.tool(
        name="skill_get",
        description=(
            "Load one Agent Skill (a name from skill_search, or a SKILL.md path) and follow its instructions for the task. "
            "Returns the full SKILL.md, the skill directory and the paths of its bundled scripts, references and assets; read "
            "those only when the skill says to."
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
            "Ask the owner a quick question in the middle of your work without stopping it. The question pops up "
            "on their Mac (the notch island, or a card at the top right; a macOS dialog when the menu app is not "
            "running), you wait up to timeout_s, then you carry on: with their answer if one came, or with your own "
            "best judgment if they skip or do not answer in time (response=null). Use it whenever a doubt, a choice "
            "or a missing detail comes up mid-task, instead of ending your turn to ask in chat or guessing; above all "
            "when you work in the background (a delegated agent, a scheduled task, a long job) and nobody is "
            "watching the chat. Example: an agent writing on Medium for the owner finds something in its research "
            "that changes the angle, asks which way to go, and continues either way. "
            "suggestions (up to 4 short answers) become one-click replies. The owner may also add files: "
            "attachments lists each with its local path, and pictures come back as images."
        ),
    )
    async def _ask_user(
        question: str,
        sender: str = "AI",
        timeout_s: int = 60,
        suggestions: Optional[List[str]] = None,
        allow_attachments: bool = True,
    ) -> Any:
        result = await asyncio.to_thread(
            _log,
            audit_logger,
            "ask_user",
            lambda: ask_user(settings, question=question, sender=sender, timeout_s=timeout_s,
                             suggestions=suggestions, allow_attachments=allow_attachments),
        )
        return ask_user_mcp_result(result)

    @mcp.tool(
        name="ask_user_voice",
        title="Ask the user by voice",
        description=(
            "Speak a short natural question aloud on the local Mac, listen for the user's spoken answer, "
            "transcribe it, and return the response without opening a text dialog. "
            "Prefer one concise conversational sentence (roughly 15 words or fewer). "
            "The default Turkish neural voice is tr-TR-AhmetNeural; saying 'atla', 'iptal', 'boşver', or 'vazgeç' skips. "
            "Off until the local user enables it; before every recording Mac MCP asks the user to allow sending the "
            "question to online text-to-speech and the recorded answer to Groq for transcription. If it returns "
            "experimental_tool_disabled or consent was declined (skipped with fallback_tool), use ask_user instead. "
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
                on_egress=_record_voice_egress,
            ),
        )

    def _record_voice_egress(event: Dict[str, Any]) -> None:
        # A local ledger of what left the Mac: provider, consent and outcome only.
        consent = str(event.get("consent") or "")
        telemetry.record_security_event(
            session_id=None, event_type="voice_egress", tool="ask_user_voice", tool_class="voice",
            origin=str(event.get("provider") or "groq"),
            decision="allowed" if consent in {"record_once", "always"} else "declined",
            reason_code=f"voice_consent_{consent or 'unknown'}",
            profile=None, actor="local_user", agent_id=None,
            target_summary=f"provider=groq; model={event.get('model')}; tts={event.get('tts')}; outcome={event.get('outcome')}",
        )

    def _voice_title(text: str, limit: int) -> str:
        words = " ".join(text.split())
        if len(words) <= limit:
            return words
        return (words[:limit].rsplit(" ", 1)[0] or words[:limit]) + "…"

    @mcp.tool(
        name="voice_capture",
        title="Capture a spoken note",
        description=(
            "Record one short spoken note on this Mac when the person asks to dictate something: a short sound, "
            "then they speak (recording stops after a short silence), and the words come back as text. "
            "target=transcript (default) only returns the text; notes saves it as a new note (title optional, "
            "otherwise its first words; optional folder); reminders as a new reminder (optional title, list_name). "
            "The saved item is read back and can be undone with mac_app. language: tr, en or auto (default from "
            "Settings > Voice). Uses the voice switch and consent of ask_user_voice: off until the person turns voice "
            "on, and before every recording they allow uploading the audio to Groq for transcription; the recording "
            "is deleted afterwards. Saying 'atla', 'iptal', 'boşver' or 'vazgeç', silence or declining saves nothing. "
            "It never listens on its own: call it only when the person wants to dictate."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
        structured_output=False,
    )
    async def _voice_capture(
        target: str = "transcript",
        title: Optional[str] = None,
        folder: Optional[str] = None,
        list_name: Optional[str] = None,
        language: Optional[str] = None,
        sender: str = "AI",
        timeout_s: Optional[int] = None,
    ) -> Dict[str, Any]:
        def save(where: str, text: str) -> Dict[str, Any]:
            if where == "notes":
                return mac_app(settings, app="Notes", action="create_note",
                               title=title or _voice_title(text, 60), body=text, folder=folder)
            name = title or _voice_title(text, 120)
            return mac_app(settings, app="Reminders", action="create_reminder", title=name,
                           notes=None if name == text else text, list_name=list_name)

        return await asyncio.to_thread(
            _log,
            audit_logger,
            "voice_capture",
            lambda: voice_capture(settings, target=target, sender=sender, timeout_s=timeout_s, language=language,
                                  on_egress=_record_voice_egress, save=save),
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

    @mcp.tool(
        name="tool_discover",
        description=(
            "Find which Mac MCP tool to use, including less-common ones, among those the active permission profile and delegated "
            "scope allow. Query in plain words ('read several files', 'run long command'); results are ranked with a reason, a "
            "complete short description, required fields and parameter bounds. has_more + next_cursor page the rest; "
            "include_schema=true adds the full description and input schema."
        ),
    )
    async def _tool_discover(query: str = "", limit: int = tool_discovery.DEFAULT_LIMIT, include_schema: bool = False,
                             cursor: Optional[str] = None) -> Dict[str, Any]:
        limit = max(1, min(int(limit), tool_discovery.MAX_LIMIT))
        try:
            offset = tool_discovery.decode_cursor(cursor, query)
        except tool_discovery.CursorError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        infos = {
            info.name: info for info in await mcp.list_available_tools(compact=False)
            if info.name not in {"tool_discover", "tool_invoke"}
        }
        ranked = tool_discovery.rank(
            ((name, info.description or "") for name, info in infos.items()), query,
            core=CORE_TOOL_NAMES, demoted=ADVANCED_BROWSER_TOOLS,
        )
        page = ranked[offset: offset + limit]
        matches = []
        for match in page:
            info = infos[match.name]
            params = _tool_input_schema(info)
            properties = params.get("properties") or {}
            full_description = info.description or ""
            short, truncated = tool_discovery.short_description(full_description, CORE_TOOL_SUMMARIES.get(info.name))
            availability = mcp.effective_tool_availability(info.name)
            risk = declared_risk(info.name)
            profile = PROFILES.get(str(availability.get("profile") or ""))
            item = {
                "name": info.name,
                # include_schema asks for the whole contract, so keep the full text.
                "description": full_description if include_schema else short,
                "description_truncated": not include_schema and truncated,
                "required": params.get("required") or [],
                "parameters": {
                    name: tool_discovery.parameter_summary(spec) for name, spec in properties.items()
                },
                "policy": {
                    "profile": availability.get("profile"),
                    "availability": availability.get("reason"),
                    "conditional": bool(availability.get("conditional")),
                    "scope_limited": bool(availability.get("scope_limited")),
                    "risk": {
                        "family": risk.family,
                        "capabilities": sorted(capability.value for capability in risk.capabilities),
                        "destructive": risk.destructive,
                        "sensitive": risk.sensitive,
                        "resolution": "argument_dependent" if availability.get("conditional") else "static",
                    },
                    "approval": (
                        profile.approval.to_dict() if profile is not None
                        else {"source": "none", "automatic_confirmation": False}
                    ),
                },
            }
            if str(query or "").strip():
                item["score"] = round(match.score, 1)
                item["why"] = "; ".join(match.reasons) or "related words"
            preferred = ADVANCED_BROWSER_TOOLS.get(info.name)
            if preferred:
                item["tier"] = "advanced_compat"
                item["prefer"] = preferred
            if include_schema:
                item["input_schema"] = params
            matches.append(item)
        has_more = offset + len(page) < len(ranked)
        return {
            "ok": True, "query": query, "count": len(matches), "total": len(ranked), "has_more": has_more,
            "next_cursor": tool_discovery.encode_cursor(query, offset + len(page)) if has_more else None,
            "tools": matches,
        }

    @mcp.tool(
        name="tool_invoke",
        description="Invoke a less-common registered Mac MCP tool by name after tool_discover, preserving normal policy and telemetry checks.",
    )
    async def _tool_invoke(tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        return await _invoke_registered_tool(mcp, tool_name, arguments)

    register_chatgpt_panel(mcp, telemetry, settings)
    # The owner's own MCP servers (Settings > MCP Servers) become <server>__<tool> tools here.
    external_servers = external_mcp.attach(mcp)
    if os.getenv("MAC_MCP_MANAGED_SERVER") == "1":
        external_servers.start()

    # ── App setup ────────────────────────────────────────────────────────────
    app = mcp.streamable_http_app()
    # ChatGPT currently opens a fresh stateful transport for many tool calls. Keep
    # those protocol transports bounded while logical steering sessions (derived
    # from request metadata when available) can remain visible independently.
    mcp.session_manager.session_idle_timeout = session_idle_timeout_s()
    app.add_middleware(SecurityMiddleware)

    async def health(request: Request) -> Response:
        # Keep unauthenticated health probes deliberately non-sensitive; the
        # public tunnel may expose this endpoint too. Nested public-endpoint
        # checks use probe=basic to avoid recursively invoking the deep gate.
        if request.query_params.get("probe") == "basic":
            return JSONResponse({"ok": True, "server": "mac-mcp"})

        update_context = pending_update_context()
        if update_context is None:
            return JSONResponse({"ok": True, "server": "mac-mcp"})

        report = await get_or_start_post_update_health_gate(settings, update_context)
        if report is None:
            return JSONResponse(
                {"ok": False, "server": "mac-mcp", "update_gate": "running"},
                status_code=503,
            )
        healthy = bool(report.get("ok"))
        return JSONResponse(
            {
                "ok": healthy,
                "server": "mac-mcp",
                "update_gate": "passed" if healthy else "failed",
                "target": str(report.get("target_commit") or "")[:12],
            },
            status_code=200 if healthy else 503,
        )

    app.router.routes.append(Route("/health", health, methods=["GET"]))
    app.router.routes.extend(create_chrome_background_bridge_routes())
    async def run_recipe_for_launcher(recipe_id: str, values: Dict[str, Any], *, actor: str = "local_launcher",
                                      description: str = "Run a saved recipe from a launcher") -> Dict[str, Any]:
        """Run one saved recipe for Shortcuts, Raycast, the CLI or a scheduled task (#257).

        It goes through the normal MCP tool path as the local launcher, so profile,
        scopes and approval rules apply exactly as they would for an agent.
        """
        token = set_policy_context(environment_policy_context(actor=actor))
        try:
            arguments: Dict[str, Any] = {"action": "run", "recipe_id": recipe_id, "values": values}
            if mcp.intent_descriptions_enabled():
                arguments["description"] = description
            raw = await mcp.call_tool("recipe", arguments)
        except ToolError as exc:
            text = str(exc)
            needs_approval = "approval_required" in text or "approval" in text.lower()
            return {"ok": False, "status": "approval_required" if needs_approval else "refused",
                    "reason_code": "APPROVAL_REQUIRED" if needs_approval else "TOOL_REFUSED", "message": text[:500]}
        finally:
            reset_policy_context(token)
        result = _unwrap_tool_result(raw)
        return result if isinstance(result, dict) else {"ok": False, "status": "failed", "message": str(result)[:500]}

    app.router.routes.extend(create_dashboard_routes(
        telemetry, settings, dashboard_token, mcp.steering, mcp.security_context,
        recipe_runner=run_recipe_for_launcher,
    ))

    # Scheduled recipe tasks (#257) run on the scheduler's thread; their recipe runs on this server's loop.
    serve_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan_with_recipe_runner(asgi_app):
        loop = asyncio.get_running_loop()

        def run_scheduled_recipe(recipe_id: str, values: Dict[str, Any], limit_s: float) -> Dict[str, Any]:
            future = asyncio.run_coroutine_threadsafe(run_recipe_for_launcher(
                recipe_id, values, actor="scheduled_task", description="Run a saved recipe on its schedule"), loop)
            try:
                return future.result(timeout=limit_s)
            except concurrent.futures.TimeoutError as exc:
                future.cancel()
                raise TimeoutError from exc

        scheduled_task_runner.set_recipe_runner(run_scheduled_recipe)
        try:
            async with serve_lifespan(asgi_app) as state:
                yield state
        finally:
            scheduled_task_runner.set_recipe_runner(None)

    app.router.lifespan_context = lifespan_with_recipe_runner
    from .dashboard_routes import _dashboard_guard
    app.router.routes.extend(external_mcp_routes.create_routes(lambda request: _dashboard_guard(request, dashboard_token)))
    app.router.routes.extend(agent_message_bridge.routes())
    app.router.routes.extend(create_mobile_routes(telemetry, settings, dashboard_token, mcp.steering))
    app.router.routes.extend(create_fleet_routes(lambda request: _dashboard_guard(request, dashboard_token),
                                                 recipe_runner=run_recipe_for_launcher))

    # REST API — FastAPI sub-app mounted at /api
    from fastapi import FastAPI
    from .rest_routes import configure_rest_security, require_auth, router as rest_router
    from . import rest_v2
    configure_rest_security(mcp.security_context, telemetry, security_approval, auth_failures=auth_failures)
    # Runtime API docs would let anyone on the public tunnel enumerate routes
    # unauthenticated; the integration schema ships as openapi/custom-gpt-actions.json.
    rest_app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @rest_app.middleware("http")
    async def _capture_rest_telemetry(request: Request, call_next):
        return await rest_telemetry_middleware(request, call_next, telemetry)

    rest_app.include_router(rest_router)
    error_contract.install_rest_handlers(rest_app, _tool_can_mutate)
    rest_v2.install_routes(rest_app, mcp, require_auth)
    app.mount("/api", rest_app)

    # Outermost: refuse DNS-rebinding requests (foreign Host) on every route,
    # including WebSockets, before any authentication or tool dispatch.
    app.add_middleware(
        HostGuard,
        enforce_hosts=_loopback_host(_effective_bind_host()),
        no_auth=settings.allow_no_auth,
        public_hosts=_public_endpoint_hosts,
        extra_hosts=extra_hosts_from_env(),
    )
    return app


def _public_endpoint_hosts() -> list[str]:
    """Hosts the configured public endpoint (tunnel or custom) delivers requests with."""
    try:
        public = resolve_public_endpoint()
    except Exception:
        return []
    hosts = []
    if public.endpoint_url:
        hosts.append(urlsplit(public.endpoint_url).netloc)
    if getattr(public, "ngrok_domain", None):
        hosts.append(str(public.ngrok_domain))
    return hosts


app = create_app()

if os.getenv("MAC_MCP_MANAGED_SERVER") == "1":
    # The CLI-started server owns log upkeep; imports in tests never rotate real logs.
    start_log_rotation(base_dir=BASE_DIR)
    # Reusable AppleScript host for the browser bridge (osascript stays the fallback).
    applescript_host.enable()
    # Native Accessibility observer (the AppleScript observer stays the fallback).
    ax_native.enable()
    # Accessibility change watcher that lets unchanged windows skip full walks.
    ax_watch.enable()
    # Finished agents' local files expire per Settings > Subagents (default 30 days).
    start_agent_history_expiry()
    # Scheduled Tasks: sleeps until the next due run; no wakeups without active tasks.
    scheduled_task_runner.start(load_settings())
    # Knowledge folders (#197): keeps the index of the owner's chosen folders current.
    knowledge.start_indexer()
    # Owner-only store directories (agents, teams, jobs, telemetry, memory, lessons).
    secure_content_stores([AGENTS_DIR, AGENTS_DIR.parent / "agent_teams", AGENTS_DIR.parent / "jobs"])
    if privacy_policy.privacy_setting("exclude_from_backups", False) is True:
        # Stores created since the owner chose this are kept out of Time Machine too.
        threading.Thread(target=privacy_inventory.set_backup_exclusion, args=(True,),
                         name="mac-mcp-backup-exclusion", daemon=True).start()
