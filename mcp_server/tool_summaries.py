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

CORE_TOOL_SUMMARIES: Dict[str, str] = {
    "run_command": (
        "Run a short shell command in zsh and wait for it (full access unless scoped); long builds or servers belong in "
        "start_background_job. reversible=true records file changes for file_transaction_undo."
    ),
    "file_transaction_undo": (
        "Undo a recent reversible filesystem transaction by transaction_id (file tools, reversible shell/jobs, compound "
        "receipts). Refuses to overwrite later changes unless force=true; partial captures never claim full undo."
    ),
    "spawn_agents": (
        "Spawn 1-10 background delegated agents as one team; access_mode defaults to read_only. "
        "Supports task DAGs (depends_on), reviewer gates and admission budgets. Collect results with wait_agents."
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
        "Typed adapters for Finder, Notes, Mail, Calendar, Preview and System Settings; action=capabilities lists actions. "
        "Keeps the user's focus by default; unsupported actions return a mac_observe/mac_act fallback."
    ),
    "computer_plan": (
        "Run a bounded closed-loop macOS/browser plan in one call: waits, branches, bounded retries and target rebind. "
        "Uncertain side effects are never replayed; every step keeps normal policy and verification."
    ),
    "browser_close_tab": (
        "Close one or more browser tabs by tab_handle or tab_handles; call browser_list_tabs first to choose by title. "
        "window_index/tab_index still works for a single tab."
    ),
    "browser_observe": (
        "BATCH-FIRST HINT: observe once -> one browser_act for all independent controls -> verify once. Re-observe only for "
        "dependency/rerender, stale/takeover risk, or consequential verification. Returns DOM element IDs."
    ),
    "browser_find": (
        "Find one rendered browser element by query/role/text with exact-first ranking; within='text in one item' limits it "
        "to that item, nearest first. wait_timeout_s>0 waits for it. Act on it with browser_act."
    ),
    "browser_act": (
        "BATCH-FIRST: forms observe once -> one browser_act with type/select/click/scroll -> observe verify. "
        "Custom dropdowns: select. Targets: element_id or query/role/text_match +intent/within. Split only for dependencies."
    ),
    "browser_do": (
        "Preferred one-call browser transaction: open/act/extract in one call, e.g. extract=['price','rating'] for compact reads. "
        "Leave return_state='none' normally; debug=true exposes raw state."
    ),
    "browser_upload_artifact": (
        "Select a registered artifact (artifact_id + matching path) into an input[type=file] and verify it. Selects only, "
        "never submits the form; Safari needs foreground capability and fails closed rather than stealing focus."
    ),
    "ask_user": (
        "Ask the local user a question in a native macOS dialog and return their typed answer; skip or timeout returns "
        "response=null. Use it for approval, preferences or missing information."
    ),
}
