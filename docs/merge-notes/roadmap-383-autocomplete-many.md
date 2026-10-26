# Merge handoff: Roadmap #383, batched autocomplete selection

Branch: `experiment/roadmap-383-autocomplete`

## What changed

- Added `autocomplete_many` to the existing `browser_act` action schema and dispatcher.
- Selects 1–10 unique labels sequentially in a single MCP `browser_act` call. It waits for dynamic suggestions with a `MutationObserver`, uses exact normalized label matching, scopes suggestions to the input's `aria-controls` listbox when available, and verifies committed chips using `selected_selector`.
- Supports suggestion count suffixes (for example `Automation (5.2K)`) and chip remove-label verification (for example `aria-label="Remove Automation"`).
- Reports completed, remaining, ambiguous, and failed values; does not automatically retry uncertain mutations. Revalidates tab ownership during polling and attempts cancellation if ownership changes.
- Includes browser input redaction and payload inspection for the new values array.

## Example

```json
{
  "browser": "Safari",
  "actions": [{
    "type": "autocomplete_many",
    "selector": "input[role='combobox'][aria-controls='tagMultiSelectMenu']",
    "option_selector": "button",
    "selected_selector": "button[aria-label^='Remove ']",
    "values": ["Automation", "Technology", "Artificial Intelligence", "Productivity", "Writing"],
    "timeout_ms": 3500
  }]
}
```

## Verification performed

1. Python regression tests: 42/42 passed (including `test_browser_autocomplete_many`, batch guidance, action limits, human takeover revalidation, and background keys).
2. `node --check mcp_server/browser_js/autocomplete_many.js`: passed.
3. `git diff --check`: passed.
4. Isolated browser fixture: five chips selected and DOM-verified on both installed Chrome/Chromium and Playwright WebKit. Additional E2E scenarios: partial selection with missing suggestion, ambiguous duplicate suggestion, already-selected chip, and explicitly scoped suggestions.
5. Isolated MCP server on `127.0.0.1:8878`: `/health?probe=basic` returned HTTP 200. This server was stopped after testing; production port `8765` remained healthy.
6. **Real Medium Safari test**, on a disposable unpublished draft, with the isolated MCP server: one `browser_act` containing one `autocomplete_many` selected **5/5** topics (`Automation`, `Technology`, `Artificial Intelligence`, `Productivity`, `Writing`), all with `verification: selected_tags_verified`, `outcome: confirmed`, `remaining: []`, `error: null`. Client-observed wall time **3.529 seconds**; server `duration_ms: 3022`, browser JS `duration_ms: 2936`, internal JS calls: 15. `Automation` was already selected from earlier exploratory inspection; the action correctly recognized it and selected the remaining four. **The real-world 5/5 result therefore includes one already-selected item.** The draft was not published.

## Merge-agent review checklist

- Rebase/cherry-pick only after reviewing current `main` and concurrent browser changes; this branch is deliberately not merged into `main`.
- Review cross-origin selector scope, async cancellation and tab-lease behavior, state cleanup, timeout bounds, and outcome-unknown semantics. In particular, verify the native browser transport's JS lifecycle when the page rerenders or navigates.
- Consider making `selected_selector` verification robust to different tag/chip structures without weakening fail-closed matching.
- Add coverage for rerender and ownership changes during an active autocomplete action, and a clean five-from-zero real Medium test, before production rollout.
- Keep background-only behavior and permission checks unchanged. Do not publish the Medium test draft.

## Scope

Only this feature branch was modified. No production runtime deployment, version bump, merge into `main`, or release was performed.
