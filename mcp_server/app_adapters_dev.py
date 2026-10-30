"""Xcode builds and tests, and the iOS Simulator (#133).

build and test run xcodebuild as a background job (like start_background_job: running commands must be
allowed) with a result bundle Mac MCP keeps; build_result turns that bundle into the errors with their
file and line, the failing tests with their messages, and the built app. The Simulator is driven with
`xcrun simctl`: list, boot, shut down, erase, install, launch, stop, a screenshot with its text read on
this Mac, and the device log. Nothing here deletes DerivedData or edits the project.
"""
from __future__ import annotations

import json
import plistlib
import re
import shlex
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

from .app_adapter_core import AppAdapterError, _run_js

_RESULTS_KEPT = 20
_BUILD_TIMEOUT_S = 600  # the most a background job may run
_NO_OUTPUT_TIMEOUT_S = 300
_MAX_ISSUES = 30
_MAX_LOG_LINES = 200


def _state_dir(name: str) -> Path:
    folder = Path.home() / ".mac-mcp" / name
    folder.mkdir(parents=True, exist_ok=True)
    try:
        folder.chmod(0o700)
    except OSError:
        pass
    return folder


def _xcrun(args: List[str], *, timeout_s: float, code: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["xcrun", *args], capture_output=True, text=True, timeout=timeout_s)
    except FileNotFoundError as exc:
        raise AppAdapterError("XCODE_NOT_INSTALLED", "Xcode's command line tools are not installed.",
                              retryable=False) from exc
    except subprocess.TimeoutExpired as exc:
        raise AppAdapterError(code, f"{args[0]} did not finish within {timeout_s:g}s.", retryable=True) from exc


def _failed(proc: subprocess.CompletedProcess, code: str) -> AppAdapterError:
    detail = (proc.stderr or proc.stdout or "failed").strip().splitlines()
    return AppAdapterError(code, " ".join(detail[-3:])[:800], retryable=False)


# Xcode: build and test ---------------------------------------------------------------------------

def _project_args(path: str) -> Tuple[List[str], Path]:
    """xcodebuild's project arguments and working folder for a project, workspace or package path."""
    target = Path(path).expanduser().resolve(strict=True)
    if target.suffix == ".xcworkspace":
        return ["-workspace", str(target)], target.parent
    if target.suffix == ".xcodeproj":
        return ["-project", str(target)], target.parent
    if target.is_dir():
        if (target / "Package.swift").is_file():
            return [], target
        # A folder holding one workspace (preferred: it includes the projects) or one project.
        for suffix in (".xcworkspace", ".xcodeproj"):
            found = sorted(item for item in target.glob(f"*{suffix}") if item.is_dir())
            if len(found) == 1:
                return _project_args(str(found[0]))
            if len(found) > 1:
                raise AppAdapterError("XCODE_PROJECT_AMBIGUOUS", f"{target} holds several {suffix} files; pass "
                                      "path = the one to build.", retryable=False,
                                      candidates=[item.name for item in found])
    raise AppAdapterError("XCODE_PROJECT_INVALID", "path is an .xcodeproj, an .xcworkspace or a folder with one "
                          "of them or a Package.swift.", retryable=False)


def _xcodebuild(args: List[str], cwd: Path, *, timeout_s: float, code: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["xcodebuild", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout_s)
    except FileNotFoundError as exc:
        raise AppAdapterError("XCODE_NOT_INSTALLED", "xcodebuild is not installed.", retryable=False) from exc
    except subprocess.TimeoutExpired as exc:
        raise AppAdapterError(code, f"xcodebuild did not answer within {timeout_s:g}s.", retryable=True) from exc


def _scheme(project: List[str], cwd: Path, wanted: Optional[str]) -> str:
    proc = _xcodebuild([*project, "-list", "-json"], cwd, timeout_s=60, code="XCODE_LIST_FAILED")
    try:
        info = json.loads(proc.stdout or "{}")
    except ValueError:
        raise _failed(proc, "XCODE_LIST_FAILED") from None
    body = info.get("workspace") or info.get("project") or {}
    schemes = list(body.get("schemes") or [])
    name = str(wanted or "").strip()
    if name:
        if name in schemes:
            return name
        raise AppAdapterError("XCODE_SCHEME_NOT_FOUND", f"The project has no scheme named {name}.",
                              retryable=False, schemes=schemes)
    if len(schemes) == 1:
        return schemes[0]
    raise AppAdapterError("XCODE_SCHEME_REQUIRED", "Pass item_id = the scheme to build." if schemes else
                          "The project has no shared scheme.", retryable=False, schemes=schemes)


_DESTINATION = re.compile(r"\{\s*(.*?)\s*\}")


def _destinations(project: List[str], cwd: Path, scheme: str) -> List[Dict[str, str]]:
    proc = _xcodebuild([*project, "-scheme", scheme, "-showdestinations"], cwd, timeout_s=90,
                       code="XCODE_DESTINATIONS_FAILED")
    available = (proc.stdout or "").split("Ineligible destinations")[0]
    rows = []
    for match in _DESTINATION.finditer(available):
        fields = {}
        for part in match.group(1).split(", "):
            key, _, value = part.partition(":")
            fields[key.strip()] = value.strip()
        if fields.get("platform") and "placeholder" not in fields.get("id", ""):
            rows.append(fields)
    return rows


def _default_destination(project: List[str], cwd: Path, scheme: str) -> str:
    """A booted simulator, else this Mac, else an iPhone simulator; never a connected device."""
    rows = _destinations(project, cwd, scheme)
    sims = [row for row in rows if row.get("platform", "").endswith("Simulator") and row.get("id")]
    booted = {device["udid"] for device in _devices() if device["state"] == "Booted"}
    for row in sims:
        if row["id"] in booted:
            return f"platform={row['platform']},id={row['id']}"
    for row in rows:
        if row.get("platform") == "macOS" and not row.get("variant"):
            return "platform=macOS" + (f",arch={row['arch']}" if row.get("arch") else "")
    phones = [row for row in sims if row.get("name", "").startswith("iPhone")] or sims
    if phones:
        return f"platform={phones[0]['platform']},id={phones[0]['id']}"
    raise AppAdapterError("XCODE_DESTINATION_REQUIRED", "No simulator or Mac destination was found; pass "
                          "destination (xcodebuild -destination syntax).", retryable=False,
                          destinations=[row.get("name") for row in rows])


def _prune_results(folder: Path) -> None:
    records = sorted(folder.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
    for record in records[_RESULTS_KEPT:]:
        try:
            bundle = Path(json.loads(record.read_text()).get("result_bundle") or "")
            if bundle.parent == folder and bundle.suffix == ".xcresult":
                shutil.rmtree(bundle, ignore_errors=True)
        except (OSError, ValueError):
            pass
        record.unlink(missing_ok=True)


def start(settings: Any, action: str, path: str, *, scheme: Optional[str], destination: Optional[str],
          tests: Optional[List[str]]) -> Dict[str, Any]:
    """Start xcodebuild build or test as a background job; build_result reads its outcome."""
    from . import tools_jobs

    if not getattr(settings, "allow_shell", False):
        raise AppAdapterError("SHELL_DISABLED", "Building runs the project's build scripts, so it needs running "
                              "commands to be allowed (MCP_ALLOW_SHELL).", retryable=False)
    if not path:
        raise AppAdapterError("XCODE_PROJECT_REQUIRED", "Pass path = the .xcodeproj, .xcworkspace or package "
                              "folder (Xcode had no open project to default to).", retryable=False)
    project, cwd = _project_args(path)
    chosen = _scheme(project, cwd, scheme)
    where = str(destination or "").strip() or _default_destination(project, cwd, chosen)
    folder = _state_dir("xcode")
    _prune_results(folder)
    bundle = folder / f"{uuid.uuid4().hex[:12]}.xcresult"
    argv = ["xcodebuild", *project, "-scheme", chosen, "-destination", where, "-resultBundlePath", str(bundle)]
    if action == "test":
        argv.extend(f"-only-testing:{item.strip()}" for item in tests or [] if item and item.strip())
    argv.append(action)
    job = tools_jobs.start_background_job(settings, shlex.join(argv), cwd=str(cwd), timeout_s=_BUILD_TIMEOUT_S,
                                          no_output_timeout_s=_NO_OUTPUT_TIMEOUT_S)
    record = {"job_id": job["job_id"], "action": action, "project": project, "cwd": str(cwd), "scheme": chosen,
              "destination": where, "result_bundle": str(bundle), "started_at": time.time()}
    (folder / f"{job['job_id']}.json").write_text(json.dumps(record))
    return {"ok": True, "started": True, "job_id": job["job_id"], "action": action, "scheme": chosen,
            "destination": where, "cwd": str(cwd),
            "note": f"Wait with wait_jobs(job_ids=['{job['job_id']}']), then build_result (item_id = job_id) gives "
                    "the errors, failing tests and the built app. A build may take at most "
                    f"{_BUILD_TIMEOUT_S // 60} minutes."}


def _location(url: Optional[str]) -> Dict[str, Any]:
    """file:///a/B.swift#StartingLineNumber=11&StartingColumnNumber=4... -> file, line, column (1-based)."""
    if not url:
        return {}
    parsed = urlparse(url)
    where: Dict[str, Any] = {"file": unquote(parsed.path)} if parsed.path else {}
    marks = dict(part.split("=", 1) for part in parsed.fragment.split("&") if "=" in part)
    for key, name in (("StartingLineNumber", "line"), ("StartingColumnNumber", "column")):
        if str(marks.get(key, "")).isdigit():
            where[name] = int(marks[key]) + 1  # the bundle counts from 0
    return where


def _issues(items: Any) -> List[Dict[str, Any]]:
    out = []
    for item in (items or [])[:_MAX_ISSUES]:
        entry = {"message": item.get("message"), **_location(item.get("sourceURL"))}
        if item.get("issueType"):
            entry["kind"] = item["issueType"]
        if item.get("targetName"):
            entry["target"] = item["targetName"]
        out.append(entry)
    return out


def _bundle_json(args: List[str]) -> Optional[Dict[str, Any]]:
    proc = _xcrun(["xcresulttool", "get", *args, "--compact"], timeout_s=60, code="XCODE_RESULT_FAILED")
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout or "{}")
    except ValueError:
        return None


def _products(record: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The apps this build made (path and bundle id), for the Simulator's install_app and launch_app."""
    proc = _xcodebuild([*record["project"], "-scheme", record["scheme"], "-destination", record["destination"],
                        "-showBuildSettings", "-json"], Path(record["cwd"]), timeout_s=90,
                       code="XCODE_SETTINGS_FAILED")
    try:
        targets = json.loads(proc.stdout or "[]")
    except ValueError:
        return []
    apps = []
    for target in targets:
        values = target.get("buildSettings") or {}
        if values.get("WRAPPER_EXTENSION") != "app":
            continue
        app = Path(values.get("TARGET_BUILD_DIR", "")) / values.get("FULL_PRODUCT_NAME", "")
        apps.append({"target": target.get("target"), "app_path": str(app), "exists": app.exists(),
                     "bundle_id": values.get("PRODUCT_BUNDLE_IDENTIFIER"), "platform": values.get("PLATFORM_NAME")})
    return apps


def _tail_errors(settings: Any, job_id: str) -> List[str]:
    from . import tools_jobs

    output = tools_jobs.get_job_output(settings, job_id, tail_lines=80)
    lines = "\n".join(str(output.get(name) or "") for name in ("stderr", "stdout")).splitlines()
    picked = [line.strip() for line in lines if "error:" in line or "** BUILD FAILED" in line or "** TEST FAILED" in line]
    return (picked or [line.strip() for line in lines if line.strip()][-15:])[:_MAX_ISSUES]


def result(settings: Any, job_id: Optional[str]) -> Dict[str, Any]:
    from fastapi import HTTPException

    from . import tools_jobs

    wanted = str(job_id or "").strip()
    if not wanted or "/" in wanted:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "item_id is the job_id that build or test returned.",
                              retryable=False)
    record_path = _state_dir("xcode") / f"{wanted}.json"
    if not record_path.is_file():
        raise AppAdapterError("XCODE_JOB_UNKNOWN", "No build or test with that job_id is kept (the last "
                              f"{_RESULTS_KEPT} are).", retryable=False)
    record = json.loads(record_path.read_text())
    try:
        status = tools_jobs.get_job_status(settings, wanted)
    except HTTPException as exc:
        raise AppAdapterError("XCODE_JOB_UNKNOWN", str(exc.detail), retryable=False) from exc
    base = {"ok": True, "job_id": wanted, "action": record["action"], "scheme": record["scheme"],
            "destination": record["destination"], "status": status.get("status"), "exit_code": status.get("exit_code")}
    if status.get("status") in {"starting", "running", "stopping"}:
        return {**base, "finished": False,
                "note": f"Still running; wait_jobs(job_ids=['{wanted}']) and ask again."}
    bundle = Path(record["result_bundle"])
    out: Dict[str, Any] = {**base, "finished": True, "succeeded": status.get("exit_code") == 0}
    build = _bundle_json(["build-results", "--path", str(bundle)]) if bundle.exists() else None
    if build:
        out.update(error_count=build.get("errorCount", 0), warning_count=build.get("warningCount", 0),
                   errors=_issues(build.get("errors")), warnings=_issues(build.get("warnings")))
    if record["action"] == "test" and bundle.exists():
        summary = _bundle_json(["test-results", "summary", "--path", str(bundle)])
        if summary:
            out["tests"] = {key: summary.get(name) for key, name in (
                ("result", "result"), ("total", "totalTestCount"), ("passed", "passedTests"),
                ("failed", "failedTests"), ("skipped", "skippedTests"), ("expected_failures", "expectedFailures"))}
            out["failures"] = [{"test": item.get("testIdentifierString") or item.get("testName"),
                                "target": item.get("targetName"), "message": item.get("failureText")}
                               for item in (summary.get("testFailures") or [])[:_MAX_ISSUES]]
    if not build and not out["succeeded"]:
        # xcodebuild stopped before writing a result bundle (a wrong scheme or destination): its own words.
        out["output"] = _tail_errors(settings, wanted)
    if out["succeeded"] and record["action"] == "build":
        out["products"] = _products(record)
    out["result_bundle"] = str(bundle) if bundle.exists() else None
    return out


# Simulator ---------------------------------------------------------------------------------------

def _runtime_name(identifier: str) -> str:
    # com.apple.CoreSimulator.SimRuntime.iOS-26-3 -> iOS 26.3
    tail = identifier.rsplit(".", 1)[-1]
    system, _, version = tail.partition("-")
    return f"{system} {version.replace('-', '.')}".strip()


def _devices() -> List[Dict[str, Any]]:
    proc = _xcrun(["simctl", "list", "devices", "available", "-j"], timeout_s=30, code="SIMULATOR_LIST_FAILED")
    if proc.returncode != 0:
        raise _failed(proc, "SIMULATOR_LIST_FAILED")
    devices = []
    for runtime, items in (json.loads(proc.stdout or "{}").get("devices") or {}).items():
        for item in items:
            devices.append({"udid": item.get("udid"), "name": item.get("name"), "state": item.get("state"),
                            "runtime": _runtime_name(runtime)})
    return devices


def list_devices(query: Optional[str], *, limit: int) -> Dict[str, Any]:
    needle = str(query or "").strip().lower()
    devices = [device for device in _devices() if not needle or needle in f"{device['name']} {device['runtime']}".lower()]
    devices.sort(key=lambda device: (device["state"] != "Booted", device["runtime"], device["name"]))
    return {"ok": True, "count": len(devices[:limit]), "matched": len(devices), "devices": devices[:limit],
            "truncated": len(devices) > limit,
            "note": "Use a device's udid as item_id; a booted device is used when item_id is left out."}


def _device(item_id: Optional[str]) -> Dict[str, Any]:
    """A device by udid or name; without one, the only booted device."""
    devices = _devices()
    wanted = str(item_id or "").strip()
    if wanted:
        matches = [device for device in devices if device["udid"] == wanted] or \
                  [device for device in devices if device["name"].lower() == wanted.lower()]
        booted = [device for device in matches if device["state"] == "Booted"]
        if len(booted) == 1 or len(matches) == 1:
            return (booted or matches)[0]
        if not matches:
            raise AppAdapterError("SIMULATOR_NOT_FOUND", f"No simulator is named {wanted}; list_devices lists them.",
                                  retryable=False)
        raise AppAdapterError("SIMULATOR_AMBIGUOUS", f"Several simulators are named {wanted}; pass its udid.",
                              retryable=False, candidates=[f"{d['udid']} ({d['runtime']})" for d in matches])
    booted = [device for device in devices if device["state"] == "Booted"]
    if len(booted) == 1:
        return booted[0]
    raise AppAdapterError("SIMULATOR_REQUIRED", "Pass item_id = a simulator's udid or name (list_devices); "
                          + ("several are booted." if booted else "none is booted."), retryable=False)


def _state(udid: str) -> str:
    return next((device["state"] for device in _devices() if device["udid"] == udid), "Unknown")


def _simctl(args: List[str], *, timeout_s: float = 60, code: str) -> subprocess.CompletedProcess:
    proc = _xcrun(["simctl", *args], timeout_s=timeout_s, code=code)
    if proc.returncode != 0:
        raise _failed(proc, code)
    return proc


def boot(item_id: Optional[str]) -> Dict[str, Any]:
    device = _device(item_id)
    if device["state"] == "Booted":
        return {"ok": True, "changed": False, "verified": True, "device": device}
    _simctl(["boot", device["udid"]], code="SIMULATOR_BOOT_FAILED")
    # Without the Simulator app the device runs headless; screenshot and read_log still work.
    ready = _xcrun(["simctl", "bootstatus", device["udid"], "-b"], timeout_s=120, code="SIMULATOR_BOOT_FAILED")
    state = _state(device["udid"])
    return {"ok": True, "changed": True, "verified": state == "Booted" and ready.returncode == 0,
            "device": {**device, "state": state}, "reverse_action": "shutdown",
            "note": "The device runs without a window; open_app Simulator shows it."}


def shutdown(item_id: Optional[str]) -> Dict[str, Any]:
    device = _device(item_id)
    if device["state"] != "Booted":
        return {"ok": True, "changed": False, "verified": True, "device": device}
    _simctl(["shutdown", device["udid"]], code="SIMULATOR_SHUTDOWN_FAILED")
    state = _state(device["udid"])
    return {"ok": True, "changed": True, "verified": state == "Shutdown", "device": {**device, "state": state},
            "reverse_action": "boot"}


def erase(item_id: Optional[str]) -> Dict[str, Any]:
    """Factory-reset a shut-down simulator: its apps and data go; nothing on the Mac is touched."""
    if not str(item_id or "").strip():
        raise AppAdapterError("SIMULATOR_REQUIRED", "erase needs item_id = the simulator's udid.", retryable=False)
    device = _device(item_id)
    if device["state"] != "Shutdown":
        raise AppAdapterError("SIMULATOR_BOOTED", "Shut the simulator down first (shutdown); a running device is "
                              "never erased.", retryable=False)
    _simctl(["erase", device["udid"]], code="SIMULATOR_ERASE_FAILED")
    return {"ok": True, "changed": True, "verified": _state(device["udid"]) == "Shutdown", "device": device,
            "undoable": False}


def _bundle_id_of(app: Path) -> str:
    try:
        with (app / "Info.plist").open("rb") as handle:
            return str(plistlib.load(handle)["CFBundleIdentifier"])
    except (OSError, KeyError, plistlib.InvalidFileException) as exc:
        raise AppAdapterError("SIMULATOR_APP_INVALID", "path is a built .app for the simulator (build_result "
                              "products).", retryable=False) from exc


def install_app(item_id: Optional[str], path: Optional[str]) -> Dict[str, Any]:
    app = Path(str(path or "")).expanduser().resolve(strict=True)
    if app.suffix != ".app" or not app.is_dir():
        raise AppAdapterError("SIMULATOR_APP_INVALID", "path is a built .app for the simulator (build_result "
                              "products).", retryable=False)
    bundle_id = _bundle_id_of(app)
    device = _device(item_id)
    if device["state"] != "Booted":
        raise AppAdapterError("SIMULATOR_NOT_BOOTED", "Boot the simulator first (boot).", retryable=False)
    _simctl(["install", device["udid"], str(app)], timeout_s=120, code="SIMULATOR_INSTALL_FAILED")
    container = _xcrun(["simctl", "get_app_container", device["udid"], bundle_id], timeout_s=30,
                       code="SIMULATOR_INSTALL_FAILED")
    return {"ok": True, "changed": True, "verified": container.returncode == 0, "bundle_id": bundle_id,
            "device": device, "note": f"Start it with launch_app (target_app = {bundle_id})."}


def _bundle_arg(target_app: Optional[str]) -> str:
    text = str(target_app or "").strip()
    if not text:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "target_app is the app's bundle id.", retryable=False)
    return text


def launch_app(item_id: Optional[str], target_app: Optional[str]) -> Dict[str, Any]:
    bundle_id = _bundle_arg(target_app)
    device = _device(item_id)
    proc = _simctl(["launch", "--terminate-running-process", device["udid"], bundle_id], timeout_s=60,
                   code="SIMULATOR_LAUNCH_FAILED")
    match = re.search(r":\s*(\d+)\s*$", proc.stdout.strip())
    return {"ok": True, "changed": True, "verified": bool(match), "bundle_id": bundle_id,
            "pid": int(match.group(1)) if match else None, "device": device, "reverse_action": "terminate_app",
            "note": "screenshot shows what it draws (with its text); read_log reads its log."}


def terminate_app(item_id: Optional[str], target_app: Optional[str]) -> Dict[str, Any]:
    bundle_id = _bundle_arg(target_app)
    device = _device(item_id)
    proc = _xcrun(["simctl", "terminate", device["udid"], bundle_id], timeout_s=30, code="SIMULATOR_TERMINATE_FAILED")
    return {"ok": True, "changed": proc.returncode == 0, "verified": True, "bundle_id": bundle_id, "device": device,
            **({} if proc.returncode == 0 else {"note": "It was not running."})}


def _ocr(image: Path) -> Optional[str]:
    from .knowledge import OCR_JS

    script = OCR_JS + """
function run(argv) {
  var picture = $.NSImage.alloc.initWithContentsOfFile(argv[0]);
  if (!picture || picture.isNil()) return JSON.stringify({text: null});
  return JSON.stringify({text: ocr(cgOf(picture, picture.size.width, picture.size.height))});
}
"""
    try:
        return _run_js(script, [str(image)], timeout_s=30, code="SIMULATOR_OCR_FAILED").get("text")
    except AppAdapterError:
        return None


def screenshot(item_id: Optional[str], path: Optional[str]) -> Dict[str, Any]:
    """The device's screen as a PNG, with the text on it read by on-device OCR."""
    device = _device(item_id)
    if device["state"] != "Booted":
        raise AppAdapterError("SIMULATOR_NOT_BOOTED", "Boot the simulator first (boot).", retryable=False)
    if path:
        target = Path(str(path)).expanduser()
        if target.suffix.lower() != ".png":
            target = target / f"{device['name']} {time.strftime('%Y-%m-%d %H.%M.%S')}.png"
    else:
        target = _state_dir("cache/simulator") / f"{device['udid']}-{int(time.time() * 1000)}.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    _simctl(["io", device["udid"], "screenshot", "--type=png", str(target)], timeout_s=60,
            code="SIMULATOR_SCREENSHOT_FAILED")
    text = _ocr(target)
    return {"ok": True, "path": str(target), "device": device,
            "text": text, "lines": [line for line in (text or "").splitlines() if line.strip()],
            "note": "read_file on path shows the picture; text was recognised on this Mac and may hold errors."}


def read_log(item_id: Optional[str], query: Optional[str], *, minutes: Optional[int]) -> Dict[str, Any]:
    """The device log of the last minutes, for one process (query) or all of it, newest lines last."""
    device = _device(item_id)
    if device["state"] != "Booted":
        raise AppAdapterError("SIMULATOR_NOT_BOOTED", "Boot the simulator first (boot).", retryable=False)
    span = max(1, min(int(minutes or 2), 60))
    args = ["simctl", "spawn", device["udid"], "log", "show", "--last", f"{span}m", "--style", "compact"]
    process = str(query or "").strip()
    if process:
        if not re.fullmatch(r"[\w .\-]+", process):
            raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "query is a process name (letters, digits, "
                                  "spaces, dots, dashes).", retryable=False)
        args.extend(["--predicate", f'process == "{process}" OR subsystem == "{process}"'])
    proc = _xcrun(args, timeout_s=60, code="SIMULATOR_LOG_FAILED")
    if proc.returncode != 0:
        raise _failed(proc, "SIMULATOR_LOG_FAILED")
    lines = [line for line in (proc.stdout or "").splitlines() if line.strip() and not line.startswith("Timestamp")]
    return {"ok": True, "device": device, "minutes": span, "process": process or None,
            "line_count": len(lines), "lines": lines[-_MAX_LOG_LINES:], "truncated": len(lines) > _MAX_LOG_LINES}
