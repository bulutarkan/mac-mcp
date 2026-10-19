"""Keep tests that build the app away from the user's real Mac MCP state.

create_app() opens the telemetry/security-event database, creates the
dashboard token, prunes file-transaction journals, reads settings.json and
may refresh the CLI launcher. Importing this module points all of those at one throwaway
directory for the test process, so a test run never writes into ~/.mac-mcp or ~/.local/bin
and behaves the same as on a fresh CI machine. Paths already in the environment (for example
a shell that exported the real state directory) are replaced, unless MAC_MCP_TEST_AMBIENT_ENV=1.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile

ROOT = tempfile.mkdtemp(prefix="mac-mcp-test-state-")
atexit.register(shutil.rmtree, ROOT, True)

DEFAULTS = {
    "MAC_MCP_STATE_DIR": os.path.join(ROOT, "state"),
    "MAC_MCP_SETTINGS_PATH": os.path.join(ROOT, "state", "settings.json"),
    "MAC_MCP_TELEMETRY_DIR": os.path.join(ROOT, "state", "dashboard"),
    "MAC_MCP_DASHBOARD_TOKEN_FILE": os.path.join(ROOT, "state", "dashboard-token"),
    "MAC_MCP_CLI_PATH": os.path.join(ROOT, "bin", "mac-mcp"),
    "MAC_MCP_SKIP_MENU_APP_INSTALL": "1",
    # Undo records read the real app around each change; adapter tests fake the app's answers instead.
    "MAC_MCP_APP_UNDO": "0",
    # Never the owner's Messages: a test that wants the reader patches messages_reader itself.
    "MAC_MCP_MESSAGES_READER": os.path.join(ROOT, "no-messages-reader.app"),
}
_KEEP = os.environ.get("MAC_MCP_TEST_AMBIENT_ENV") == "1"
for _name, _value in DEFAULTS.items():
    if _KEEP:
        os.environ.setdefault(_name, _value)
    else:
        os.environ[_name] = _value
