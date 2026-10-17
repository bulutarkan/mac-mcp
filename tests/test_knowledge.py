"""#197: knowledge folders. The owner picks folders; Mac MCP indexes their text on this Mac and answers
searches with the file, page and a snippet, keeps the index current as files change, and never shows a
scoped agent anything outside its own roots."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp_server import embedding_manager as embeddings
from mcp_server import knowledge
from mcp_server.policy import PolicyContext, reset_policy_context, set_policy_context
from mcp_server.policy_scope import ResourceScope

TOKEN = "dashboard-test-token-0123456789-abcdefghijklmnopqrstuvwxyz"
AUTH = {"authorization": f"Bearer {TOKEN}"}


def _feature_vectors(texts):
    return embeddings.FEATURE_VECTOR_BACKEND, [embeddings.feature_vector(text) for text in texts]


class KnowledgeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="mac-mcp-knowledge-")).resolve()
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.state = self.temp / "state"
        self.state.mkdir()
        self.settings = self.state / "settings.json"
        self.settings.write_text("{}", encoding="utf-8")
        self.docs = self.temp / "Contracts"
        self.docs.mkdir()
        env = patch.dict(os.environ, {"MAC_MCP_STATE_DIR": str(self.state), "MAC_MCP_SETTINGS_PATH": str(self.settings),
                                      "MAC_MCP_KNOWLEDGE_DIR": str(self.state / "knowledge")})
        env.start()
        self.addCleanup(env.stop)
        for target, value in (("_vectors", _feature_vectors), ("_private_mode", lambda: False)):
            patcher = patch.object(knowledge, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, name: str, text: str) -> Path:
        path = self.docs / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def use_folder(self, folder: Path = None) -> None:
        knowledge.add_folder(str(folder or self.docs))


class FolderRuleTests(KnowledgeTestCase):
    def test_broad_and_system_folders_are_refused(self) -> None:
        for path in ["/", str(Path.home()), str(Path.home() / "Library" / "Mail"), "/System/Library", "/usr/bin",
                     str(self.state), "relative/folder", str(self.temp / "missing")]:
            with self.subTest(path=path):
                self.assertIsNotNone(knowledge.folder_problem(path))
        self.assertIsNone(knowledge.folder_problem(str(self.docs)))

    def test_add_and_remove_are_saved_and_a_parent_replaces_its_children(self) -> None:
        child = self.docs / "2026"
        child.mkdir()
        knowledge.add_folder(str(child))
        self.assertEqual([str(child)], knowledge.configured_folders())
        knowledge.add_folder(str(child / "."))  # the same folder again changes nothing
        self.assertEqual([str(child)], knowledge.configured_folders())
        with self.assertRaises(Exception):
            knowledge.add_folder(str(child / "missing"))
        knowledge.add_folder(str(self.docs))
        self.assertEqual([str(self.docs)], knowledge.configured_folders())
        saved = json.loads(self.settings.read_text())
        self.assertEqual([str(self.docs)], saved["knowledge"]["folders"])
        knowledge.remove_folder(str(self.docs))
        self.assertEqual([], knowledge.configured_folders())

    def test_a_folder_inside_a_listed_one_is_refused(self) -> None:
        (self.docs / "inner").mkdir()
        self.use_folder()
        with self.assertRaises(Exception) as raised:
            knowledge.add_folder(str(self.docs / "inner"))
        self.assertIn("already inside", str(raised.exception.detail))


class IndexAndSearchTests(KnowledgeTestCase):
    def test_search_names_the_file_snippet_and_hash(self) -> None:
        self.write("lease.md", "Lease agreement.\n\nThe deposit is two months of rent, paid before moving in.")
        self.write("invoice.txt", "Invoice 2026-114 for web design, due on the 30th.")
        self.write("photo.jpg", "not text")  # unsupported types are skipped
        self.use_folder()
        result = knowledge.knowledge_search("how much is the deposit")
        top = result["results"][0]
        self.assertEqual("lease.md", top["name"])
        self.assertIn("deposit", top["snippet"])
        self.assertEqual(64, len(top["sha256"]))
        self.assertIsNone(top["page"])
        self.assertEqual(str(self.docs), top["folder"])
        self.assertIn("file name", result["how_to_cite"])
        self.assertEqual(2, result["index"]["searched_files"])

    def test_changes_deletions_and_renames_follow_the_folder(self) -> None:
        note = self.write("notes.md", "Backups run every night at 03:00.")
        self.use_folder()
        self.assertEqual(1, knowledge.scan()["indexed"])
        self.assertEqual({"indexed": 0, "removed": 0, "unchanged": 1, "pending": 0, "paused": None}, knowledge.scan())
        note.write_text("Backups now run every night at 04:30 and are kept 30 days.", encoding="utf-8")
        os.utime(note, None)
        self.write("extra.txt", "A second file about invoices.")
        summary = knowledge.scan()
        self.assertEqual(2, summary["indexed"])
        self.assertIn("04:30", knowledge.knowledge_search("backups night")["results"][0]["snippet"])
        note.rename(self.docs / "renamed.md")
        (self.docs / "extra.txt").unlink()
        summary = knowledge.scan()
        self.assertEqual((1, 2), (summary["indexed"], summary["removed"]))
        names = {item["name"] for item in knowledge.knowledge_search("backups invoices")["results"]}
        self.assertEqual({"renamed.md"}, names)

    def test_hidden_files_and_skipped_folders_are_not_read(self) -> None:
        self.write(".secret.txt", "deposit hidden")
        self.write("node_modules/pkg/readme.md", "deposit in a dependency")
        self.write(".git/notes.md", "deposit in git")
        self.write("real.md", "deposit real")
        self.use_folder()
        self.assertEqual({"real.md"}, {item["name"] for item in knowledge.knowledge_search("deposit")["results"]})

    def test_path_narrows_and_must_be_inside_a_folder(self) -> None:
        self.write("a/one.md", "rent clause in folder a")
        self.write("b/two.md", "rent clause in folder b")
        self.use_folder()
        narrowed = knowledge.knowledge_search("rent clause", path=str(self.docs / "a"))
        self.assertEqual({"one.md"}, {item["name"] for item in narrowed["results"]})
        with self.assertRaises(Exception) as raised:
            knowledge.knowledge_search("rent", path=str(self.temp))
        self.assertEqual(400, raised.exception.status_code)

    def test_a_scoped_agent_only_sees_its_own_roots(self) -> None:
        self.write("team/plan.md", "project budget is 40k")
        self.write("hr/salaries.md", "project budget and salaries")
        self.use_folder()
        knowledge.scan()
        token = set_policy_context(PolicyContext(actor="agent", agent_id="agt_x",
                                                 scope=ResourceScope(path_roots=[str(self.docs / "team")])))
        try:
            result = knowledge.knowledge_search("project budget")
        finally:
            reset_policy_context(token)
        self.assertEqual({"plan.md"}, {item["name"] for item in result["results"]})
        self.assertEqual(1, result["index"]["searched_files"])

    def test_without_folders_it_says_where_to_add_one(self) -> None:
        result = knowledge.knowledge_search("anything")
        self.assertEqual([], result["results"])
        self.assertIn("Settings", result["message"])
        with self.assertRaises(Exception):
            knowledge.knowledge_search("   ")

    def test_private_mode_pauses_indexing_but_search_still_answers(self) -> None:
        self.write("kept.md", "indexed before private mode: deposit")
        self.use_folder()
        knowledge.scan()
        self.write("new.md", "written in private mode: deposit")
        with patch.object(knowledge, "_private_mode", lambda: True):
            self.assertEqual("private_mode", knowledge.scan()["paused"])
            names = {item["name"] for item in knowledge.knowledge_search("deposit")["results"]}
        self.assertEqual({"kept.md"}, names)

    def test_a_removed_folder_takes_its_text_with_it(self) -> None:
        self.write("lease.md", "deposit terms")
        self.use_folder()
        knowledge.scan()
        knowledge.remove_folder(str(self.docs))
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(str(self.state / "knowledge" / knowledge.INDEX_NAME))) as conn:
            counts = tuple(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                           for table in ("files", "chunks", "chunks_fts"))
        self.assertEqual((0, 0, 0), counts)

    def test_the_index_is_owner_only(self) -> None:
        self.write("a.md", "text")
        self.use_folder()
        knowledge.scan()
        index = self.state / "knowledge"
        self.assertEqual("0o700", oct(index.stat().st_mode & 0o777))
        self.assertEqual("0o600", oct((index / knowledge.INDEX_NAME).stat().st_mode & 0o777))

    def test_overview_counts_files_per_folder(self) -> None:
        self.write("a.md", "some text")
        self.write("empty.txt", "   ")
        self.use_folder()
        knowledge.scan()
        folder = knowledge.overview()["folders"][0]
        self.assertEqual((2, 1, 1), (folder["files"], folder["indexed"], folder["no_text"]))


@unittest.skipUnless(Path("/usr/bin/textutil").exists() and Path("/usr/sbin/cupsfilter").exists(), "macOS tools")
class DocumentExtractionTests(KnowledgeTestCase):
    def test_pdf_pages_and_word_files(self) -> None:
        source = self.write("lease.txt", "Lease page one: rent is paid on the 5th.\n\f\nPage two: the lease ends on 1 January 2027.\n")
        pdf = self.docs / "lease.pdf"
        with pdf.open("wb") as handle:
            subprocess.run(["/usr/sbin/cupsfilter", "-m", "application/pdf", str(source)], stdout=handle,
                           stderr=subprocess.DEVNULL, check=True, timeout=60)
        subprocess.run(["/usr/bin/textutil", "-convert", "docx", "-output", str(self.docs / "lease.docx"), str(source)],
                       check=True, timeout=60)
        source.unlink()
        pages, error = knowledge.extract(pdf)
        self.assertIsNone(error)
        self.assertEqual([(1, False), (2, False)], [(page, ocr) for page, _text, ocr in pages])
        self.use_folder()
        hits = knowledge.knowledge_search("when does the lease end", limit=5)["results"]
        pdf_hit = next(item for item in hits if item["name"] == "lease.pdf")
        self.assertEqual(2, pdf_hit["page"])
        self.assertIn("lease.docx", {item["name"] for item in hits})


class IndexRepairTests(KnowledgeTestCase):
    """#242: a file that fails to index keeps its earlier rows; a damaged index is rebuilt from the folders."""

    def test_a_file_that_fails_to_index_keeps_its_earlier_text(self) -> None:
        note = self.write("plan.md", "The garden fence is painted green in May.")
        self.use_folder()
        knowledge.scan()
        with closing(knowledge._connect()) as conn:
            # Fails after the file's old rows were deleted, half way through writing the new ones.
            conn.execute("CREATE TRIGGER boom BEFORE INSERT ON chunks WHEN NEW.text LIKE '%BOOM%' "
                         "BEGIN SELECT RAISE(ABORT, 'injected'); END")
            conn.commit()
        note.write_text("BOOM: the fence is now blue.", encoding="utf-8")
        os.utime(note, None)
        with self.assertLogs("mac_mcp.knowledge", "WARNING"):
            self.assertEqual(0, knowledge.scan()["indexed"])
        hits = knowledge.knowledge_search("garden fence painted")["results"]
        self.assertEqual(["plan.md"], [item["name"] for item in hits])
        self.assertIn("green", hits[0]["snippet"])

    def test_a_damaged_index_is_rebuilt_by_the_next_search(self) -> None:
        self.write("lease.md", "The deposit is two months of rent.")
        self.use_folder()
        knowledge.scan()
        index = knowledge.index_dir() / knowledge.INDEX_NAME
        for suffix in ("-wal", "-shm"):
            Path(str(index) + suffix).unlink(missing_ok=True)
        index.write_bytes(b"this is not a database" * 400)
        with self.assertLogs("mac_mcp.knowledge", "WARNING"):
            result = knowledge.knowledge_search("deposit rent")
        self.assertEqual(["lease.md"], [item["name"] for item in result["results"]])
        with closing(sqlite3.connect(str(index))) as conn:
            self.assertEqual("ok", conn.execute("PRAGMA quick_check").fetchone()[0])

    def test_damage_found_while_searching_rebuilds_once(self) -> None:
        self.write("lease.md", "The deposit is two months of rent.")
        self.use_folder()
        knowledge.scan()
        real = knowledge._fts_scores
        calls = []

        def damaged_once(conn, query):
            calls.append(query)
            if len(calls) == 1:
                raise sqlite3.DatabaseError("database disk image is malformed")
            return real(conn, query)

        with patch.object(knowledge, "_fts_scores", damaged_once), self.assertLogs("mac_mcp.knowledge", "WARNING"):
            result = knowledge.knowledge_search("deposit rent")
        self.assertTrue(result["index"]["rebuilt"])
        self.assertEqual(["lease.md"], [item["name"] for item in result["results"]])
        self.assertEqual(2, len(calls))

    def test_other_database_errors_are_not_treated_as_damage(self) -> None:
        self.assertFalse(knowledge._damaged(sqlite3.OperationalError("database is locked")))
        self.assertTrue(knowledge._damaged(sqlite3.DatabaseError("file is not a database")))


class IndexCostTests(KnowledgeTestCase):
    """#253: search reads only the best passages, indexing keeps a running count, slow walks back off."""

    def test_a_changed_file_is_counted_without_its_old_passages(self) -> None:
        note = self.write("a.md", "first note about rent")
        self.write("b.md", "second note about rent")
        self.use_folder()
        with patch.object(knowledge, "MAX_CHUNKS", 2):
            knowledge.scan()
            note.write_text("first note about rent, changed", encoding="utf-8")
            os.utime(note, None)
            self.assertEqual(1, knowledge.scan()["indexed"])
            with closing(knowledge._connect()) as conn:
                self.assertEqual({"indexed"}, {row[0] for row in conn.execute("SELECT status FROM files")})
            self.write("c.md", "third note about rent")
            knowledge.scan()
            with closing(knowledge._connect()) as conn:
                self.assertEqual("index is full", conn.execute(
                    "SELECT error FROM files WHERE path LIKE '%c.md'").fetchone()[0])

    def test_semantic_candidates_keep_the_best_and_every_lexical_match(self) -> None:
        for i in range(6):
            self.write(f"n{i}.md", f"note {i} about the garden")
        self.use_folder()
        knowledge.scan()
        with closing(knowledge._connect()) as conn:
            everything = knowledge._semantic_scores(conn, "garden note", None)
            weakest = min(everything, key=everything.get)
            with patch.object(knowledge, "SEMANTIC_KEEP", 2):
                kept = knowledge._semantic_scores(conn, "garden note", None, also={weakest: 1.0})
        best = sorted(everything, key=everything.get, reverse=True)[:2]
        self.assertEqual(set(best) | {weakest}, set(kept))

    def test_search_does_not_wait_for_a_slow_walk(self) -> None:
        self.write("lease.md", "The deposit is two months of rent.")
        self.use_folder()
        knowledge.scan()
        with closing(knowledge._connect()) as conn:
            knowledge._set_meta(conn, "walk_s", "4.0")
            knowledge._set_meta(conn, "last_scan_monotonic", "-1e18")
            conn.commit()
        with patch.object(knowledge, "scan", side_effect=AssertionError("search waited for a scan")), \
                patch.object(knowledge, "wake") as wake:
            result = knowledge.knowledge_search("deposit")
        wake.assert_called_once()
        self.assertTrue(result["index"]["refreshing"])
        self.assertEqual(["lease.md"], [item["name"] for item in result["results"]])
        self.assertEqual(400.0, knowledge._idle_wait())

    def test_a_quick_walk_keeps_the_usual_interval(self) -> None:
        self.write("lease.md", "rent")
        self.use_folder()
        knowledge.scan()
        self.assertEqual(knowledge.SCAN_EVERY_S, knowledge._idle_wait())


# A page of text drawn into an image: what a scanner or a phone photo of a page gives.
_DRAW_PAGE = """
ObjC.import('AppKit');
function run(argv) {
  var w = 1240, h = 1754;
  var img = $.NSImage.alloc.initWithSize($.NSMakeSize(w, h));
  img.lockFocus;
  $.NSColor.whiteColor.setFill; $.NSRectFill($.NSMakeRect(0, 0, w, h));
  var attrs = $({'NSFont': $.NSFont.systemFontOfSize(36), 'NSColor': $.NSColor.blackColor});
  var lines = argv[1].split('|');
  for (var i = 0; i < lines.length; i++) {
    $(lines[i]).drawAtPointWithAttributes($.NSMakePoint(80, h - 160 - i * 70), attrs);
  }
  img.unlockFocus;
  var rep = $.NSBitmapImageRep.imageRepWithData(img.TIFFRepresentation);
  rep.representationUsingTypeProperties($.NSBitmapImageFileTypePNG, $()).writeToFileAtomically(argv[0], true);
  return 'ok';
}
"""


@unittest.skipUnless(Path("/usr/bin/osascript").exists() and Path("/usr/bin/sips").exists(), "macOS tools")
class ScannedDocumentTests(KnowledgeTestCase):
    """#258: scanned PDFs and photos of pages are read with on-device text recognition (Vision)."""

    def draw(self, name: str, lines: str) -> Path:
        path = self.temp / name
        subprocess.run(["/usr/bin/osascript", "-l", "JavaScript", "-e", _DRAW_PAGE, str(path), lines],
                       check=True, capture_output=True, timeout=60)
        return path

    def test_a_scanned_pdf_and_a_photo_are_searchable_and_marked_as_ocr(self) -> None:
        page = self.draw("page.png", "Kira sözleşmesi|Aylık kira bedeli 12.500 TL olarak belirlenmiştir.")
        subprocess.run(["/usr/bin/sips", "-s", "format", "pdf", str(page), "--out", str(self.docs / "scan.pdf")],
                       check=True, capture_output=True, timeout=60)
        self.draw("receipt.png", "Invoice 4471|Total due: 980 EUR by 30 November")
        shutil.copy(self.temp / "receipt.png", self.docs / "receipt.png")
        pages, error = knowledge.extract(self.docs / "scan.pdf")
        self.assertIsNone(error)
        self.assertEqual(1, len(pages))
        self.assertEqual(1, pages[0][0])
        self.assertTrue(pages[0][2])
        self.assertIn("12.500", pages[0][1])
        self.assertIn("sözleşmesi", pages[0][1])
        self.use_folder()
        knowledge.scan()
        hits = knowledge.knowledge_search("aylık kira bedeli", limit=5)["results"]
        scan_hit = next(item for item in hits if item["name"] == "scan.pdf")
        self.assertEqual((1, True), (scan_hit["page"], scan_hit["ocr"]))
        hits = knowledge.knowledge_search("invoice total due", limit=5)["results"]
        photo = next(item for item in hits if item["name"] == "receipt.png")
        self.assertEqual((None, True), (photo["page"], photo["ocr"]))

    def test_a_pdf_with_a_text_layer_is_not_ocred(self) -> None:
        source = self.write("note.txt", "This page already has real text that a PDF reader can select and copy.\n")
        pdf = self.docs / "note.pdf"
        with pdf.open("wb") as handle:
            subprocess.run(["/usr/sbin/cupsfilter", "-m", "application/pdf", str(source)], stdout=handle,
                           stderr=subprocess.DEVNULL, check=True, timeout=60)
        pages, error = knowledge.extract(pdf)
        self.assertIsNone(error)
        self.assertEqual([False], [ocr for _page, _text, ocr in pages])

    def test_files_found_empty_before_ocr_are_read_once_more(self) -> None:
        self.draw("page.png", "Teslim tarihi 15 Mart 2027")
        shutil.copy(self.temp / "page.png", self.docs / "page.png")
        self.use_folder()
        with patch.object(knowledge, "extract", return_value=([], None)):
            knowledge.scan()
        with closing(knowledge._connect()) as conn:
            self.assertEqual("no_text", conn.execute("SELECT status FROM files").fetchone()[0])
            conn.execute("DELETE FROM meta WHERE key = 'extractor'")
            conn.commit()
        self.assertEqual(1, knowledge.scan()["indexed"])
        self.assertEqual(0, knowledge.scan()["indexed"])
        hits = knowledge.knowledge_search("teslim tarihi", limit=5)["results"]
        self.assertEqual(["page.png"], [item["name"] for item in hits])


class ChunkTests(unittest.TestCase):
    def test_chunks_overlap_and_stay_within_size(self) -> None:
        text = " ".join(f"Sentence number {i} about the contract." for i in range(400))
        pieces = knowledge.chunk(text)
        self.assertGreater(len(pieces), 5)
        self.assertTrue(all(len(piece) <= knowledge.CHUNK_CHARS for piece in pieces))
        self.assertTrue(pieces[1][:20] in pieces[0] or pieces[0][-40:].strip()[:10] in pieces[1])
        self.assertEqual([], knowledge.chunk("  \n\n "))


class OwnerRouteTests(KnowledgeTestCase):
    def setUp(self) -> None:
        super().setUp()
        from mcp_server.dashboard_routes import create_dashboard_routes
        from mcp_server.observability import TelemetryManager
        from mcp_server.security import load_settings
        telemetry = TelemetryManager(db_path=self.state / "telemetry.sqlite3")
        self.client = TestClient(Starlette(routes=create_dashboard_routes(telemetry, load_settings(), TOKEN)))

    def test_only_the_owner_reads_and_changes_folders(self) -> None:
        self.assertEqual(401, self.client.get("/dashboard/api/knowledge").status_code)
        self.assertEqual(401, self.client.post("/dashboard/api/knowledge/folders",
                                               json={"action": "add", "path": str(self.docs)}).status_code)
        added = self.client.post("/dashboard/api/knowledge/folders", headers=AUTH,
                                 json={"action": "add", "path": str(self.docs)}).json()
        self.assertEqual([str(self.docs)], [folder["path"] for folder in added["folders"]])
        refused = self.client.post("/dashboard/api/knowledge/folders", headers=AUTH,
                                   json={"action": "add", "path": str(Path.home())})
        self.assertEqual(400, refused.status_code)
        self.assertIn("home folder", refused.json()["message"])
        self.assertEqual(400, self.client.post("/dashboard/api/knowledge/folders", headers=AUTH,
                                               json={"action": "index_everything"}).status_code)
        removed = self.client.post("/dashboard/api/knowledge/folders", headers=AUTH,
                                   json={"action": "remove", "path": str(self.docs)}).json()
        self.assertEqual([], removed["folders"])


class RegistrationTests(unittest.TestCase):
    def test_it_is_a_sensitive_read_scoped_like_files(self) -> None:
        from mcp_server import data_guard, policy, privacy_inventory, tool_summaries
        risk = policy.TOOL_RISKS["knowledge_search"] if hasattr(policy, "TOOL_RISKS") else policy.resolve_risk(
            "knowledge_search", {})[1]
        self.assertEqual("files", risk.family)
        self.assertIn("knowledge_search", data_guard._SOURCE_TOOLS)
        self.assertIn("knowledge_search", tool_summaries.CORE_TOOL_NAMES)
        self.assertIn("knowledge", privacy_inventory.DELETABLE)
        self.assertIn("knowledge", {row["key"] for row in privacy_inventory.stores(measure=False)})


if __name__ == "__main__":
    unittest.main()
