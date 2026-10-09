"""Keep tests that build the app away from the user's real Mac MCP state.

create_app() opens the telemetry/security-event database, creates the
dashboard token, prunes file-transaction journals, reads settings.json and
may refresh the CLI launcher. Importing this module points all of those at one throwaway
directory for the test process (only where the caller has not chosen a path
already), so a test run never writes into ~/.mac-mcp or ~/.local/bin and behaves the
same as on a fresh CI machine.
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
}
for _name, _value in DEFAULTS.items():
    os.environ.setdefault(_name, _value)
