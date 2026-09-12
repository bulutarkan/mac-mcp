# Mac MCP Terminology

Mac MCP uses the following product/security terms consistently in documentation and UI. These definitions describe the actual enforcement boundary; they are not marketing shorthand.

## Background browser automation

**Visible, non-focus-stealing browser automation.** Mac MCP controls a normal Safari or Chrome tab without bringing that browser/tab to the front. Background does **not** mean hidden, invisible, or headless.

## Capability

A **capability** is a tool or risk class that the Mac MCP server policy allows for the current permission profile. Allowed capability means the server permits the operation; it does **not** imply that a human confirmation prompt will appear.

## Approval

**Approval** is a human-confirmation mechanism that runs before an action and has an explicit source: client, Mac MCP server, external guard, or none. Current built-in Mac MCP profiles report approval source `none`; client-side approval may still exist independently.

## Localhost / loopback

**Machine-local transport, not user isolation.** Loopback restricts network reachability to this Mac. It does not prove which local macOS user/process made the request and is not a same-user sandbox. Sensitive dashboard endpoints therefore also require a per-user Bearer token.

## Mac MCP logical session

A **Mac MCP logical session** is the local steering/session identity used to keep one conversation/agent flow coherent across tool calls. It is derived from supported client metadata or transport identity and is represented internally with a local hashed identity. It is **not** the raw provider session/account identifier.

## Dedicated user

A **dedicated non-admin user** is a deployment-hardening/containment technique that reduces the files, credentials, and macOS permissions reachable by the service. It improves blast-radius containment; it is **not** a complete sandbox or virtualization boundary.

## Sandbox

Use **sandbox** only for a real OS/provider enforcement boundary. Do not use the word as a synonym for localhost, read-only mode, approval, a permission profile, or a dedicated user.

## Three quick checks

- **Is background browser work visible?** Yes. It uses a normal visible browser tab while avoiding focus stealing.
- **Does an allowed write always prompt the user?** No. Capability and approval are separate; current built-in profiles do not add an automatic Mac MCP confirmation prompt.
- **Does localhost mean only the same macOS user can access it?** No. It means machine-local transport, not same-user isolation.
