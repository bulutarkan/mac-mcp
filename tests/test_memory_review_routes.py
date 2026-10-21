"""#60: the owner reviews what agents learned, in Settings, without asking an agent.

Settings › Memory offered counts, retention, export and delete-all, but no way to read or fix one memory,
and lessons had no screen at all. These routes back the new Review sheet: memories are searched, edited
and deleted one by one; lessons are filtered by role and state, approved, enabled, disabled and deleted.
Private mode still refuses what would keep content; deleting always works.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from mcp_server import tools_lessons as lessons
from mcp_server import tools_memory as tm
from tests.test_memory_privacy import TOKEN, MemoryEndpointTests

AUTH = {"Authorization": "Bearer " + TOKEN}


class MemoryReviewRouteTests(MemoryEndpointTests):
    def setUp(self) -> None:
        super().setUp()
        lesson_dir = patch.dict("os.environ", {"MAC_MCP_LESSON_DIR": str(Path(self.temp.name) / "lessons")})
        lesson_dir.start()
        self.addCleanup(lesson_dir.stop)
        self.api = self.client()

    def test_memories_are_listed_searched_edited_and_deleted(self) -> None:
        self.add("The owner's dog is called Pamuk", importance="high")
        self.add("Invoices go to the accounting folder")
        self.assertEqual(401, self.api.get("/dashboard/api/memory/items").status_code)
        listed = self.api.get("/dashboard/api/memory/items", headers=AUTH).json()
        self.assertEqual(2, listed["count"])
        found = self.api.get("/dashboard/api/memory/items?query=dog", headers=AUTH).json()
        self.assertIn("Pamuk", found["results"][0]["content"])
        memory_id = found["results"][0]["memory_id"]
        high = self.api.get("/dashboard/api/memory/items?importance=high", headers=AUTH).json()
        self.assertEqual([memory_id], [row["memory_id"] for row in high["results"]])

        changed = self.api.post("/dashboard/api/memory/item", headers=AUTH,
                                json={"memory_id": memory_id, "content": "The owner's dog is called Pamuk (a Kangal)",
                                      "tags": ["pets"], "importance": "normal"}).json()
        self.assertEqual(("updated", ["pets"], "normal"), (changed["action"], changed["tags"], changed["importance"]))
        self.assertIn("Kangal", tm.memory_get(memory_id)["content"])
        self.assertEqual(400, self.api.post("/dashboard/api/memory/item", headers=AUTH,
                                            json={"memory_id": memory_id, "content": "  "}).status_code)
        self.assertEqual(400, self.api.post("/dashboard/api/memory/item", headers=AUTH, json={}).status_code)

        deleted = self.api.post("/dashboard/api/memory/item/delete", headers=AUTH, json={"memory_id": memory_id}).json()
        self.assertEqual("deleted", deleted["action"])
        self.assertEqual(1, self.api.get("/dashboard/api/memory/items", headers=AUTH).json()["count"])
        self.assertEqual(404, self.api.post("/dashboard/api/memory/item/delete", headers=AUTH,
                                            json={"memory_id": memory_id}).status_code)

    def test_secrets_are_refused_in_an_edit(self) -> None:
        memory_id = self.add("A plain fact")["memory_id"]
        refused = self.api.post("/dashboard/api/memory/item", headers=AUTH,
                                json={"memory_id": memory_id, "content": "api_key=sk-abcdefghijklmnopqrstuvwx"})
        self.assertEqual(400, refused.status_code)
        self.assertEqual("A plain fact", tm.memory_get(memory_id)["content"])

    def test_lessons_are_filtered_and_their_state_changed_or_deleted(self) -> None:
        first = lessons.lesson_record(role="coder", trigger_context="editing a migration", mistake_pattern="no backup",
                                      preferred_action="back up the database before migrating")["lesson"]["lesson_id"]
        lessons.lesson_record(role="reviewer", trigger_context="reviewing a PR", mistake_pattern="skipped tests",
                              preferred_action="run the tests before approving")
        listed = self.api.get("/dashboard/api/lessons", headers=AUTH).json()
        self.assertEqual(2, listed["count"])
        coder = self.api.get("/dashboard/api/lessons?role=coder&state=candidate", headers=AUTH).json()
        self.assertEqual([first], [row["lesson_id"] for row in coder["results"]])
        found = self.api.get("/dashboard/api/lessons?query=migration", headers=AUTH).json()
        self.assertEqual(first, found["results"][0]["lesson_id"])

        approved = self.api.post("/dashboard/api/lessons/feedback", headers=AUTH,
                                 json={"lesson_id": first, "outcome": "approve"}).json()
        self.assertEqual("active", approved["lesson"]["state"])
        disabled = self.api.post("/dashboard/api/lessons/feedback", headers=AUTH,
                                 json={"lesson_id": first, "outcome": "disable"}).json()
        self.assertEqual("disabled", disabled["lesson"]["state"])
        self.assertEqual(400, self.api.post("/dashboard/api/lessons/feedback", headers=AUTH,
                                            json={"lesson_id": first, "outcome": "success"}).status_code)
        self.assertEqual(404, self.api.post("/dashboard/api/lessons/feedback", headers=AUTH,
                                            json={"lesson_id": "nope", "outcome": "enable"}).status_code)

        deleted = self.api.post("/dashboard/api/lessons/delete", headers=AUTH, json={"lesson_id": first}).json()
        self.assertEqual(1, deleted["deleted"])
        self.assertEqual(1, self.api.get("/dashboard/api/lessons", headers=AUTH).json()["count"])

    def test_private_mode_refuses_what_keeps_content_but_allows_deleting(self) -> None:
        memory_id = self.add("Kept fact")["memory_id"]
        lesson = lessons.lesson_record(role="coder", trigger_context="x", mistake_pattern="y",
                                       preferred_action="z")["lesson"]["lesson_id"]
        self.set_privacy(private_mode=True)
        edit = self.api.post("/dashboard/api/memory/item", headers=AUTH, json={"memory_id": memory_id, "content": "new"})
        self.assertEqual((409, "private_mode"), (edit.status_code, edit.json()["error"]))
        enable = self.api.post("/dashboard/api/lessons/feedback", headers=AUTH, json={"lesson_id": lesson, "outcome": "enable"})
        self.assertEqual(409, enable.status_code)
        disable = self.api.post("/dashboard/api/lessons/feedback", headers=AUTH, json={"lesson_id": lesson, "outcome": "disable"})
        self.assertEqual(200, disable.status_code)
        self.assertEqual("deleted", self.api.post("/dashboard/api/memory/item/delete", headers=AUTH,
                                                  json={"memory_id": memory_id}).json()["action"])
        self.assertEqual(1, self.api.post("/dashboard/api/lessons/delete", headers=AUTH,
                                          json={"lesson_id": lesson}).json()["deleted"])
        self.assertNotIn("Kept fact", json.dumps(tm.memory_export_all()))


class MemoryReviewUITests(MemoryEndpointTests):
    def test_settings_opens_a_review_sheet_backed_by_these_routes(self) -> None:
        root = Path(__file__).resolve().parents[1] / "menu_app"
        settings = (root / "Sources" / "SettingsView.swift").read_text(encoding="utf-8")
        self.assertIn('Button("Review Memories and Lessons…") { reviewingMemory = true }', settings)
        self.assertIn("MemoryReviewView().environmentObject(state)", settings)
        review = (root / "Sources" / "MemoryReviewView.swift").read_text(encoding="utf-8")
        for route in ("dashboard/api/memory/items?", "dashboard/api/memory/item\"", "dashboard/api/memory/item/delete",
                      "dashboard/api/lessons?", "dashboard/api/lessons/feedback", "dashboard/api/lessons/delete"):
            self.assertIn(route, review)
        for words in ('Button("Approve")', 'Button("Turn Off")', 'Button("Turn On")', '"Delete this memory?"'):
            self.assertIn(words, review)
        self.assertIn('"${SCRIPT_DIR}/Sources/MemoryReviewView.swift"', (root / "build_app.sh").read_text(encoding="utf-8"))
