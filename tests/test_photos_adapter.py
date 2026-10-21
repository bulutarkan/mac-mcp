"""#45: the Photos library through Mac MCP Photos (PhotoKit), read-only.

Photos had no adapter: mac_app app=Photos fell back to the generic path. Now list_albums, find_photos and
export_photos go through a small helper the owner allows once in Privacy & Security › Photos. The Photos
app is never opened and the library never changed; items say has_location, never where. A fake library
stands in for the helper here; the helper itself ran against the real library in the E2E check.
"""
from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp_server import photos_reader
from mcp_server.app_adapters import mac_app, support_matrix
from mcp_server.policy import Capability, resolve_risk

SETTINGS = SimpleNamespace()
ITEMS = [
    {"id": "A/L0/001", "media_type": "photo", "created": "2026-10-20T10:00:00+03:00", "favorite": True,
     "has_location": True, "filename": "IMG_0001.HEIC", "width": 4032, "height": 3024},
    {"id": "B/L0/001", "media_type": "video", "created": "2026-10-01T09:00:00+03:00", "favorite": False,
     "has_location": False, "filename": "IMG_0002.MOV", "width": 1920, "height": 1080, "duration_s": 12.5},
]


class FakeLibrary:
    def __init__(self, access="granted"):
        self.access = access
        self.requests: list[dict] = []

    def __call__(self, request, timeout_s):
        self.requests.append(dict(request))
        if self.access != "granted" and request["action"] != "access":
            return {"ok": False, "error": "photos_access", "message": "no"}
        action = request["action"]
        if action == "access":
            return {"ok": True, "access": self.access}
        if action == "list_albums":
            return {"ok": True, "count": 1, "truncated": False, "access": "granted",
                    "albums": [{"id": "ALB/1", "title": "Tatil", "kind": "album", "count": 2}]}
        if action == "find_photos":
            if request.get("album") == "Yok":
                return {"ok": False, "error": "album_not_found", "message": "No album has that id or name."}
            rows = [item for item in ITEMS if request.get("media_type") in (None, item["media_type"])]
            if request.get("favorite") is not None:
                rows = [item for item in rows if item["favorite"] == request["favorite"]]
            limit = request.get("limit", 20)
            return {"ok": True, "count": len(rows[:limit]), "total": len(rows), "items": rows[:limit],
                    "truncated": len(rows) > limit, "access": "granted"}
        if action == "export":
            out = []
            for item_id in request["ids"]:
                if item_id.startswith("missing"):
                    out.append({"id": item_id, "ok": False, "error": "not_found"})
                    continue
                path = Path(request["folder"]) / f"{item_id.replace('/', '_')}.HEIC"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"heic-bytes")
                out.append({"id": item_id, "ok": True, "path": str(path)})
            return {"ok": True, "items": out, "access": "granted"}
        raise AssertionError(action)


class PhotosAdapterTests(unittest.TestCase):
    def call(self, library, **kwargs):
        with patch.object(photos_reader, "_RUNNER", library), \
                patch.object(photos_reader, "helper_app", return_value=Path("/tmp/Mac MCP Photos.app")), \
                patch("mcp_server.privacy_policy.check_app", return_value=None):
            return mac_app(SETTINGS, app="Photos", **kwargs)

    def test_capabilities_say_photos_is_built_in_and_whether_access_is_given(self) -> None:
        caps = self.call(FakeLibrary(access="not_asked"), action="capabilities")
        self.assertEqual(["list_albums", "find_photos", "export_photos"], caps["actions"])
        self.assertEqual("not_asked", caps["access"]["state"])
        self.assertEqual("partial_first_party", support_matrix()["Photos"]["support"])

    def test_albums_and_filters_go_to_the_helper(self) -> None:
        library = FakeLibrary()
        albums = self.call(library, action="list_albums")
        self.assertEqual("Tatil", albums["albums"][0]["title"])
        found = self.call(library, action="find_photos", date_from="2026-10-01", date_to="2026-10-21",
                          media_type="photo", favorite=True, album="Tatil", query="IMG", limit=5)
        self.assertEqual(["A/L0/001"], [item["id"] for item in found["items"]])
        sent = library.requests[-1]
        self.assertEqual(datetime(2026, 10, 1).timestamp(), sent["from"])
        self.assertEqual(datetime(2026, 10, 21, 23, 59, 59).timestamp(), sent["to"])
        self.assertEqual(("photo", True, "Tatil", "IMG", 5),
                         (sent["media_type"], sent["favorite"], sent["album"], sent["name"], sent["limit"]))
        self.assertTrue(found["read_only"])
        self.assertNotIn("latitude", str(found))

    def test_no_access_and_unknown_albums_say_why(self) -> None:
        blocked = self.call(FakeLibrary(access="denied"), action="find_photos")
        self.assertFalse(blocked["ok"])
        self.assertEqual("PHOTOS_ACCESS_REQUIRED", blocked["reason_code"])
        self.assertIn("Do not work around it", blocked["error"] if "error" in blocked else str(blocked))
        unknown = self.call(FakeLibrary(), action="find_photos", album="Yok")
        self.assertEqual("PHOTOS_ALBUM_NOT_FOUND", unknown["reason_code"])
        missing = FakeLibrary()
        missing.__class__ = type("NoLibrary", (FakeLibrary,), {"__call__": lambda self, request, timeout_s: {
            "ok": False, "error": "library_unavailable", "message": "No Photos library is on this Mac."}})
        gone = self.call(missing, action="list_albums")
        self.assertEqual("PHOTOS_LIBRARY_UNAVAILABLE", gone["reason_code"])
        bad = self.call(FakeLibrary(), action="find_photos", media_type="audio")
        self.assertEqual("APP_ADAPTER_ARGUMENT_INVALID", bad["reason_code"])

    def test_export_copies_originals_and_reads_each_file_back(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            result = self.call(FakeLibrary(), action="export_photos", item_id="A/L0/001, missing/1", path=folder)
            self.assertTrue(result["ok"])
            self.assertEqual((1, 1, False), (result["exported"], result["failed"], result["verified"]))
            done = next(item for item in result["items"] if item["ok"])
            self.assertTrue(Path(done["path"]).is_file())
            self.assertEqual(10, done["bytes"])
            self.assertEqual(64, len(done["sha256"]))
            self.assertFalse(result["library_changed"])

    def test_export_needs_ids_and_respects_the_agent_scope(self) -> None:
        self.assertEqual("APP_ADAPTER_ARGUMENT_INVALID",
                         self.call(FakeLibrary(), action="export_photos", item_id=" ")["reason_code"])
        scope = SimpleNamespace(path_roots=("/tmp/allowed-root",))
        with patch("mcp_server.policy.current_policy_context", return_value=SimpleNamespace(scope=scope)):
            denied = self.call(FakeLibrary(), action="export_photos", item_id="A/L0/001", path="/tmp/elsewhere")
        self.assertEqual("SCOPE_PATH_DENIED", denied["reason_code"])

    def test_reads_are_reads_and_export_is_a_local_write(self) -> None:
        _, read = resolve_risk("mac_app", {"app": "Photos", "action": "find_photos"})
        self.assertNotIn(Capability.LOCAL_WRITE, read.capabilities)
        _, write = resolve_risk("mac_app", {"app": "Photos", "action": "export_photos"})
        self.assertIn(Capability.LOCAL_WRITE, write.capabilities)

    def test_the_build_ships_the_helper_with_a_photos_usage_text(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "menu_app" / "build_app.sh").read_text()
        self.assertIn('"${SCRIPT_DIR}/PhotosReader/main.swift"', script)
        self.assertIn("NSPhotoLibraryUsageDescription", script)
        self.assertIn("-framework Photos", script)
        helper = (Path(__file__).resolve().parents[1] / "menu_app" / "PhotosReader" / "main.swift").read_text()
        # PhotoKit waits a minute and more for a library that is not there; the helper looks first and has a deadline.
        self.assertLess(helper.index("if !libraryOnDisk()"), helper.index("switch action {"))
        self.assertIn('request["deadline_s"]', helper)
        self.assertNotIn("location.coordinate", helper)


if __name__ == "__main__":
    unittest.main()
