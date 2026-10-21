"""#61: the skills Mac MCP finds are listed in Settings, and the owner can keep one out.

Mac MCP discovers skills in its own folder and in the CLIs' folders, but nothing showed which or let the
owner leave one out. Settings › Agents now lists each with its source and last change; an excluded skill
is gone from skill_list, skill_search and skill_get, stays excluded after a re-index, and its files and
the CLI that owns it are untouched.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp_server import embedding_manager as embeddings
from mcp_server import tools_skills as skills
from mcp_server.dashboard_routes import create_dashboard_routes
from mcp_server.observability import TelemetryManager
from mcp_server.security import load_settings

TOKEN = "skill-choice-test-token-0123456789abcdef"
AUTH = {"Authorization": "Bearer " + TOKEN}


def skill(folder: Path, name: str, description: str) -> Path:
    (folder / name).mkdir(parents=True)
    path = folder / name / "SKILL.md"
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n\nSteps for {description}.\n",
                    encoding="utf-8")
    return path


class SkillChoiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mac-mcp-skill-choice-")
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.cli = base / "cli-skills"
        env = patch.dict(os.environ, {
            "MAC_MCP_SKILLS_DIR": str(base / "skills"), "MAC_MCP_CLI_SKILLS_DIRS": str(self.cli),
            "MAC_MCP_EMBEDDING": "feature_hash", "MAC_MCP_EMBEDDING_MODEL_CACHE": str(base / "cache"),
            "MAC_MCP_EMBEDDING_IDLE_SECONDS": "0", "MAC_MCP_SETTINGS_PATH": str(base / "settings.json"),
        })
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(lambda: embeddings.discard_worker_locked(terminate=True))
        self.managed = skill(base / "skills", "invoice-filing", "filing invoices into folders")
        self.external = skill(self.cli, "deploy-check", "checking a deploy before release")
        self.external_text = self.external.read_text(encoding="utf-8")
        self.settings_path = base / "settings.json"

    def names(self, listing):
        return sorted(item["name"] for item in listing)

    def test_settings_lists_every_skill_with_its_source_and_change_time(self) -> None:
        catalog = skills.skill_catalog()
        self.assertEqual(["deploy-check", "invoice-filing"], self.names(catalog["skills"]))
        by_name = {item["name"]: item for item in catalog["skills"]}
        self.assertEqual("Mac MCP", by_name["invoice-filing"]["source"])
        self.assertEqual("Registered", by_name["deploy-check"]["source"])  # a folder outside the known CLI roots
        self.assertTrue(by_name["deploy-check"]["updated_at"].startswith("20"))
        self.assertFalse(any(item["excluded"] for item in catalog["skills"]))

    def test_an_excluded_skill_is_gone_from_list_search_and_get_but_its_files_stay(self) -> None:
        location = str(self.external.resolve())
        skills.set_skill_excluded(location, True)
        self.assertEqual(["invoice-filing"], self.names(skills.skill_list()["skills"]))
        self.assertNotIn("deploy-check", [item["name"] for item in skills.skill_search("deploy release")["results"]])
        with self.assertRaises(HTTPException) as gone:
            skills.skill_get(name="deploy-check")
        self.assertEqual(404, gone.exception.status_code)
        self.assertEqual(self.external_text, self.external.read_text(encoding="utf-8"))
        # Kept after a re-index, shown as excluded in Settings, and let back in.
        skills.skill_update_index()
        catalog = {item["name"]: item for item in skills.skill_catalog()["skills"]}
        self.assertTrue(catalog["deploy-check"]["excluded"])
        self.assertEqual([location], json.loads(self.settings_path.read_text())["skills"]["excluded"])
        skills.set_skill_excluded(location, False)
        self.assertEqual(["deploy-check", "invoice-filing"], self.names(skills.skill_list()["skills"]))

    def test_only_a_found_skill_can_be_excluded(self) -> None:
        with self.assertRaises(HTTPException) as missing:
            skills.set_skill_excluded(str(Path(self.temp.name) / "nowhere" / "SKILL.md"), True)
        self.assertEqual(404, missing.exception.status_code)

    def test_the_settings_routes(self) -> None:
        telemetry = TelemetryManager(db_path=Path(self.temp.name) / "telemetry.sqlite3")
        api = TestClient(Starlette(routes=create_dashboard_routes(telemetry, load_settings(), TOKEN)))
        self.assertEqual(401, api.get("/dashboard/api/skills").status_code)
        listed = api.get("/dashboard/api/skills", headers=AUTH).json()
        self.assertEqual(2, listed["count"])
        location = next(item["location"] for item in listed["skills"] if item["name"] == "deploy-check")
        self.assertEqual(400, api.post("/dashboard/api/skills/exclude", headers=AUTH,
                                       json={"location": location, "excluded": "yes"}).status_code)
        done = api.post("/dashboard/api/skills/exclude", headers=AUTH, json={"location": location, "excluded": True}).json()
        self.assertEqual((True, False), (done["excluded"], done["files_changed"]))
        again = api.post("/dashboard/api/skills/reindex", headers=AUTH).json()
        self.assertEqual(1, again["excluded"])

    def test_settings_shows_the_list_with_a_switch_per_skill(self) -> None:
        sources = Path(__file__).resolve().parents[1] / "menu_app" / "Sources"
        text = "\n".join(path.read_text(encoding="utf-8") for path in sources.glob("*.swift"))
        self.assertIn("dashboard/api/skills/exclude", text)
        self.assertIn("dashboard/api/skills/reindex", text)
        self.assertIn('Toggle("Use in Mac MCP"', text)


if __name__ == "__main__":
    unittest.main()
