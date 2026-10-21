"""#69: named workspaces are saved and opened again without taking the owner's focus.

mac_snapshot could read what is open and agents could open apps onto a desktop, but nothing kept a set
such as "Research" to open again. workspace saves apps (by bundle id), browser pages and Finder places,
only the parts asked for and never what Privacy excludes; restore starts what is missing in the
background, leaves running apps alone, opens onto a saved desktop when there is one, and reports what
is not installed or gone. Nothing here launches a real app.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import workspaces as ws
from mcp_server.change_summary import change_items_for_event
from mcp_server.policy import RISK_REGISTRY, resolve_risk

RUNNING = [{"name": "Safari", "bundle_id": "com.apple.Safari"}, {"name": "Code", "bundle_id": "com.microsoft.VSCode"},
           {"name": "Mac MCP", "bundle_id": "com.bulutarkan.mac-mcp.menu"}, {"name": "Finder", "bundle_id": "com.apple.finder"},
           {"name": "1Password", "bundle_id": "com.1password.1password"}]


class WorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mac-mcp-workspaces-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        env = patch.dict(os.environ, {"MAC_MCP_STATE_DIR": str(self.base / "state")})
        env.start()
        self.addCleanup(env.stop)
        privacy = patch.object(ws.privacy_policy, "excluded_app",
                               side_effect=lambda *names: "excluded" if "1Password" in names else None)
        sites = patch.object(ws.privacy_policy, "excluded_site",
                             side_effect=lambda url: "excluded" if "bank.example" in url else None)
        privacy.start()
        sites.start()
        self.addCleanup(privacy.stop)
        self.addCleanup(sites.stop)
        self.opens: list = []
        self.tabs: list = []

    def fake_open(self, args, **_kwargs):
        self.opens.append(args[1:])
        if "com.example.gone" in args:
            return subprocess.CompletedProcess(args, 1, "", "Unable to find application with identifier com.example.gone")
        return subprocess.CompletedProcess(args, 0, "", "")

    def open_tab(self, browser, url):
        self.tabs.append((browser, url))
        return {"ok": True}

    def test_save_reads_what_is_open_and_leaves_out_mac_mcp_finder_and_private_items(self) -> None:
        folder = self.base / "Project"
        folder.mkdir()
        with patch.object(ws, "running_apps", return_value=RUNNING), \
                patch.object(ws, "_current_tabs", return_value=[
                    {"browser": "Safari", "url": "https://docs.example/a"},
                    {"browser": "Google Chrome", "url": "https://bank.example/login"}]), \
                patch.object(ws, "_current_finder", return_value=[str(folder)]):
            saved = ws.save(None, "Research")
        profile = saved["workspace"]
        self.assertEqual(["Safari", "Code"], [app["name"] for app in profile["apps"]])
        self.assertEqual([{"browser": "Safari", "url": "https://docs.example/a"}], profile["urls"])
        self.assertEqual([str(folder)], profile["finder"])
        self.assertEqual(2, len(saved["skipped"]))
        self.assertNotIn("bank.example", (self.base / "state" / "workspaces.json").read_text())
        self.assertEqual(0o600, (self.base / "state" / "workspaces.json").stat().st_mode & 0o777)

    def test_only_chosen_parts_and_given_lists_are_saved(self) -> None:
        saved = ws.save(None, "Writing", include=["apps", "urls"], apps=["com.apple.TextEdit", "Notes"],
                        urls=["https://writing.example"], exclude_apps=["notes"], desktop=4)["workspace"]
        self.assertEqual([{"name": "com.apple.TextEdit", "bundle_id": "com.apple.TextEdit"}], saved["apps"])
        self.assertNotIn("finder", saved)
        self.assertEqual(4, saved["desktop"])
        with self.assertRaises(HTTPException):
            ws.save(None, "Bad", include=["clipboard"])
        with self.assertRaises(HTTPException):
            ws.save(None, "Bad", urls=["file:///etc/hosts"])
        with self.assertRaises(HTTPException):
            ws.save(None, "a/b", apps=[])

    def test_list_inspect_replace_and_delete(self) -> None:
        ws.save(None, "Research", include=["apps"], apps=["Safari"])
        again = ws.save(None, "research", include=["apps"], apps=["Notes"])
        self.assertEqual("replaced", again["action"])
        listing = ws.list_workspaces()
        self.assertEqual((1, "research", 1), (listing["count"], listing["workspaces"][0]["name"], listing["workspaces"][0]["apps"]))
        self.assertEqual("Notes", ws.inspect("RESEARCH")["workspace"]["apps"][0]["name"])
        self.assertEqual("research", ws.delete("Research")["deleted"])
        with self.assertRaises(HTTPException) as gone:
            ws.inspect("Research")
        self.assertEqual(404, gone.exception.status_code)

    def test_restore_opens_only_what_is_missing_in_the_background_and_reports_the_rest(self) -> None:
        folder = self.base / "Project"
        folder.mkdir()
        note = folder / "plan.txt"
        note.write_text("x")
        ws.save(None, "Research", apps=[{"name": "Safari", "bundle_id": "com.apple.Safari"},
                                        {"name": "Notes", "bundle_id": "com.apple.Notes"},
                                        {"name": "Gone", "bundle_id": "com.example.gone"}],
                urls=[{"browser": "Safari", "url": "https://docs.example/a"},
                      {"browser": "Google Chrome", "url": "https://chrome.example"}],
                finder_paths=[str(folder), str(note), str(self.base / "moved-away")])
        with patch.object(ws, "running_apps", return_value=[{"name": "Safari", "bundle_id": "com.apple.Safari"}]), \
                patch.object(ws.subprocess, "run", side_effect=self.fake_open):
            result = ws.restore(None, "Research", open_tab=self.open_tab, on_desktop=lambda *a: self.fail("no desktop"))
        self.assertEqual(["Safari"], result["already_open"])
        self.assertEqual([("Safari", "https://docs.example/a")], self.tabs)  # a background tab in the running Safari
        self.assertEqual([["-g", "-b", "com.apple.Notes"], ["-g", "-b", "com.example.gone"],
                          ["-g", "-b", "com.google.Chrome", "https://chrome.example"],
                          ["-g", str(folder)], ["-g", "-R", str(note)]], self.opens)
        self.assertTrue(all(args[0] == "-g" for args in self.opens))  # nothing comes to the front
        self.assertEqual({"Gone": "not installed", str(self.base / "moved-away"): "no longer there"},
                         {row["item"]: row["reason"] for row in result["missing"]})
        self.assertFalse(result["ok"])

    def test_restore_onto_a_desktop_uses_the_desktop_helper(self) -> None:
        ws.save(None, "Agent", apps=[{"name": "Notes", "bundle_id": "com.apple.Notes"}],
                urls=[{"browser": "Google Chrome", "url": "https://a.example"},
                      {"browser": "Google Chrome", "url": "https://b.example"}], finder_paths=[], desktop=7)
        calls = []
        with patch.object(ws, "running_apps", return_value=[{"name": "Notes", "bundle_id": "com.apple.Notes"}]), \
                patch.object(ws.subprocess, "run", side_effect=self.fake_open):
            result = ws.restore(None, "Agent", open_tab=self.open_tab,
                                on_desktop=lambda space, app, targets: calls.append((space, app, targets)) or {"ok": True})
        self.assertEqual([(7, "com.apple.Notes", []), (7, "com.google.Chrome", ["https://a.example"]),
                          (7, "com.google.Chrome", ["https://b.example"])], calls)
        self.assertEqual(([], 7, True), (self.opens, result["desktop"], result["ok"]))

    def test_policy_and_changes_list(self) -> None:
        self.assertIn("workspace", RISK_REGISTRY)
        self.assertFalse(resolve_risk("workspace", {"action": "list"})[1].destructive)
        self.assertTrue(resolve_risk("workspace", {"action": "delete"})[1].destructive)
        base = {"tool": "workspace", "status": "success", "event_id": "e", "timestamp": "t", "result": {}}
        self.assertEqual([], change_items_for_event({**base, "arguments": {"action": "inspect", "name": "R"}}))
        items = change_items_for_event({**base, "arguments": {"action": "restore", "name": "Research"}})
        self.assertEqual([("Opened workspace", "Research")], [(item["action"], item["target"]) for item in items])


if __name__ == "__main__":
    unittest.main()
