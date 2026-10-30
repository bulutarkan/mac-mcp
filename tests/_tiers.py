"""Test tiers for CI (#191).

Swift-only modules compile and run menu app sources and never import the Python server,
so their result does not depend on the Python version. CI runs them once, on the primary
Python; the other Python versions run the rest (`scripts/run_tests.py --tier python`).
tests/test_test_tiers.py keeps this list in step with the files.
"""
from __future__ import annotations

SWIFT_ONLY = (
    "test_browser_tab_pulse",
    "test_chat_library",
    "test_chat_long_transcript",
    "test_live_island_hit_area",
    "test_live_island_iphone_notice",
    "test_live_island_waiting_peeks",
    "test_login_item",
    "test_menu_agent_failure_aging",
    "test_menu_chatgpt_resilience",
    "test_menu_hover_tip",
    "test_menu_popup_states",
    "test_menu_relaunch_steering_recovery",
    "test_menu_session_groups",
    "test_menu_state_performance",
    "test_menu_status_surfaces",
    "test_menu_steering_generation_recovery",
    "test_menu_steering_idempotency",
    "test_menu_team_cards",
    "test_native_accessibility",
    "test_onboarding_bridge",
    "test_onboarding_checklist",
    "test_owner_prompt_placement",
    "test_provider_usage_section",
    "test_scheduled_task_checks_native",
    "test_scheduler_watchdog_native",
    "test_settings_rules",
    "test_settings_search",
    "test_team_presets_native",
    "test_team_usage_native",
    "test_tool_activity_bubble_epoch",
    "test_update_progress_surface",
)
