# Browser text input acceptance

`browser_act` text actions and `browser_type_selector` share target readiness,
input dispatch, and delayed DOM readback. Native inputs and textareas use their
native value setter and input events. Rich text receives a synthetic in-page
paste event containing plain text and escaped paragraph HTML, after synchronizing
the DOM selection with the editor. Plain contenteditable elements without a
paste handler use the browser editing command. Neither path activates Chrome or
uses the operating system clipboard.

With `clear=true`, text replaces the editable contents. With `clear=false`, text
appends. A CSS selector must resolve to exactly one element. Missing, ambiguous,
readonly, disabled or occluded targets fail before text is dispatched.

Success requires the requested text to match in two delayed readbacks. Results
include `verification="dom_readback_verified"` and
`persistence_verified=false`. A rejected or reverted edit returns `ok=false` and
`error="input_not_applied"`. Input is not automatically replayed on failure.
An editor that ignores synthetic paste can therefore report failure even if it
would accept foreground native input; no foreground fallback is attempted.

## Compatibility

`browser_type_selector` previously returned a JavaScript result string such as
`result="OK"`, even when a DIV's visible text had not changed. It now returns the
same structured action response as `browser_act`, with top-level `ok` and an
`actions` array. Callers should inspect `ok` and the action's verification/error
fields instead of checking the old string. The tool's arguments are unchanged.

DOM acceptance does not establish application autosave or server persistence.
For a workflow requiring saved content, check the application's save outcome and
read the content again through its durable storage or after a suitable reload.

The optional Chromium tests use an offline fixture with a separate editor
selection model, accepted/rejected paste handlers and asynchronous rollback.
See `CONTRIBUTING.md` for the opt-in test command. Framework-specific policies,
transforms, collaboration and save behavior still require application testing.
