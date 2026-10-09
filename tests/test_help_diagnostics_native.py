from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import diagnostics, permission_probe as pp

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "menu_app" / "Sources"

HARNESS = r'''
import Foundation

@main
struct DoctorReportHarness {
    static func main() {
        let text = try! String(contentsOfFile: CommandLine.arguments[1], encoding: .utf8)
        guard let report = DoctorReport.parse(text) else {
            print("PARSE_FAILED"); exit(1)
        }
        precondition(report.ok == false)
        precondition(report.permissions.map(\.checkID) == [
            "permissions.accessibility", "permissions.screen_recording",
            "permissions.automation", "permissions.microphone",
        ])
        let ax = report.permissions[0]
        precondition(ax.details?.state == "denied")
        precondition(ax.details?.recovery?.action == "open_system_settings")
        precondition(ax.details?.recovery?.url?.hasPrefix("x-apple.systempreferences:") == true)
        precondition(ax.details?.features?.isEmpty == false)
        precondition(report.permissions[2].details?.targets?.contains(DoctorTarget(app: "Safari", state: "granted")) == true)
        let port = report.problems.first { $0.checkID == "process.server" }
        precondition(port?.details?.recovery?.pane == "advanced")
        // A row whose details use other shapes still decodes; only unreadable fields are dropped.
        let odd = report.checks.first { $0.checkID == "odd.shape" }
        precondition(odd != nil && odd?.details?.state == nil && odd?.summary == "Odd shape")
        precondition(report.passed.contains { $0.checkID == "odd.shape" })
        print("DOCTOR_REPORT_PASS")
    }
}
'''


def payload(ax: str, targets: list[dict]) -> dict:
    with patch.object(pp, "accessibility_state", return_value=ax), \
         patch.object(pp, "screen_recording_state", return_value="granted"), \
         patch.object(pp, "automation_state", side_effect=lambda bundle: next(
             (t["state"] for t in targets if t["bundle_id"] == bundle), pp.NOT_RUNNING)), \
         patch.object(pp, "process_identity", return_value={"pid": 1, "executable": "/x/Python", "listed_as": "Python"}):
        return pp.probe_permissions()


def doctor_json() -> str:
    with patch.object(diagnostics, "_server_permissions", return_value=payload(ax="denied", targets=[
            {"app": "Safari", "bundle_id": "com.apple.Safari", "state": "granted"}])):
        rows = diagnostics._permission_rows()
    rows.append(diagnostics.result(
        "process.server", "process", "fail", "SERVER_PORT_FOREIGN_LISTENER", "Port used by node.",
        remediation="Quit node.", details={"port": 8765, "foreign_listener_pids": [4242],
                                            "recovery": {"action": "open_settings", "pane": "advanced"}},
    ))
    rows.append(diagnostics.result(
        "odd.shape", "runtime", "info", "ODD", "Odd shape",
        details={"state": {"nested": True}, "targets": "not-a-list", "features": 3, "recovery": "bad"},
    ))
    report = diagnostics.build_report(rows)
    # The CLI may print around the object; parsing must find it.
    return "warning: something printed first\n" + json.dumps(report, indent=2) + "\n"


@unittest.skipUnless(shutil.which("xcrun"), "Swift toolchain is not installed")
class DoctorReportDecodingTests(unittest.TestCase):
    def test_real_doctor_json_decodes_with_permissions_and_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            report = Path(td) / "doctor.json"
            report.write_text(doctor_json(), encoding="utf-8")
            harness = Path(td) / "DoctorReportHarness.swift"
            harness.write_text(textwrap.dedent(HARNESS), encoding="utf-8")
            binary = Path(td) / "doctor-report"
            built = subprocess.run(
                ["xcrun", "swiftc", "-parse-as-library", str(SOURCES / "DoctorReport.swift"), str(harness),
                 "-o", str(binary)],
                capture_output=True, text=True, timeout=300,
            )
            self.assertEqual(0, built.returncode, built.stderr)
            run = subprocess.run([str(binary), str(report)], capture_output=True, text=True, timeout=20)
            self.assertEqual(0, run.returncode, run.stdout + run.stderr)
            self.assertIn("DOCTOR_REPORT_PASS", run.stdout)


class HelpPaneWiringTests(unittest.TestCase):
    def test_help_pane_is_reachable_and_never_sends_anything_by_itself(self) -> None:
        settings = (SOURCES / "SettingsView.swift").read_text(encoding="utf-8")
        help_view = (SOURCES / "HelpDiagnostics.swift").read_text(encoding="utf-8")
        search = (SOURCES / "SettingsSearch.swift").read_text(encoding="utf-8")
        build = (ROOT / "menu_app" / "build_app.sh").read_text(encoding="utf-8")
        self.assertIn("case .help: return \"Help & Diagnostics\"", settings)
        self.assertIn("HelpDiagnosticsPane(state: state, settings: settings, center: diagnostics)", settings)
        self.assertIn('"help": "help diagnostics doctor', search)
        for name in ("DoctorReport.swift", "HelpDiagnostics.swift"):
            self.assertIn(f'"${{SCRIPT_DIR}}/Sources/{name}"', build)
        # The only automatic work is running local diagnostics; links open on a click.
        task = help_view[help_view.index(".task {"):help_view.index(".sheet(item:")]
        self.assertNotIn("NSWorkspace", task)
        self.assertIn('["doctor", "--json", "--support-bundle", url.path]', help_view)
        self.assertIn("never includes .env values, settings values, logs, credentials", help_view)
        self.assertNotIn("URLSession", help_view)


if __name__ == "__main__":
    unittest.main()
