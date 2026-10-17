# Safety, permissions and undo

What the security words mean, permission profiles and Server Approval, the outbound URL boundary, file undo and the security assurance checks.

[← Back to the README](../README.md)

## Product and security terminology

Mac MCP uses a small canonical glossary so UI and documentation do not imply stronger isolation or invisibility than the product actually provides. See [`docs/TERMINOLOGY.md`](TERMINOLOGY.md) for the full definitions.

- **Background browser automation:** visible, non-focus-stealing Safari/Chrome automation; not hidden or headless.
- **Capability:** whether the Mac MCP server policy permits a tool/risk class.
- **Approval:** a human-confirmation mechanism and its source; separate from capability enforcement.
- **Localhost / loopback:** machine-local transport, not same-user isolation or a sandbox.
- **Mac MCP logical session:** a local hashed steering identity that keeps one agent/conversation flow coherent; not the raw provider identity.
- **Dedicated user:** containment/hardening that reduces blast radius; not a complete sandbox.

For an advanced two-account deployment, see **[Hardened deployment with a dedicated non-admin macOS user](HARDENED_DEDICATED_USER.md)**. It covers loopback authentication, an explicit ACL-shared directory, TCC/GUI-session limits, tool behavior, rollback, and a two-user validation matrix.

## Permission profiles and approval semantics

Mac MCP treats **capability enforcement** and **human approval** as separate security concepts. A capability being allowed means only that the Mac MCP server policy permits that tool/risk class. It does **not** mean a second confirmation prompt will appear before the action runs.

| Profile | Server-enforced capability behavior | Approval source with Server Approval `Off` | Automatic risk prompt |
| --- | --- | --- | --- |
| `trusted` | All registered capabilities; destructive families are not additionally restricted by the profile. | `none` | No |
| `standard` | Blocks `raw_execution` and `update_control`; destructive operations are limited to browser/accessibility families; access-mode ceiling is read-only. | `none` | No |
| `read_only` | Allows read/network/browser/native-accessibility capabilities only and denies destructive calls. | `none` | No |

**Server Approval** is an optional approval overlay, separate from these capability profiles. It defaults to `Off`. `Critical` requires Mac MCP **Allow Once / Block** confirmation for raw execution, update control, and destructive process-control calls. `High Risk` adds destructive external/browser/native actions. The overlay is evaluated only after capability enforcement, uses exact-action single-use grants, denies when an approval is required but the local approval provider is unavailable or times out, and can be changed from **Settings → Permissions & Safety** without restarting the server. Mac MCP does not accept a client-supplied “already approved” claim as proof to bypass this server gate; client/external approval may still apply independently.

`ask_confirmation` remains an explicit interaction tool and is not a blanket confirmation wrapper around normal tool calls. Separately, Mac MCP has mandatory narrow server-side security gates for risky trust-boundary crossings. Under `standard` and scoped delegated profiles, an untrusted web context → privileged host action can require a source-aware **Allow Once / Block** decision. The global `trusted` profile intentionally skips that routine web→host confirmation so trusted interactive workflows are not interrupted on every shell/file/UI hop unless the optional Server Approval overlay independently matches the action. Detected credential/secret egress to an untrusted origin remains source-aware and approval-gated even under `trusted`. Detection also looks inside base64/base64url (padded, unpadded or split into lines), hex and percent-encoded parts of an outgoing payload, one encoding inside another included, so encoding a secret read on this Mac before sending it does not get around the gate. Secrets from `NAME_TOKEN=` / `SERVICE_API_KEY=` style lines are remembered when such a file is read. This is a check for common encodings, not a guarantee against any transformation: an agent that encrypts or splits a secret across requests is not detected. Exact-action grants remain origin-bound and single-use.

Delegated Codex workers currently run with Codex `approval_policy="never"` and Claude Code workers with `--permission-mode bypassPermissions`; their sandbox/access mode is separate from human approval. OpenCode permission behavior is also provider-side and must not be treated as a Mac MCP server confirmation guarantee.

Set the server capability profile with `MAC_MCP_PERMISSION_PROFILE=trusted|standard|read_only`. The native menu bar app reads `/dashboard/api/security/semantics`. Settings › Permissions & Safety first says in plain words what AI apps can do under each profile and when Mac MCP asks you first under each Server Approval level, with a choice for each; **Details** shows **Allowed Capabilities**, **Approval Behavior**, and the optional **Server Approval** risk profile separately. Permission-profile changes persist in `mcp_server/.env`; Server Approval persists as `security.server_approval_profile` in owner-only `~/.mac-mcp/settings.json`. Both apply to new requests immediately without restarting the server. Existing delegated agents keep the scoped capability profile issued when they were started; the server-side risk approval overlay remains a server execution gate.

Browser resilience guards are configurable with `MAC_MCP_NO_PROGRESS_THRESHOLD` (default `4`, range `2–10`) and `MAC_MCP_TAB_LEASE_TTL_S` (default `300` seconds, range `30–3600`). The no-progress breaker stops only repeated meaningful browser actions that fail to change DOM revision, URL, or title; wait/scroll/extract flows do not consume that budget. Delegated-agent tab ownership is logical and time-bounded: completing/cancelling/crashing an agent releases ownership without closing the user's tab, and the next agent must make a fresh `browser_observe` before acting on a previously owned handle.

### Outbound URL / SSRF boundary

`http_request` and `browser_open_url` treat public hostname allowlists and private-network access as separate permissions. A wildcard `HTTP_ALLOWLIST=*` or `BROWSER_ALLOWLIST=*` permits public hostnames; it does **not** permit loopback, RFC1918/ULA, link-local/metadata, carrier-grade NAT, multicast, unspecified, reserved, or other non-global address space. Hostnames that resolve to any blocked address fail closed. URL userinfo and non-HTTP(S) schemes are also rejected.

For `http_request`, every redirect hop is revalidated before the next request. The HTTP transport additionally resolves again at TCP-connect time, rejects a DNS answer that has changed into blocked address space, and connects to the already-validated IP while retaining the original hostname for HTTP/TLS identity. Environment proxy variables are deliberately not inherited by this host-access path. Redirect output is bounded to destination origins rather than copying full redirect paths or query strings.

Browser engines own their own network stack, so Mac MCP does not claim transport-level IP pinning for Safari or Chrome. Instead, browser navigation validates DNS before navigation and then revalidates the browser-observed destination URL after navigation; a same-host DNS change is resolved again at that boundary. If a newly created tab is observed on a blocked destination it is closed best-effort and the call fails as `browser_redirect_blocked`; for an existing tab Mac MCP best-effort restores the previously observed safe URL.

Local/private development access is opt-in by hostname through `HTTP_PRIVATE_ALLOWLIST` and `BROWSER_PRIVATE_ALLOWLIST` (comma-separated names/suffixes, for example `localhost`). These exceptions are independent from the normal public allowlists; `*` is intentionally ignored in the private allowlists so private-network access cannot be enabled accidentally.

Untrusted web provenance is **sticky at the logical-session level**. Once a session consumes third-party browser content, writing that content to a local scratch file, reading it back, closing the tab, or passing through unrelated read-only tools does not erase that provenance. Scoped/non-trusted workflows continue through the web→host security gate until the work moves to an independent clean-room session. The global `trusted` profile keeps the sticky provenance for audit and secret-egress checks but does not show a routine Allow Once dialog for each normal privileged host hop. Delegated children spawned from a tainted session inherit the taint metadata (origin, reason, and credential fingerprints) without copying raw DOM or secrets into the security log; nested delegation preserves the inheritance chain. A genuinely independent MCP session with no transferred payload starts clean under the normal policy.

## Transactional filesystem changes and undo

Mac MCP journals its primary destructive file operations before changing the filesystem. `write_file`, `edit_file`, `move_file`, and `delete_path` now return a random `transaction_id` plus `undoable`, `undo_expires_at`, and any explicit `irreversible_reason`. `write_files_batch(..., atomic=true)` prepares all preimages before the first write and restores the whole batch if any later write fails. `file_transaction_batch` extends the same all-or-nothing boundary to a mixed sequence of up to 50 `write`, `move`, and `delete` actions. Recent reversible transactions can be restored with `file_transaction_undo`; by default undo refuses to overwrite files changed after the original commit, while `force=true` is an explicit conflict override.

The journal lives under `~/.mac-mcp/transactions/` by default. The directory and transaction folders are owner-only (`0700`); manifests and snapshot archives are `0600`. Manifests contain only the paths/state required for restore, hashes/fingerprints, sizes, timestamps, and transaction metadata — never the new file content or edit text. Reversible preimages necessarily contain the original bytes, so they are kept only in owner-only snapshot archives with bounded retention. Defaults are a 7-day undo/expiry window, 64 transactions, 1 GiB total journal storage, and 256 MiB of preimage data per transaction; expired entries are pruned at daemon startup and on journal activity, while count/byte caps are enforced on every transaction. The corresponding `MAC_MCP_FILE_JOURNAL_*` environment variables can tighten those bounds.

If a single write/move/delete preimage exceeds the snapshot limit, the operation preserves backwards compatibility but returns `undoable=false` with `irreversible_reason=snapshot_limit_exceeded`. Atomic batch operations are stricter: if a complete rollback snapshot cannot be prepared, the batch is refused **before the first mutation**. A process crash after prepare but before commit leaves a `prepared` journal entry; normal undo treats that outcome as unknown, while an explicit `force=true` can restore the recorded pre-state. Delegated-agent undo also re-checks every transaction path against the agent's current resource scope, so a transaction ID cannot bypass workspace confinement. `copy_file` and `create_directory` are not part of this initial transaction-journal boundary.

### Symlink-safe delegated file scopes

Delegated calls with explicit `path_roots` use a second, operation-time filesystem boundary in addition to the normal server policy check. Scoped reads, writes, edits, moves, copies, deletes, directory traversal, filename/content search, transaction snapshots, and undo/rollback walk path components with directory file descriptors plus `O_NOFOLLOW` rather than trusting a path string that was checked earlier. A symlink swap between policy validation and the actual filesystem operation therefore fails closed instead of following the replacement outside the workspace. Recursive find/search/tree operations report or skip symlink entries without traversing through them, and direct access to a symlink that resolves outside the allowed roots is denied.

This stricter traversal is applied only when a delegated resource scope has explicit `path_roots`; ordinary local/unscoped file behavior remains compatible. Scoped cross-device moves are rejected rather than falling back to a copy/delete sequence that would weaken the descriptor boundary. Race failures return structured `scoped_path_unsafe` errors and do not expose outside-workspace file contents.

## Security assurance

Mac MCP publishes a regression-backed [Security Assurance Matrix](security-assurance.md) that maps stable risk classes to their controls, exact automated tests, and release history. `scripts/verify_security_assurance.py` validates those links in CI so renamed tests, missing controls, orphan assurance tags, missing CHANGELOG linkage, and secret-like/private-path material in the public matrix fail the gate instead of silently going stale.

Delegated agent control-plane access is lineage-scoped. A scoped agent can inspect and manage itself and descendants it owns, but sibling, ancestor, and unrelated agent/team metadata, results, logs, waits, and lifecycle actions fail closed. The local authenticated/root control plane keeps its existing administrative view. Owner/root/parent lineage is persisted in agent/team metadata and denial events are recorded without opening a mutating side-effect intent.
