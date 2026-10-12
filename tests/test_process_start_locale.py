import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import managed_process, native_targets, tools_agents

TURKISH = {"LANG": "tr_TR.UTF-8", "LC_ALL": "tr_TR.UTF-8", "LC_TIME": "tr_TR.UTF-8"}
C = {"LANG": "C", "LC_ALL": "C", "LC_TIME": "C"}


class ProcessStartLocaleTests(unittest.TestCase):
    """Process start times identify a process; they must read the same in every locale."""

    def test_agent_process_start_time_parses_in_a_turkish_locale(self) -> None:
        with patch.dict(os.environ, TURKISH):
            state, started = tools_agents._process_state(os.getpid())
        self.assertIsNotNone(state)
        self.assertIsNotNone(started)

    def test_server_identity_does_not_depend_on_the_locale(self) -> None:
        with patch.dict(os.environ, TURKISH):
            turkish = managed_process.process_snapshot(os.getpid())
        with patch.dict(os.environ, C):
            plain = managed_process.process_snapshot(os.getpid())
        self.assertEqual(plain.start_time, turkish.start_time)

    def test_a_record_written_in_another_locale_still_validates(self) -> None:
        # Records written before start times were read in the C locale keep matching.
        with tempfile.TemporaryDirectory(prefix="mac-mcp-locale-record-") as td:
            record = Path(td) / "server.pid"
            with patch.object(managed_process, "matches_role", return_value=True):
                with patch.dict(os.environ, TURKISH), \
                        patch.object(managed_process, "_C_LOCALE", {}):
                    managed_process.write_process_record(record, "server", os.getpid())
                with patch.dict(os.environ, TURKISH):
                    result = managed_process.validate_process_record(record, "server")
        self.assertEqual("valid", result.status, result.reason)

    def test_app_instance_token_does_not_depend_on_the_locale(self) -> None:
        with patch.dict(os.environ, TURKISH):
            turkish = native_targets._process_instance_token(os.getpid())
        with patch.dict(os.environ, C):
            plain = native_targets._process_instance_token(os.getpid())
        self.assertTrue(plain)
        self.assertEqual(plain, turkish)


if __name__ == "__main__":
    unittest.main()
