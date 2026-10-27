"""Hand-written catalog summaries for the default core tool profile.

The core profile caps each description at COMPACT_DESCRIPTION_LIMIT characters.
Clipping a long description mid-sentence used to hide limits and safety rules,
so each long core tool gets a complete summary here that states the task, the
best use and the key constraint. tool_discover(include_schema=true) still
returns the full description.
"""
from __future__ import annotations

from typing import Dict

COMPACT_DESCRIPTION_LIMIT = 220

# Tools the default core profile lists (before permission-profile filtering).
# The post-update health gate requires every one the profile allows.
CORE_TOOL_NAMES = frozenset({
    "open_mac_mcp_panel", "mac_mcp_panel_state", "mac_mcp_panel_setting",
    "run_command", "run_commands_parallel",
    "read_file", "write_file", "edit_file", "file_transaction_undo", "artifact_pipeline", "context_handoff", "search_files", "http_request",
    "mac_snapshot", "mac_observe", "mac_act", "mac_app", "computer_plan",
    "browser_list_tabs", "browser_close_tab", "browser_observe", "browser_find", "browser_act", "browser_do", "browser_upload_artifact",
    "browser_checkpoint",
    "spawn_agents", "wait_agents", "team_messages",
    "memory_search", "memory_add", "memory_update", "knowledge_search", "lesson_search", "lesson_feedback", "ask_user",
    "skill_search", "skill_get",
    "tool_discover", "tool_invoke",
})

# Low-level browser primitives kept registered for compatibility. They stay out
# of the default catalog; tool_discover labels them and names the tool to prefer.
ADVANCED_BROWSER_TOOLS: Dict[str, str] = {
    "browser_click_selector": "browser_act",
    "browser_type_selector": "browser_act",
    "browser_wait_for_selector": "browser_act",
    "browser_scroll": "browser_act",
    "browser_press_key": "browser_act",
    "browser_coordinate_click": "browser_act",
    "browser_get_html": "browser_observe",
    "browser_get_snapshot": "browser_observe",
    "browser_open_url": "browser_do",
}

CORE_TOOL_SUMMARIES: Dict[str, str] = {
    "team_messages": (
        "Message agents working at the same time. When you change something a teammate's task depends on, tell "
        "them at once: action=send, to=<task id> or 'lead'. No progress reports. Replies arrive after your tool calls."
    ),
    "run_command": (
        "Run a short shell command in zsh and wait for it (full access unless scoped); long builds or servers belong in "
        "start_background_job. reversible=true records file changes for file_transaction_undo."
    ),
    "file_transaction_undo": (
        "Undo a recent reversible filesystem transaction by transaction_id (file tools, reversible shell/jobs, compound "
        "receipts). Refuses to overwrite later changes unless force=true; partial captures never claim full undo."
    ),
    "spawn_agents": (
        "Spawn 1-10 background delegated agents as one team; access_mode defaults to read_only. Task DAGs "
        "(depends_on), reviewer gates; budgets gate new starts only, so running agents may overshoot. wait_agents."
    ),
    "wait_agents": (
        "Bounded wait for a team or agent_ids (mode all|any|majority). Returns typed result envelopes; "
        "full_result points to get_agent(result_mode='full') when a report was cut. timed_out is the waiter's own deadline."
    ),
    "artifact_pipeline": (
        "Register and verify local file artifacts by handle + SHA-256, open one in Preview or run Preview Save As "
        "(action register|inspect|open_preview|preview_save_as). Needs artifact_id + matching path; stale files fail closed."
    ),
    "context_handoff": (
        "Create or inspect a sealed single-use handoff of browser text or a file artifact to a verified native/browser target. "
        "It writes nothing itself: consume handoff_id with mac_act or browser_upload_artifact."
    ),
    "mac_snapshot": (
        "First rung of low-context perception: one read-only parallel snapshot of visible apps, windows, Finder selection, "
        "browser tabs, clipboard metadata (no contents) and system health. sections/limits bound output."
    ),
    "mac_observe": (
        "Read a macOS app's UI as an Accessibility tree with element_ids and an observation_id for mac_act. Semantic-only by "
        "default; reuse previous_observation_id. Screenshot/OCR only on request, OCR last."
    ),
    "mac_act": (
        "Bounded native UI actions on element_ids from mac_observe; pass observation_id + app/window handle. Background-first; "
        "foreground-only input and risky clicks fail closed unless explicitly authorized."
    ),
    "mac_app": (
        "Typed adapters: Finder, Notes, Mail, Calendar, Reminders, Contacts, Shortcuts, Photos, Preview, System "
        "Settings, Messages, Xcode, Mission Control; use before UI control. Sends ask first. action=capabilities lists them."
    ),
    "computer_plan": (
        "One bounded macOS/browser plan in a call. Step: {id:'a', tool:'mac_app', arguments:{...}}; "
        "waits, branches, retries, rebind. Uncertain side effects never replayed; normal policy per step."
    ),
    "browser_close_tab": (
        "Close one or more browser tabs by tab_handle or tab_handles; call browser_list_tabs first to choose by title. "
        "window_index/tab_index still works for a single tab."
    ),
    "browser_observe": (
        "BATCH-FIRST HINT: observe once -> one browser_act -> verify from its changes; re-observe only for dependency/rerender, "
        "stale/takeover or consequential verification. Lists controls; to read a page: browser_do read."
    ),
    "browser_find": (
        "Read-only: find a rendered element by query/role/text; within='text in one item' limits it to that item. "
        "Not needed before acting: browser_act takes the same query/role/within and resolves the target itself."
    ),
    "browser_act": (
        "BATCH-FIRST: observe once->one browser_act->verify from its changes. Form:{type:'fill',values:{label:value}}; "
        "comment: {type:'post',text}. query/role/text_match +intent/within, no find first. Split only for dependencies."
    ),
    "browser_do": (
        "One-shot workflow: open URL -> actions -> extract -> close_after. Whole page: actions=[{type:'read'}]; "
        "PDF: {type:'pdf'}. tabs=[handles]: same reads on open tabs. Multi-step: observe -> browser_act instead."
    ),
    "browser_upload_artifact": (
        "Select a registered artifact (artifact_id + matching path) into an input[type=file] and verify it; never submits. "
        "Safari asks the owner first; owner_declined_upload means carry on without it. Chrome needs no question."
    ),
    "browser_checkpoint": (
        "Sign-in/2FA/passkey/captcha step: create (tab_handle) -> awaiting_human + notifies the user; wait (<=300s) or "
        "status -> resolved when the tab no longer shows it. Never type passwords or codes."
    ),
    "tool_discover": (
        "Find which tool to use, including less-common ones the profile allows: plain-word query, ranked results with why, "
        "short description, required fields and bounds; next_cursor pages more; include_schema=true: full schema."
    ),
    "memory_search": (
        "Recall what the owner told you before: call it when a request touches earlier work, a preference, decision, "
        "person, project or account, and before asking them. Skip self-contained tasks."
    ),
    "memory_add": (
        "Save on your own what the owner will expect you to remember: a lasting preference, correction, decision or "
        "stable fact. One short sentence; memory_search first, update instead of duplicating. No secrets."
    ),
    "memory_update": (
        "When the owner corrects or changes something you remembered: find it with memory_search, then update it by "
        "memory_id so the old version does not stay beside the new one. No secrets."
    ),
    "skill_search": (
        "Find a written procedure before a task that has one (Swift, Expo, frontend/design, Remotion, Office/PDF files, "
        "ads/SEO, debugging, code review); load the best match with skill_get and follow it."
    ),
    "skill_get": (
        "Load one Agent Skill by name (from skill_search) or SKILL.md path and follow it for the task; its scripts and "
        "references come as paths, read them only when the skill says to."
    ),
    "knowledge_search": (
        "Search the owner's chosen folders (PDFs, scans, Word, notes) locally in any language. Results give file, page, snippet and SHA-256: answer from them and cite file and page. path narrows to one folder."
    ),
    "ask_user": (
        "Ask the owner a quick question mid-task without stopping; then go on with the answer, or your own judgment if "
        "it is null. Use it instead of ending your turn, above all in background work. suggestions: one-click replies."
    ),
}
