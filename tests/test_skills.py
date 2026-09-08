import os
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from mcp_server import embedding_manager as embeddings
from mcp_server import tools_memory as memory
from mcp_server import tools_skills as skills


SKILL_TEXT = """---
name: wordpress-performance
description: Diagnose WordPress slowness, cache issues, PHP-FPM bottlenecks, and 5xx incidents.
metadata:
  owner: test
---
# WordPress Performance

1. Inspect logs before changes.
2. Run `scripts/check.sh` when server latency is suspected.
3. Read `references/gtranslate.md` when translation traffic is involved.
"""


class SkillToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mac-mcp-skills-test-")
        self.root = Path(self.temp.name) / "skills"
        self.external = Path(self.temp.name) / "external"
        self.old = {key: os.environ.get(key) for key in [
            "MAC_MCP_SKILLS_DIR", "MAC_MCP_EMBEDDING",
            "MAC_MCP_EMBEDDING_MODEL_CACHE", "MAC_MCP_EMBEDDING_IDLE_SECONDS",
        ]}
        os.environ["MAC_MCP_SKILLS_DIR"] = str(self.root)
        os.environ["MAC_MCP_EMBEDDING"] = "feature_hash"
        os.environ["MAC_MCP_EMBEDDING_MODEL_CACHE"] = str(Path(self.temp.name) / "cache")
        os.environ["MAC_MCP_EMBEDDING_IDLE_SECONDS"] = "0"
        skill_dir = self.root / "wordpress-performance"
        (skill_dir / "scripts").mkdir(parents=True)
        (skill_dir / "references").mkdir()
        (skill_dir / "assets").mkdir()
        (skill_dir / "SKILL.md").write_text(SKILL_TEXT, encoding="utf-8")
        (skill_dir / "scripts" / "check.sh").write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
        (skill_dir / "references" / "gtranslate.md").write_text("GTranslate reference", encoding="utf-8")
        (skill_dir / "assets" / "template.json").write_text("{}", encoding="utf-8")

    def tearDown(self):
        for key, value in self.old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        with embeddings.WORKER_LOCK:
            embeddings.discard_worker_locked(terminate=True)
        self.temp.cleanup()

    def test_managed_skill_is_discovered_and_progressively_loaded(self):
        updated = skills.skill_update_index()
        self.assertEqual(1, updated["total"])
        listed = skills.skill_list()
        self.assertEqual(1, listed["count"])
        item = listed["skills"][0]
        self.assertEqual("wordpress-performance", item["name"])
        self.assertNotIn("body", item)
        loaded = skills.skill_get(name="wordpress-performance")
        self.assertIn("# WordPress Performance", loaded["content"])
        paths = {resource["path"] for resource in loaded["resources"]}
        self.assertIn("scripts/check.sh", paths)
        self.assertIn("references/gtranslate.md", paths)
        self.assertIn("assets/template.json", paths)

    def test_search_uses_hybrid_index(self):
        skills.skill_update_index()
        found = skills.skill_search("wordpress cache php-fpm slow", limit=5)
        self.assertEqual("hybrid_search", found["mode"])
        self.assertEqual("wordpress-performance", found["results"][0]["name"])
        self.assertEqual("sqlite_fts5", found["search_backend"]["fts"])
        self.assertTrue(found["search_backend"]["shared_with_memory"])

    def test_external_skill_can_be_registered(self):
        directory = self.external / "server-security"
        directory.mkdir(parents=True)
        path = directory / "SKILL.md"
        path.write_text(
            "---\nname: server-security\ndescription: Review Linux server security controls and suspicious activity.\n---\n# Server Security\nRead logs first.\n",
            encoding="utf-8",
        )
        registered = skills.skill_register(str(directory))
        self.assertEqual("server-security", registered["name"])
        self.assertFalse(registered["managed"])
        loaded = skills.skill_get(path=str(path))
        self.assertEqual("server-security", loaded["name"])

    def test_manual_skill_edit_is_reindexed(self):
        skills.skill_update_index()
        path = self.root / "wordpress-performance" / "SKILL.md"
        path.write_text(SKILL_TEXT.replace("translation traffic", "cloudflare timeout traffic"), encoding="utf-8")
        found = skills.skill_search("cloudflare timeout", limit=5)
        self.assertEqual("wordpress-performance", found["results"][0]["name"])
        self.assertGreaterEqual(found["index_sync"]["updated"], 1)

    def test_invalid_skill_frontmatter_is_rejected(self):
        directory = self.external / "bad"
        directory.mkdir(parents=True)
        path = directory / "SKILL.md"
        path.write_text("---\nname: Bad Name\ndescription: bad\n---\nbody\n", encoding="utf-8")
        with self.assertRaises(HTTPException):
            skills.skill_register(str(path))

    def test_memory_and_skills_share_embedding_manager(self):
        self.assertIs(memory.embeddings, skills.embeddings)
        self.assertIs(memory._FASTEMBED_WORKER_LOCK, embeddings.WORKER_LOCK)


if __name__ == "__main__":
    unittest.main()
