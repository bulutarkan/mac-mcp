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
            "MAC_MCP_SKILLS_DIR", "MAC_MCP_CLI_SKILLS_DIRS", "MAC_MCP_EMBEDDING",
            "MAC_MCP_EMBEDDING_MODEL_CACHE", "MAC_MCP_EMBEDDING_IDLE_SECONDS",
        ]}
        os.environ["MAC_MCP_SKILLS_DIR"] = str(self.root)
        os.environ["MAC_MCP_CLI_SKILLS_DIRS"] = ""
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

    def test_unchanged_skills_are_not_parsed_again(self):
        # #171: a sync reuses the indexed row while the file is byte-for-byte unchanged.
        from unittest.mock import patch
        skills.skill_update_index()
        with patch.object(skills, "_parse_skill", wraps=skills._parse_skill) as parse:
            result = skills.skill_update_index()
        parse.assert_not_called()
        self.assertEqual(0, result["index_sync"]["updated"])
        self.assertEqual(1, result["total"])

    def test_a_touched_or_edited_skill_is_parsed_again(self):
        from unittest.mock import patch
        skills.skill_update_index()
        path = self.root / "wordpress-performance" / "SKILL.md"
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000))
        with patch.object(skills, "_parse_skill", wraps=skills._parse_skill) as parse:
            skills.skill_update_index()
        self.assertEqual(1, parse.call_count, "an mtime change alone is not trusted as unchanged")
        # Same size and mtime but different bytes is still caught by the content hash.
        edited = SKILL_TEXT.replace("Inspect logs", "Inspect Logs")
        self.assertEqual(len(edited), len(SKILL_TEXT))
        stat = path.stat()
        path.write_text(edited, encoding="utf-8")
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        result = skills.skill_update_index()
        self.assertEqual(1, result["index_sync"]["updated"])
        self.assertIn("Inspect Logs", skills.skill_get(name="wordpress-performance")["body"])

    def test_deleted_and_invalid_skills_are_still_reported_each_sync(self):
        skills.skill_update_index()
        broken = self.root / "broken-skill"
        broken.mkdir()
        (broken / "SKILL.md").write_text("no frontmatter here", encoding="utf-8")
        for _ in range(2):
            result = skills.skill_update_index()
            self.assertTrue(any(d.get("path", "").endswith("broken-skill/SKILL.md") for d in result["index_sync"]["diagnostics"]))
        (self.root / "wordpress-performance" / "SKILL.md").unlink()
        result = skills.skill_update_index()
        self.assertEqual(1, result["index_sync"]["removed"])
        self.assertEqual(0, result["total"])

    def test_managed_skill_still_wins_a_name_collision(self):
        self.external.mkdir(parents=True)
        outside = self.external / "wordpress-performance"
        outside.mkdir()
        (outside / "SKILL.md").write_text(SKILL_TEXT.replace("Inspect logs", "External copy"), encoding="utf-8")
        skills.skill_register(str(outside / "SKILL.md"))
        for _ in range(2):  # the second sync takes the unchanged path
            result = skills.skill_update_index()
            row = skills.skill_get(name="wordpress-performance")
            self.assertTrue(row["location"].startswith(str(self.root.resolve())), row["location"])
            self.assertTrue(any("duplicate skill name" in d.get("warning", "") for d in result["index_sync"]["diagnostics"]))

    def test_batch_embeddings_on_bulk_skill_discovery(self):
        from unittest.mock import patch
        folder = self.external / "cli"
        for n in range(25):
            d = folder / f"skill-{n}"
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(
                f"---\nname: skill-{n}\ndescription: Test skill number {n}.\n---\nSkill {n}\n"
            )
        os.environ["MAC_MCP_CLI_SKILLS_DIRS"] = str(folder)
        with patch.object(skills.embeddings, "semantic_vectors", wraps=skills.embeddings.semantic_vectors) as embed:
            result = skills.skill_update_index()
        self.assertEqual(26, result["total"])
        self.assertEqual(1, embed.call_count, "Only the managed skill should need a semantic embedding")
        self.assertEqual(0, skills.skill_update_index()["index_sync"]["updated"])
        from contextlib import closing
        from sqlite3 import connect
        with closing(connect(skills._index_path())) as db:
            result = db.execute("SELECT DISTINCT vector_backend FROM skills WHERE managed=0").fetchall()
        self.assertEqual([(embeddings.FEATURE_VECTOR_BACKEND,)], result)

    def test_auto_import_updates_and_removes_cli_skills(self):
        folder = self.external / "cli" / "remotion-render"
        folder.mkdir(parents=True)
        skill = folder / "SKILL.md"
        os.environ["MAC_MCP_CLI_SKILLS_DIRS"] = str(folder.parent)
        skill.write_text("---\nname: remotion-render\ndescription: Render Remotion videos.\n---\nOriginal\n")
        self.assertEqual(2, skills.skill_update_index()["total"])
        self.assertIn("Original", skills.skill_get(name="remotion-render")["content"])
        skill.write_text(skill.read_text().replace("Original", "Updated"))
        self.assertIn("Updated", skills.skill_get(name="remotion-render")["content"])
        skill.unlink()
        self.assertEqual(1, skills.skill_update_index()["total"])

    def test_shared_cli_skill_wins_duplicate_and_managed_wins_cli(self):
        shared = self.external / "shared"
        claude = self.external / "claude"
        for root, body in ((shared, "Shared"), (claude, "Claude")):
            folder = root / "wordpress-performance"
            folder.mkdir(parents=True)
            (folder / "SKILL.md").write_text(SKILL_TEXT.replace("Inspect logs", body))
        os.environ["MAC_MCP_CLI_SKILLS_DIRS"] = os.pathsep.join((str(shared), str(claude)))
        self.assertEqual(1, skills.skill_update_index()["total"])
        self.assertIn("Inspect logs", skills.skill_get(name="wordpress-performance")["body"])
        for root, text in ((shared, "Shared"), (claude, "Claude")):
            folder = root / "another-skill"
            folder.mkdir()
            (folder / "SKILL.md").write_text("---\nname: another-skill\ndescription: Example skill.\n---\n" + text)
        self.assertEqual(2, skills.skill_update_index()["total"])
        self.assertEqual("Shared", skills.skill_get(name="another-skill")["body"])

    def test_mac_mcp_skills_rank_above_equal_external_ones_and_external_is_marked(self):
        # An agent searching for browser help loaded a third-party browser skill (made for another tool)
        # instead of Mac MCP guidance; with about equal relevance Mac MCP's own skills now come first.
        text = "---\nname: {name}\ndescription: Fill web forms in Safari tabs and verify the values.\n---\n{body}\n"
        body = "Open the tab, fill every form field at once, verify the values, close the tab."
        bundled = self.external / "bundled"
        cli = self.external / "cli"
        # The external skill is a shade more relevant (one more query word); others give the index a real spread.
        rows = [(bundled, "forms-alpha", body), (cli, "forms-zulu", body + " Safari."),
                (cli, "forms-mike", body + " Use browser_act fill.")]
        rows += [(cli, f"other-{n:02d}", f"Safari tabs note {n}.") for n in range(30)]
        for root, name, content in rows:
            (root / name).mkdir(parents=True)
            (root / name / "SKILL.md").write_text(text.format(name=name, body=content), encoding="utf-8")
        os.environ["MAC_MCP_CLI_SKILLS_DIRS"] = str(cli)
        old_bundled, old_flag = skills.BUNDLED_SKILLS_DIR, os.environ.get("MAC_MCP_BUNDLED_SKILLS")
        skills.BUNDLED_SKILLS_DIR = bundled.resolve()
        os.environ["MAC_MCP_BUNDLED_SKILLS"] = "1"
        try:
            found = skills.skill_search("Safari form fill verify close tabs", limit=10)["results"]
        finally:
            skills.BUNDLED_SKILLS_DIR = old_bundled
            if old_flag is None:
                os.environ.pop("MAC_MCP_BUNDLED_SKILLS", None)
            else:
                os.environ["MAC_MCP_BUNDLED_SKILLS"] = old_flag
        order = [item["name"] for item in found]
        self.assertLess(order.index("forms-alpha"), order.index("forms-zulu"))
        self.assertLess(order.index("forms-mike"), order.index("forms-zulu"))
        sources = {item["name"]: item["source"] for item in found}
        self.assertEqual("mac_mcp", sources["forms-alpha"])
        self.assertEqual("external", sources["forms-zulu"])
        self.assertEqual("external", sources["forms-mike"])  # still another tool's folder, only ranked up
        self.assertEqual("owner", skills.skill_get(name="wordpress-performance")["source"])

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
