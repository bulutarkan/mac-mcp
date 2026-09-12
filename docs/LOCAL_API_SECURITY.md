# Local API Security

## Boundary

Mac MCP has three distinct HTTP surfaces:

- `/mcp`: the agent/connector surface, protected by the configured global MCP authentication policy.
- `/api`: the legacy REST/OpenAPI surface, protected by the same request identity/authentication model.
- `/dashboard/api/*` and `/dashboard/events`: local operations/session/steering surfaces, restricted to loopback **and** protected by a separate per-user dashboard Bearer token.

`/dashboard` and its static CSS/JavaScript assets may load on loopback without a credential, but they contain no sensitive state by themselves. Sensitive data and actions require the dashboard token. `/health` remains unauthenticated for health probes but returns only non-sensitive server health metadata.

## Dashboard credential

The dashboard credential is intentionally separate from `MCP_API_KEY`. It is generated with a cryptographically strong random value and stored at `~/.mac-mcp/dashboard-token` by default. The token file is mode `0600`; the default state directory is mode `0700`. Symlink token paths and files owned by another Unix user are rejected.

The native menu app reads the token locally and sends it as `Authorization: Bearer …`. `mac-mcp dashboard` and the menu app launch the browser with the credential in the URL fragment (`#token=…`). URL fragments are not transmitted to the HTTP server or tunnel. Dashboard JavaScript moves the value to `sessionStorage`, removes the fragment from the visible URL, and authenticates both JSON fetches and the streaming telemetry request with the Bearer header.

No dashboard credential is accepted in a query string. This avoids local/access-log leakage and avoids creating a second query-token convention.

## CSRF and browser-origin requests

Dashboard authentication does not use cookies. State-changing local endpoints require an explicit Bearer header, so a normal cross-origin form/navigation cannot authenticate a write. Cross-origin JavaScript also cannot manufacture the secret Bearer value without already compromising the user's account/process boundary. Loopback checking remains in place before authentication, including forwarded-address checks, so a tunnel/proxy cannot make a remote request appear local.

## Identity and secret redaction

Telemetry sanitization redacts authorization/API credentials and normalized provider identity fields including OpenAI session, subject, organization, and location metadata. Steering uses hashed logical identities rather than raw provider session/subject values. The dashboard token is never printed by the CLI and should never be copied into telemetry.

On the first startup with this sanitizer version, existing telemetry rows are re-sanitized in place. Mac MCP enables SQLite secure-delete for the migration, truncates the WAL, and vacuums the bounded telemetry database so legacy provider identity values are not left behind in superseded/free pages.

## Unix-domain socket decision

A Unix-domain socket was evaluated for menu-app ↔ daemon traffic. A socket file can be mode-restricted to the current Unix user and removes TCP loopback exposure for that client. It was **not** adopted because:

1. the browser dashboard cannot directly consume a Unix socket, requiring a second transport or proxy;
2. a `0600` socket does not isolate a malicious process already running under the same uid, which is also able to read the user's token file and many other user resources;
3. maintaining one authenticated loopback protocol keeps menu app, browser dashboard, tests, and recovery behavior consistent.

A future privileged helper or dedicated non-admin service account could create a stronger containment boundary. A Unix socket may still be reconsidered if the browser dashboard is removed from the same transport.

## What this does not guarantee

Loopback is machine-local transport, not proof of the macOS user identity. The dashboard token adds a per-user file-secret boundary, but Mac MCP does not claim to sandbox mutually hostile processes running as the same logged-in user. For stronger containment, use a dedicated OS account and restrict filesystem/Automation permissions.
