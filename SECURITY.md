# Security Policy

Mac MCP can execute commands, read and write files, automate browsers and desktop apps, expose remote endpoints, and delegate work to external agent providers. Security reports are taken seriously.

## Supported versions

Security fixes are generally made on the current `main` branch and the latest published release. Older releases may not receive backported fixes.

If you are reporting a problem, include the Mac MCP version or commit when known.

## Reporting a vulnerability

Please do **not** open a public GitHub issue for a vulnerability that could expose secrets, bypass authentication or permissions, enable unintended code execution, escape a security boundary, or compromise another user's system.

Preferred reporting path:

1. Use GitHub's **Report a vulnerability** / private security reporting flow for this repository when it is available.
2. If private security reporting is unavailable, open a minimal issue asking the maintainer for a private contact path **without including exploit details, secrets, tokens, or proof-of-concept payloads**.
3. Move the technical details to the private channel before sharing reproduction steps or proof-of-concept material.

A useful report includes:

- affected version or commit;
- affected component;
- impact and realistic attack preconditions;
- reproducible steps or a minimal proof of concept;
- whether secrets, authentication, permissions, browser control, shell/file access, updates, or remote endpoints are involved;
- suggested mitigation, if you have one.

Please give the maintainer reasonable time to investigate and ship a fix before public disclosure.

## Scope

Examples of security-relevant areas include:

- MCP and dashboard authentication;
- local and remote authorization boundaries;
- shell, file, browser, and macOS UI controls;
- focus/foreground enforcement for Computer Use;
- provider access-mode and sandbox enforcement;
- public endpoint and tunnel configuration;
- update, installer, rollback, and process-identity verification;
- secret storage, redaction, and accidental credential disclosure;
- mobile pairing and session authorization.

Security properties differ between delegated providers. A provider limitation is not automatically a vulnerability if Mac MCP accurately reports the limitation and fails closed where a protection is required.

## Secrets in reports

Never paste real API keys, bearer tokens, cookies, session credentials, private keys, dashboard tokens, browser companion tokens, or private `.env` contents into an issue, pull request, log excerpt, or screenshot.

If a credential was exposed publicly, rotate or revoke it immediately.
