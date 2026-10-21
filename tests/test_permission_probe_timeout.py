"""An app that does not answer never holds up the permission check (doctor, dashboard, onboarding).

macOS's automation check waited as long as a running but stuck app (here Google Chrome, mid-restart)
took to answer; doctor_checks, and with it a full test run, hung for good.
"""
from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from mcp_server import permission_probe


class PermissionProbeTimeoutTests(unittest.TestCase):
    def test_a_stuck_app_is_reported_as_not_answering_and_the_rest_still_come_back(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)
        calls: list = []

        def state(bundle_id: str) -> str:
            calls.append(bundle_id)
            if bundle_id == "com.google.Chrome":
                release.wait(30)
            return permission_probe.GRANTED

        with patch.object(permission_probe, "automation_state", side_effect=state), \
             patch.object(permission_probe, "AUTOMATION_PROBE_TIMEOUT_S", 0.5):
            started = time.monotonic()
            first = permission_probe.probe_permissions()
            self.assertLess(time.monotonic() - started, 2.0)
            rows = {row["bundle_id"]: row for row in first["permissions"]["automation"]["targets"]}
            self.assertEqual((permission_probe.UNKNOWN, True),
                             (rows["com.google.Chrome"]["state"], rows["com.google.Chrome"]["not_answering"]))
            self.assertEqual(permission_probe.GRANTED, rows["com.apple.systemevents"]["state"])
            self.assertEqual(permission_probe.GRANTED, first["permissions"]["automation"]["state"])
            # Asked again while Chrome is still stuck: no second check is started for it.
            permission_probe.probe_permissions()
            self.assertEqual(1, calls.count("com.google.Chrome"))
            release.set()
            permission_probe._PROBES["com.google.Chrome"].join(5)
            again = permission_probe.probe_permissions()
        rows = {row["bundle_id"]: row for row in again["permissions"]["automation"]["targets"]}
        self.assertNotIn("not_answering", rows["com.google.Chrome"])


if __name__ == "__main__":
    unittest.main()
