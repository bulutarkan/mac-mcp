"""Xcode build/test results and the Simulator adapter (#133)."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp_server import app_adapters_dev as dev
from mcp_server.app_adapter_core import AppAdapterError
from mcp_server.app_adapters import mac_app, normalize_app_name, owner_overview, supported_actions
from mcp_server.policy import resolve_risk

SETTINGS = SimpleNamespace(allow_shell=True, workdir="/tmp", max_output_chars=20000)


def _done(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


DESTINATIONS = """
\tAvailable destinations for the "Demo" scheme:
\t\t{ platform:macOS, arch:arm64, variant:Designed for [iPad,iPhone], id:0000-MAC, name:My Mac }
\t\t{ platform:iOS, arch:arm64, id:0000-PHONE, name:Tarkan's iPhone }
\t\t{ platform:iOS Simulator, id:dvtdevice-DVTiOSDeviceSimulatorPlaceholder-iphonesimulator:placeholder, name:Any }
\t\t{ platform:iOS Simulator, arch:arm64, id:IPAD-1, OS:26.3, name:iPad (A16) }
\t\t{ platform:iOS Simulator, arch:arm64, id:PHONE-1, OS:26.3, name:iPhone 17 }

\tIneligible destinations for the "Demo" scheme:
\t\t{ platform:visionOS Simulator, id:VISION-1, name:Apple Vision Pro }
"""


def _devices(*rows):
    return [{"udid": udid, "name": name, "state": state, "runtime": "iOS 26.3"} for udid, name, state in rows]


class RegistryAndPolicyTests(unittest.TestCase):
    def test_actions_and_aliases(self) -> None:
        self.assertEqual("Simulator", normalize_app_name("ios simulator"))
        self.assertEqual(("list_workspaces", "build", "test", "build_result"), supported_actions("Xcode"))
        self.assertIn("screenshot", supported_actions("Simulator"))

    def test_overview_groups(self) -> None:
        rows = {row["name"]: row for row in owner_overview()}
        self.assertEqual(["Build", "Test"], rows["Xcode"]["runs"])
        self.assertIn("Read a build's result", rows["Xcode"]["reads"])
        self.assertIn("Erase", rows["Simulator"]["changes"])
        self.assertIn("Screenshot", rows["Simulator"]["reads"])

    def test_risk(self) -> None:
        for action in ("build", "test"):
            _, risk = resolve_risk("mac_app", {"app": "Xcode", "action": action})
            self.assertIn("raw_execution", {cap.value for cap in risk.capabilities})
        for action in ("boot", "erase", "install_app", "launch_app"):
            _, risk = resolve_risk("mac_app", {"app": "Simulator", "action": action})
            self.assertIn("local_write", {cap.value for cap in risk.capabilities})
            self.assertFalse(risk.destructive)
        for action in ("build_result", "list_devices", "screenshot", "read_log"):
            _, risk = resolve_risk("mac_app", {"app": "Simulator", "action": action})
            self.assertEqual("read_only", risk.requested_access_mode.value)

    def test_build_needs_running_commands(self) -> None:
        result = mac_app(SimpleNamespace(allow_shell=False), app="Xcode", action="build", path="/tmp")
        self.assertEqual("SHELL_DISABLED", result["reason_code"])
        self.assertIn("build scripts", result["error"])

    def test_shortcut_gate_is_unchanged(self) -> None:
        with patch("mcp_server.app_adapters.more.owner_allowed_shortcuts", return_value=[]):
            result = mac_app(SimpleNamespace(allow_shell=False), app="Shortcuts", action="run_shortcut", title="x")
        self.assertEqual("SHELL_DISABLED", result["reason_code"])
        self.assertIn("shortcut", result["error"])


class ProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_project_kinds(self) -> None:
        (self.root / "A.xcodeproj").mkdir()
        self.assertEqual(["-project", str((self.root / "A.xcodeproj").resolve())], dev._project_args(str(self.root))[0])
        (self.root / "A.xcworkspace").mkdir()
        self.assertEqual("-workspace", dev._project_args(str(self.root))[0][0])
        package = self.root / "pkg"
        package.mkdir()
        (package / "Package.swift").write_text("// swift-tools-version:5.9")
        self.assertEqual(([], package.resolve()), dev._project_args(str(package)))

    def test_ambiguous_folder(self) -> None:
        (self.root / "A.xcodeproj").mkdir()
        (self.root / "B.xcodeproj").mkdir()
        with self.assertRaises(AppAdapterError) as caught:
            dev._project_args(str(self.root))
        self.assertEqual("XCODE_PROJECT_AMBIGUOUS", caught.exception.code)

    def test_scheme_choice(self) -> None:
        listing = _done(json.dumps({"project": {"schemes": ["Demo"]}}))
        with patch.object(dev, "_xcodebuild", return_value=listing):
            self.assertEqual("Demo", dev._scheme([], self.root, None))
            self.assertEqual("Demo", dev._scheme([], self.root, " Demo "))
            with self.assertRaises(AppAdapterError) as caught:
                dev._scheme([], self.root, "Other")
        self.assertEqual("XCODE_SCHEME_NOT_FOUND", caught.exception.code)
        two = _done(json.dumps({"workspace": {"schemes": ["A", "B"]}}))
        with patch.object(dev, "_xcodebuild", return_value=two):
            with self.assertRaises(AppAdapterError) as caught:
                dev._scheme([], self.root, None)
        self.assertEqual(["A", "B"], caught.exception.extra["schemes"])

    def test_destination_prefers_booted_then_mac_then_iphone_never_a_device(self) -> None:
        with patch.object(dev, "_xcodebuild", return_value=_done(DESTINATIONS)):
            with patch.object(dev, "_devices", return_value=_devices(("IPAD-1", "iPad (A16)", "Booted"))):
                self.assertEqual("platform=iOS Simulator,id=IPAD-1", dev._default_destination([], self.root, "Demo"))
            with patch.object(dev, "_devices", return_value=[]):
                # The Mac here is only "Designed for iPad", so an iPhone simulator is chosen, not the iPad.
                self.assertEqual("platform=iOS Simulator,id=PHONE-1", dev._default_destination([], self.root, "Demo"))
        mac = '{ platform:macOS, arch:arm64, id:0000-MAC, name:My Mac }'
        with patch.object(dev, "_xcodebuild", return_value=_done(mac)), patch.object(dev, "_devices", return_value=[]):
            self.assertEqual("platform=macOS,arch=arm64", dev._default_destination([], self.root, "Demo"))


class StartAndResultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        (self.state / "App.xcodeproj").mkdir()
        self.patches = [patch.object(dev, "_state_dir", return_value=self.state)]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in self.patches:
            item.stop()
        self.tmp.cleanup()

    def test_test_starts_a_job_with_only_testing(self) -> None:
        with patch.object(dev, "_scheme", return_value="Demo"), \
                patch("mcp_server.tools_jobs.start_background_job", return_value={"job_id": "job1"}) as job:
            result = dev.start(SETTINGS, "test", str(self.state / "App.xcodeproj"), scheme="Demo",
                               destination="platform=macOS", tests=["DemoTests/A/testB", " "])
        command = job.call_args.args[1]
        self.assertTrue(command.startswith("xcodebuild -project "))
        self.assertIn("-only-testing:DemoTests/A/testB test", command)
        self.assertEqual(600, job.call_args.kwargs["timeout_s"])
        self.assertEqual("job1", result["job_id"])
        record = json.loads((self.state / "job1.json").read_text())
        self.assertEqual("test", record["action"])
        self.assertTrue(record["result_bundle"].endswith(".xcresult"))

    def _record(self, action="build"):
        bundle = self.state / "b.xcresult"
        bundle.mkdir()
        (self.state / "job1.json").write_text(json.dumps({
            "job_id": "job1", "action": action, "project": ["-project", "x"], "cwd": str(self.state),
            "scheme": "Demo", "destination": "platform=macOS", "result_bundle": str(bundle)}))

    def test_running_job(self) -> None:
        self._record()
        with patch("mcp_server.tools_jobs.get_job_status", return_value={"status": "running"}):
            result = dev.result(SETTINGS, "job1")
        self.assertFalse(result["finished"])

    def test_failed_build_lists_errors_with_place(self) -> None:
        self._record()
        build = {"errorCount": 1, "warningCount": 0, "errors": [{
            "issueType": "Swift Compiler Error", "message": "Cannot find 'yok' in scope",
            "sourceURL": "file:///p/App%20X.swift#EndingColumnNumber=48&StartingLineNumber=14&StartingColumnNumber=45"}]}
        with patch("mcp_server.tools_jobs.get_job_status", return_value={"status": "failed", "exit_code": 65}), \
                patch.object(dev, "_bundle_json", return_value=build):
            result = dev.result(SETTINGS, "job1")
        self.assertFalse(result["succeeded"])
        self.assertEqual({"message": "Cannot find 'yok' in scope", "file": "/p/App X.swift", "line": 15, "column": 46,
                          "kind": "Swift Compiler Error"}, result["errors"][0])
        self.assertNotIn("products", result)

    def test_test_summary_and_failures(self) -> None:
        self._record("test")
        summary = {"result": "Failed", "totalTestCount": 2, "passedTests": 1, "failedTests": 1, "skippedTests": 0,
                   "testFailures": [{"testIdentifierString": "T/testB()", "targetName": "T", "failureText": "4 != 5"}]}
        answers = [{"errorCount": 0, "warningCount": 0}, summary]
        with patch("mcp_server.tools_jobs.get_job_status", return_value={"status": "failed", "exit_code": 65}), \
                patch.object(dev, "_bundle_json", side_effect=answers):
            result = dev.result(SETTINGS, "job1")
        self.assertEqual(1, result["tests"]["failed"])
        self.assertEqual({"test": "T/testB()", "target": "T", "message": "4 != 5"}, result["failures"][0])

    def test_success_lists_products(self) -> None:
        self._record()
        with patch("mcp_server.tools_jobs.get_job_status", return_value={"status": "completed", "exit_code": 0}), \
                patch.object(dev, "_bundle_json", return_value={"errorCount": 0}), \
                patch.object(dev, "_products", return_value=[{"app_path": "/x/Demo.app"}]):
            result = dev.result(SETTINGS, "job1")
        self.assertEqual("/x/Demo.app", result["products"][0]["app_path"])

    def test_no_bundle_falls_back_to_output(self) -> None:
        self._record()
        (self.state / "b.xcresult").rmdir()
        output = {"stderr": "xcodebuild: error: Unable to find a destination\nmore", "stdout": ""}
        with patch("mcp_server.tools_jobs.get_job_status", return_value={"status": "failed", "exit_code": 70}), \
                patch("mcp_server.tools_jobs.get_job_output", return_value=output):
            result = dev.result(SETTINGS, "job1")
        self.assertEqual(["xcodebuild: error: Unable to find a destination"], result["output"])

    def test_unknown_job(self) -> None:
        with self.assertRaises(AppAdapterError) as caught:
            dev.result(SETTINGS, "missing")
        self.assertEqual("XCODE_JOB_UNKNOWN", caught.exception.code)


class SimulatorTests(unittest.TestCase):
    def test_device_choice(self) -> None:
        rows = _devices(("A", "iPhone 17", "Shutdown"), ("B", "iPhone 17", "Booted"), ("C", "iPad", "Shutdown"))
        with patch.object(dev, "_devices", return_value=rows):
            self.assertEqual("B", dev._device(None)["udid"])
            self.assertEqual("B", dev._device("iphone 17")["udid"])  # the booted one of two
            self.assertEqual("C", dev._device("C")["udid"])
        with patch.object(dev, "_devices", return_value=rows[:1] + rows[2:]):
            with self.assertRaises(AppAdapterError) as caught:
                dev._device(None)
        self.assertEqual("SIMULATOR_REQUIRED", caught.exception.code)

    def test_erase_refuses_a_running_device(self) -> None:
        with patch.object(dev, "_devices", return_value=_devices(("A", "iPhone", "Booted"))), \
                patch.object(dev, "_xcrun") as run:
            with self.assertRaises(AppAdapterError) as caught:
                dev.erase("A")
        self.assertEqual("SIMULATOR_BOOTED", caught.exception.code)
        run.assert_not_called()

    def test_erase_needs_an_explicit_device(self) -> None:
        with self.assertRaises(AppAdapterError) as caught:
            dev.erase(None)
        self.assertEqual("SIMULATOR_REQUIRED", caught.exception.code)

    def test_launch_reads_the_pid(self) -> None:
        with patch.object(dev, "_devices", return_value=_devices(("A", "iPhone", "Booted"))), \
                patch.object(dev, "_xcrun", return_value=_done("dev.demo: 4242\n")) as run:
            result = mac_app(SETTINGS, app="Simulator", action="launch_app", target_app="dev.demo")
        self.assertEqual(4242, result["pid"])
        self.assertTrue(result["verified"])
        self.assertEqual(["simctl", "launch", "--terminate-running-process", "A", "dev.demo"], run.call_args.args[0])

    def test_read_log_rejects_predicate_injection(self) -> None:
        with patch.object(dev, "_devices", return_value=_devices(("A", "iPhone", "Booted"))):
            result = mac_app(SETTINGS, app="Simulator", action="read_log", query='x" OR 1')
        self.assertEqual("APP_ADAPTER_ARGUMENT_INVALID", result["reason_code"])

    def test_read_log_keeps_the_newest_lines(self) -> None:
        text = "Timestamp header\n" + "\n".join(f"line {i}" for i in range(250))
        with patch.object(dev, "_devices", return_value=_devices(("A", "iPhone", "Booted"))), \
                patch.object(dev, "_xcrun", return_value=_done(text)) as run:
            result = dev.read_log(None, "Demo", minutes=500)
        self.assertEqual("line 249", result["lines"][-1])
        self.assertTrue(result["truncated"])
        self.assertEqual(60, result["minutes"])
        self.assertIn('process == "Demo" OR subsystem == "Demo"', run.call_args.args[0])

    def test_install_reads_the_bundle_id(self) -> None:
        import plistlib

        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "Demo.app"
            app.mkdir()
            (app / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "dev.demo"}))
            with patch.object(dev, "_devices", return_value=_devices(("A", "iPhone", "Booted"))), \
                    patch.object(dev, "_xcrun", return_value=_done()):
                result = dev.install_app("A", str(app))
        self.assertEqual("dev.demo", result["bundle_id"])
        self.assertTrue(result["verified"])

    def test_screenshot_needs_a_booted_device(self) -> None:
        with patch.object(dev, "_devices", return_value=_devices(("A", "iPhone", "Shutdown"))):
            with self.assertRaises(AppAdapterError) as caught:
                dev.screenshot("A", None)
        self.assertEqual("SIMULATOR_NOT_BOOTED", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
