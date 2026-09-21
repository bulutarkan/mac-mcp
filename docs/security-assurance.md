# Security Assurance Matrix

This document is the public, regression-backed security assurance index for Mac MCP. It maps stable risk classes to the controls that mitigate them, the exact automated tests that keep those controls from silently drifting, and the release where the control entered the stable line.

The matrix is evidence, not a claim that software is risk-free. Entries intentionally stay at control and regression-test level; they do not publish credentials, machine-specific paths, exploit payloads, or operational secrets. Repository-relative source and test paths are sufficient for review.

`python3 scripts/verify_security_assurance.py --json` validates this document. CI fails if a referenced control file or test disappears, a test reference is not explicitly bound back to its assurance ID, an assurance-tagged test has no matrix record, the declared release/changelog linkage is missing, or this public document contains secret-like/private-path material.

## SEC-FS-001 — Scoped filesystem symlink / TOCTOU escape

- **Risk class:** Filesystem scope escape and check/use race
- **Current status:** Verified
- **Introduced / fixed release:** 2.1.4
- **Control:** Delegated scoped file operations revalidate at operation time and use descriptor-based traversal with no-follow semantics so a post-validation symlink swap cannot redirect access outside the allowed workspace.
- **Control paths:** `mcp_server/scoped_fs.py`, `mcp_server/tools_files.py`, `mcp_server/tools_search.py`
- **Regression tests:** `tests/test_scoped_file_symlink_safety.py::ScopedFileSymlinkSafetyTests.test_read_check_then_parent_symlink_swap_is_blocked`, `tests/test_scoped_file_symlink_safety.py::ScopedFileSymlinkSafetyTests.test_write_swap_after_safe_snapshot_never_writes_outside`, `tests/test_scoped_file_symlink_safety.py::ScopedFileSymlinkSafetyTests.test_descriptor_guard_blocks_swap_after_operation_time_revalidation`

## SEC-NET-001 — SSRF redirect / DNS destination drift

- **Risk class:** Server-side request forgery and destination revalidation
- **Current status:** Verified
- **Introduced / fixed release:** 2.1.4
- **Control:** Outbound HTTP validates every redirect destination, rejects non-public resolution unless explicitly allowlisted, disables inherited proxy routing, and revalidates the resolved destination at connect time; browser navigation independently validates requested and observed destinations.
- **Control paths:** `mcp_server/security.py`, `mcp_server/tools_http.py`, `mcp_server/tools_browser.py`
- **Regression tests:** `tests/test_ssrf_revalidation.py::SSRFRevalidationTests.test_public_to_loopback_redirect_is_blocked_before_second_request`, `tests/test_ssrf_revalidation.py::SSRFRevalidationTests.test_dns_rebinding_between_validation_and_connect_is_blocked_before_socket_connect`, `tests/test_ssrf_revalidation.py::SSRFRevalidationTests.test_browser_rebinding_same_hostname_is_caught_on_observed_revalidation`

## SEC-AUTHZ-001 — Permission / capability drift

- **Risk class:** Authorization-policy drift and delegated privilege widening
- **Current status:** Verified
- **Introduced / fixed release:** 2.0.3
- **Control:** Tool risk classification and permission profiles are centralized and deterministic; delegated resource scopes and capability presets can narrow but cannot widen their parent authorization boundary, and dynamic tool invocation inherits the target tool's effective risk.
- **Control paths:** `mcp_server/policy.py`, `mcp_server/policy_scope.py`, `mcp_server/tools_agents.py`, `mcp_server/observability.py`
- **Regression tests:** `tests/test_hardening_203.py::RiskAndScopeTests.test_registry_covers_current_96_tool_surface`, `tests/test_hardening_203.py::RiskAndScopeTests.test_profiles_are_deterministic`, `tests/test_hardening_203.py::RiskAndScopeTests.test_child_scope_can_only_narrow_parent`, `tests/test_security_boundaries.py::DelegatedCapabilityProfileTests.test_browser_only_child_cannot_spawn_full_child`

## SEC-UPD-001 — Failed-update rollback integrity

- **Risk class:** Interrupted update / rollback corruption
- **Current status:** Verified
- **Introduced / fixed release:** 2.0.4
- **Control:** The updater records pre-update state, restores runtime and source state after failed post-restart health verification when safe, and refuses destructive source rollback when concurrent user edits or commits make the original transaction assumptions invalid.
- **Control paths:** `mcp_server/update_helper.py`, `mcp_server/update_state.py`
- **Regression tests:** `tests/test_update.py::UpdateHelperTests.test_health_failure_rolls_back_split_repo_runtime_and_marker`, `tests/test_update.py::UpdateHelperTests.test_health_failure_restores_pre_update_repo_head_not_deployed_marker`, `tests/test_update.py::UpdateHelperTests.test_user_edit_after_merge_skips_repo_rollback_but_restores_runtime`

## SEC-TXN-001 — Atomic filesystem rollback under injected failure

- **Risk class:** Partial filesystem mutation after interrupted multi-action write
- **Current status:** Verified
- **Introduced / fixed release:** 2.1.4
- **Control:** Atomic file batches prepare bounded preimages before mutation and restore all prepared state when an injected mid-batch operation fails; operations that cannot prepare a complete rollback boundary are refused before mutation.
- **Control paths:** `mcp_server/file_transactions.py`, `mcp_server/tools_files.py`
- **Regression tests:** `tests/test_file_transactions.py::FileTransactionTests.test_atomic_batch_fault_in_second_write_restores_all_preimages`, `tests/test_file_transactions.py::FileTransactionTests.test_mixed_write_move_delete_batch_fault_rolls_back_everything`, `tests/test_file_transactions.py::FileTransactionTests.test_atomic_batch_refuses_irreversible_snapshot_before_mutation`

## SEC-AGENT-001 — Delegated agent control-plane lineage isolation

- **Risk class:** Cross-lineage agent metadata, result, log, and lifecycle control access
- **Current status:** Verified
- **Introduced / fixed release:** 2.1.5
- **Control:** Scoped agent identities are bound to persistent owner/root/parent lineage. Delegated agent-control calls permit self and descendants only, deny siblings/ancestors/unrelated teams by default, preserve local root administration, preflight mutating control before side-effect intents, and emit audited control-plane denials.
- **Control paths:** `mcp_server/tools_agents.py`, `mcp_server/scoped_auth.py`, `mcp_server/observability.py`
- **Regression tests:** `tests/test_agent_lineage_isolation.py::AgentLineageIsolationTests.test_sibling_and_unrelated_get_or_logs_are_denied`, `tests/test_agent_lineage_isolation.py::AgentLineageIsolationTests.test_sibling_control_actions_are_denied_before_mutation`, `tests/test_agent_lineage_isolation.py::AgentLineageIsolationTests.test_team_owner_and_ancestor_can_access_but_sibling_cannot`, `tests/test_agent_lineage_isolation.py::AgentLineageIsolationTests.test_legacy_parent_metadata_backfills_and_survives_restart_like_reload`, `tests/test_agent_lineage_isolation.py::AgentLineageObservedMCPTests.test_direct_and_tool_invoke_denials_match_and_emit_security_audit`

## SEC-GIT-001 — Delegated Git worktree isolation and safe apply

- **Risk class:** Concurrent delegated write-agent overwrite and source-checkout scope escape
- **Current status:** Verified
- **Introduced / fixed release:** 2.1.5
- **Control:** `workspace_write` Git agents default to per-agent ephemeral worktrees whose cwd/path scope is remapped inside an already-authorized parent root. Team tasks pin a common base; dependent/reviewer worktrees receive dependency patches without mutating the source checkout. Applying results back to the source tree is local/root-only and uses touched-path dirty/base checks, patch preflight, per-path CAS, bounded preimage rollback, and conflict-first failure rather than reset/clean/cherry-pick.
- **Control paths:** `mcp_server/agent_worktrees.py`, `mcp_server/tools_agents.py`, `mcp_server/policy_scope.py`
- **Regression tests:** `tests/test_agent_git_worktrees.py::AgentGitWorktreeCoreTests.test_two_parallel_write_worktrees_are_isolated_and_disjoint_apply_preserves_dirty_main`, `tests/test_agent_git_worktrees.py::AgentGitWorktreeCoreTests.test_overlapping_agent_edits_fail_closed_after_first_apply`, `tests/test_agent_git_worktrees.py::AgentGitWorktreeCoreTests.test_dependency_fan_in_combines_disjoint_worktree_changes`, `tests/test_agent_git_worktrees.py::AgentGitWorktreeSpawnIntegrationTests.test_spawn_internal_remaps_workspace_write_cwd_and_scope`, `tests/test_agent_git_worktrees.py::AgentGitWorktreeLifecycleTests.test_delegated_agent_cannot_apply_isolated_changes_to_source_tree`

## SEC-SCHED-001 — Cross-team global admission and shared-resource ownership

- **Risk class:** Cross-team provider overload, conflicting shared-resource mutation, and stale queued-work races
- **Current status:** Verified
- **Introduced / fixed release:** 2.1.5
- **Control:** Team tasks pass through a process-safe persisted global admission queue before provider start. Global/provider capacity, FIFO-aware fairness, scoped workspace/path/file claims, browser-tab parity, native/process/clipboard claims, lease heartbeat/TTL recovery, queue cancellation, and file expected-revision CAS prevent separate teams from concurrently owning conflicting resources while leaving independent resources parallel.
- **Control paths:** `mcp_server/agent_admission.py`, `mcp_server/tools_agents.py`, `mcp_server/file_transactions.py`, `mcp_server/tools_files.py`
- **Regression tests:** `tests/test_agent_global_admission.py::GlobalAdmissionCoreTests.test_three_teams_exceed_provider_limit_and_third_is_queued`, `tests/test_agent_global_admission.py::GlobalAdmissionCoreTests.test_fifo_fairness_prevents_younger_conflicting_request_bypass`, `tests/test_agent_global_admission.py::GlobalAdmissionCoreTests.test_bind_heartbeat_and_ttl_prune_release_crashed_agent`, `tests/test_agent_global_scheduler.py::CrossTeamGlobalSchedulerTests.test_overlapping_cross_team_workspace_serializes_but_distinct_workspace_runs`, `tests/test_agent_global_scheduler.py::CrossTeamGlobalSchedulerTests.test_team_cancel_removes_queued_request_without_releasing_other_active_lease`, `tests/test_agent_global_scheduler.py::CrossTeamGlobalSchedulerTests.test_file_expected_revision_conflict_fails_before_spawn`

## SEC-COMP-001 — Closed-loop computer-plan recovery without duplicate UI mutation

- **Risk class:** Stale/ambiguous UI target recovery and duplicate consequential action replay
- **Current status:** Verified
- **Introduced / fixed release:** 2.1.5
- **Control:** `computer_plan` v2 may recover only from bounded, pre-mutation stale/readiness failures. It re-observes and uniquely rebinds browser/native semantic targets, uses AXIdentifier when available, refuses ambiguous identity, enforces recovery/time/action budgets and resource preflight, and never auto-replays mutating work after `ACTION_NO_EFFECT`, policy denial, `outcome_unknown`, prior partial mutation, or an ambiguous nested-tool exception. Browser click verification no longer treats unrelated DOM revision churn as proof of success; it requires navigation, target/control/modal state change, or bounded action-correlated fetch/XHR initiation.
- **Control paths:** `mcp_server/computer_plan.py`, `mcp_server/tools_ui.py`, `mcp_server/native_action_verification.py`, `mcp_server/agent_admission.py`, `mcp_server/tools_browser_agent.py`
- **Regression tests:** `tests/test_computer_plan_recovery.py::ComputerPlanRecoveryTests.test_spa_stale_node_reobserves_semantically_rebinds_and_finishes`, `tests/test_computer_plan_recovery.py::ComputerPlanRecoveryTests.test_ambiguous_native_rebind_fails_closed_without_second_mutation`, `tests/test_computer_plan_recovery.py::ComputerPlanRecoveryTests.test_action_no_effect_and_outcome_unknown_are_never_replayed`, `tests/test_computer_plan_recovery.py::ComputerPlanRecoveryTests.test_recovery_budget_exceeded_stops_before_fresh_observe`, `tests/test_computer_plan_recovery.py::ComputerPlanRecoveryTests.test_resource_busy_preflight_stops_before_any_nested_tool`, `tests/test_browser_readiness.py::BrowserElementReadinessTests.test_unrelated_dom_revision_is_not_accepted_as_click_effect`, `tests/test_browser_readiness.py::BrowserElementReadinessTests.test_action_correlated_network_activity_is_accepted_as_effect`

## SEC-FOCUS-001 — Browser foreground capability cannot be self-granted by automation

- **Risk class:** Agent-driven focus theft, active-tab hijacking, and foreground fallback escalation
- **Current status:** Verified
- **Introduced / fixed release:** 2.1.6
- **Control:** Foreground browser behavior is protected by an internal lexical capability that is not exposed through MCP/REST parameters. `allow_foreground=true`, `background=false`, and `browser_activate_tab` cannot mint focus authority. Current/active tab selection is treated as user-visible behavior, Safari native file upload fails closed without the capability, and only the authenticated localhost **Show Tab** UI route opens the scoped internal grant around exactly one activation call. Chrome debugger-based background execution/upload remains unaffected, while the legacy Chrome `javascript:` URL bridge is treated as foreground-capable and fails closed without the same internal capability. Browser click actions default to synthetic background DOM input; `input_mode=trusted` is an explicit Chrome Background Companion-only debugger pointer request, never an automatic fallback. Safari trusted-input requests fail closed with no foreground/coordinate escalation, and no-effect synthetic actions are never replayed as trusted input.
- **Control paths:** `mcp_server/foreground_guard.py`, `mcp_server/tools_browser.py`, `mcp_server/tools_browser_agent.py`, `mcp_server/chrome_background_bridge.py`, `menu_app/ChromeVisualCompanion/background.js`, `mcp_server/dashboard_routes.py`, `mcp_server/conformance.py`
- **Regression tests:** `tests/test_browser_foreground_guard.py::ForegroundCapabilityGuardTests.test_activate_tab_is_user_visible_and_denied_even_when_allow_foreground_false`, `tests/test_browser_foreground_guard.py::ForegroundCapabilityGuardTests.test_agent_cannot_self_authorize_activate_tab_with_true_flag`, `tests/test_browser_foreground_guard.py::ForegroundCapabilityGuardTests.test_foreground_open_flags_are_denied_before_browser_script`, `tests/test_browser_foreground_guard.py::ForegroundCapabilityGuardTests.test_safari_upload_is_denied_before_artifact_or_tab_side_effect`, `tests/test_browser_foreground_guard.py::ForegroundCapabilityGuardTests.test_internal_grant_allows_exact_foreground_operation_and_does_not_leak`, `tests/test_browser_foreground_guard.py::ForegroundCapabilityGuardTests.test_computer_plan_rejects_user_visible_tab_activation_tool`, `tests/test_browser_foreground_guard.py::ForegroundCapabilityGuardTests.test_browser_act_key_cannot_escalate_via_allow_foreground_flag`, `tests/test_browser_foreground_guard.py::ForegroundCapabilityGuardTests.test_chrome_url_js_bridge_fails_closed_before_any_applescript`, `tests/test_browser_foreground_guard.py::ForegroundCapabilityGuardTests.test_chrome_direct_js_denial_cannot_escalate_into_url_bridge`, `tests/test_browser_foreground_guard.py::ForegroundCapabilityGuardTests.test_cached_chrome_native_js_denial_still_cannot_enter_url_bridge`, `tests/test_browser_readiness.py::BrowserElementReadinessTests.test_trusted_input_fails_closed_on_safari_without_foreground_fallback`, `tests/test_browser_readiness.py::BrowserElementReadinessTests.test_trusted_chrome_click_uses_background_debugger_coordinates_and_verifies_effect`, `tests/test_chrome_background_transport.py::ChromeBackgroundTransportTests.test_dispatch_mouse_rpc_is_explicit_and_bounded`

## Maintaining the matrix

Security changes should reuse an existing assurance ID when they strengthen the same risk/control boundary, or add a new ID when they introduce a materially different boundary. The relevant CHANGELOG bullet must include the ID in square brackets, and every listed regression method must carry a nearby `# ASSURANCE: <ID>` marker. Do not add a matrix row for a control that has no automated regression evidence.

The verifier is deliberately structural. It proves that the published evidence graph is internally consistent and still points to real code/tests/releases; the security regression workflow remains responsible for actually executing the referenced tests as part of the full test suite.
