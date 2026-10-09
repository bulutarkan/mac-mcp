# Contributing to Mac MCP

Thanks for helping improve Mac MCP.

Mac MCP controls real macOS resources: shell commands, files, browsers, apps, delegated agents, and remote-access surfaces. Changes should therefore be small, testable, security-conscious, and easy to review.

## Before you start

For bugs and feature requests, use the GitHub issue forms first when practical. For security vulnerabilities, follow [SECURITY.md](SECURITY.md) and do not publish sensitive details in a normal issue.

Keep each change focused. Avoid mixing unrelated cleanup, formatting, refactors, or generated files into the same pull request.

## Development setup

Clone the repository and install it in a Python environment:

```bash
git clone https://github.com/bulutarkan/mac-mcp.git
cd mac-mcp
python -m pip install -e .
```

Mac MCP currently supports Python 3.10+; CI exercises current supported Python versions on macOS.

The source repository is the canonical development copy. Do not treat an installed or deployed runtime copy as the source of truth, and do not overwrite local runtime secrets or settings while testing.

## Making a change

1. Start from an up-to-date branch based on `main`.
2. Inspect the existing implementation and tests before editing.
3. Make the smallest change that fully solves the problem.
4. Preserve unrelated user or contributor work.
5. Add or update regression tests when behavior changes.
6. Verify the real behavior, not only syntax or exit codes.

Never commit API keys, bearer tokens, cookies, `.env` secrets, private keys, dashboard tokens, browser companion tokens, or private runtime configuration.

## Tests

The server fails closed at import time without an API key, and shell/job tests need shell access enabled. Export the same test-only values CI uses (`.github/workflows/security-regression.yml`) in the terminal you run tests from. The key is a dummy placeholder: never reuse it as a real credential, and do not edit `.env`, settings or runtime state to make tests pass.

```bash
export MCP_API_KEY="ci-regression-only-0123456789abcdef0123456789abcdef"
export MCP_ALLOW_NO_AUTH=false
export MCP_ALLOW_SHELL=true
```

Run the most relevant targeted tests while developing:

```bash
python -m unittest tests.test_example -v
```

Before submitting a substantial backend or cross-cutting change, run the full regression suite:

```bash
python -m unittest discover -s tests -v
```

Also run:

```bash
python -m compileall -q mcp_server tests
bash -n install.sh
git diff --check
```

The GitHub Actions quality gates also run Python correctness checks and security assurance validation.

### Optional browser DOM regressions

Generated browser JavaScript has opt-in tests in an isolated headless Chromium.
They do not attach to the user’s Chrome or require a signed-in website. Install
the optional test dependency and browser, then enable these tests explicitly:

```bash
python -m pip install -e '.[browser-tests]'
python -m playwright install chromium
MAC_MCP_BROWSER_TESTS=1 python -m unittest discover -s tests -p 'test_browser_*_dom.py' -v
```

Use the same test-only MCP environment variables as the normal regression suite.
The Browser DOM Regression workflow runs this opt-in suite; ordinary unit tests
skip it when the flag is unset, without requiring Playwright at runtime.

## Native macOS app changes

For changes under `menu_app/`, build to a temporary output instead of replacing an installed app:

```bash
./menu_app/build_app.sh /tmp/mac-mcp-build
/usr/bin/codesign --verify --deep --strict "/tmp/mac-mcp-build/Mac MCP.app"
```

If the change affects visible UI, verify the rendered app as well. Source inspection alone is not enough for layout, state, focus, accessibility, or interaction changes.

Browser-control changes should preserve background-safe behavior and must not steal focus unless the action explicitly requires foreground interaction.

## Security-sensitive changes

Take extra care around:

- authentication and dashboard access;
- shell and file mutation;
- public endpoints and tunnels;
- browser automation and Computer Use;
- delegated-agent permissions and sandbox claims;
- update, installer, backup, rollback, and process ownership;
- secret handling and log sanitization.

Do not weaken a guard just to make a test pass. If a provider cannot enforce a security property, Mac MCP should report that limitation rather than claiming the protection exists.

## Pull requests

A good pull request:

- explains the problem and the chosen fix;
- stays focused on one logical change;
- includes tests for behavior changes;
- calls out security, compatibility, or migration impact;
- includes screenshots for visible UI work;
- identifies anything that still needs follow-up.

Complete the repository pull request template. CI should be green before merge.

By contributing, you agree that your contributions are licensed under the repository's [MIT License](LICENSE).
