"""One environment for every test run, whatever shell it starts from.

Mac MCP reads its security settings from the process environment (the runtime loads them from its
.env). A test run must not depend on that: on the developer's Mac the shell may hold the runtime's
real key and flags, in a fresh terminal it holds none, and the suite used to pass in one and fail in
the other. Importing this module sets the same values CI uses (a fake API key, auth required, shell
allowed) and removes settings that would change behavior from outside, so every run starts the same.

Set MAC_MCP_TEST_AMBIENT_ENV=1 to keep the caller's environment on purpose.
"""
from __future__ import annotations

import os

BASELINE = {
    "MCP_API_KEY": "test-only-key-0123456789abcdef0123456789abcdef",  # not a secret: tests only
    "MCP_ALLOW_NO_AUTH": "false",
    "MCP_ALLOW_SHELL": "true",
    # Tests never read this Mac's provider logins or call the providers' quota endpoints.
    "MAC_MCP_PROVIDER_QUOTA": "off",
}
# Runtime .env and shell settings that would make results depend on this Mac.
AMBIENT = (
    "MAC_MCP_PERMISSION_PROFILE", "MAC_MCP_PUBLIC_ENDPOINT_MODE", "NGROK_DOMAIN",
    "BROWSER_ALLOWLIST", "BROWSER_HTTPS_ONLY", "HTTP_ALLOWLIST", "HTTP_HTTPS_ONLY", "HTTP_MAX_RESPONSE_BYTES",
    "HTTP_TIMEOUT_S", "DEFAULT_COMMAND_TIMEOUT_S", "MAX_COMMAND_TIMEOUT_S", "MAX_OUTPUT_CHARS", "RATE_LIMIT_PER_MINUTE",
    "MAC_MCP_API_KEY",
    # Test mode and the trusted-signer overrides: a test that wants them sets them itself.
    "MAC_MCP_TEST_MODE", "MAC_MCP_RELEASE_TRUSTED_SIGNERS", "MAC_MCP_CATALOG_TRUSTED_SIGNERS",
)
APPLIED = os.environ.get("MAC_MCP_TEST_AMBIENT_ENV") != "1"

if APPLIED:
    for _name in AMBIENT:
        os.environ.pop(_name, None)
    os.environ.update(BASELINE)
