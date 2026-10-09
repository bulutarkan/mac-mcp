from __future__ import annotations

import io
import os
import stat
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from logging.handlers import RotatingFileHandler
from pathlib import Path
from unittest.mock import patch

from mcp_server import log_retention as lr


class LogRetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mac-mcp-logs-")
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {
            "MAC_MCP_STATE_DIR": str(self.root),
            "MAC_MCP_UPDATE_DIR": str(self.root / "update"),
        })
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.temp.cleanup()

    def test_copy_truncate_keeps_an_open_writer_working(self) -> None:
        log = self.root / "mac-mcp.log"
        writer = open(log, "a", encoding="utf-8")  # like uvicorn's inherited stdout
        writer.write("old line\n" * 50_000)
        writer.flush()
        self.assertTrue(lr.rotate_copy_truncate(log, max_bytes=256 * 1024, backups=3))
        writer.write("after rotation\n")
        writer.flush()
        writer.close()
        self.assertEqual("after rotation\n", log.read_text(encoding="utf-8"))
        backup = self.root / "mac-mcp.log.1"
        self.assertTrue(backup.read_text(encoding="utf-8").startswith("old line"))
        self.assertEqual(0o600, stat.S_IMODE(backup.stat().st_mode))

    def test_backups_shift_and_the_oldest_is_dropped(self) -> None:
        log = self.root / "cloudflared.log"
        for generation in range(5):
            log.write_text(f"gen{generation}\n" + "x" * 300_000, encoding="utf-8")
            lr.rotate_copy_truncate(log, max_bytes=256 * 1024, backups=3)
        names = sorted(p.name for p in self.root.glob("cloudflared.log*"))
        self.assertEqual(["cloudflared.log", "cloudflared.log.1", "cloudflared.log.2", "cloudflared.log.3"], names)
        self.assertTrue((self.root / "cloudflared.log.1").read_text(encoding="utf-8").startswith("gen4"))
        self.assertTrue((self.root / "cloudflared.log.3").read_text(encoding="utf-8").startswith("gen2"))

    def test_small_and_symlinked_logs_are_left_alone(self) -> None:
        small = self.root / "ngrok.log"
        small.write_text("tiny", encoding="utf-8")
        self.assertFalse(lr.rotate_copy_truncate(small, max_bytes=256 * 1024))
        target = self.root / "elsewhere.log"
        target.write_text("y" * 400_000, encoding="utf-8")
        link = self.root / "mac-mcp.log"
        link.symlink_to(target)
        self.assertFalse(lr.rotate_copy_truncate(link, max_bytes=256 * 1024))
        self.assertEqual(400_000, target.stat().st_size)

    def test_managed_rotation_covers_server_and_tunnel_logs_and_prunes_update_logs(self) -> None:
        (self.root / "mac-mcp.log").write_text("s" * (11 * 1024 * 1024), encoding="utf-8")
        (self.root / "cloudflared.log").write_text("small", encoding="utf-8")
        logs = self.root / "update" / "logs"
        logs.mkdir(parents=True)
        for index in range(25):
            item = logs / f"upd_{index:03d}.log"
            item.write_text("u", encoding="utf-8")
            os.utime(item, (time.time() - 1000 + index, time.time() - 1000 + index))
        rotated = lr.rotate_managed_logs()
        self.assertEqual(["server"], rotated)
        kept = sorted(p.name for p in logs.glob("upd_*.log"))
        self.assertEqual([f"upd_{index:03d}.log" for index in range(5, 25)], kept)

    def test_tail_reads_from_the_end_and_redacts_secrets(self) -> None:
        log = self.root / "mac-mcp.log"
        with log.open("w", encoding="utf-8") as handle:
            for index in range(200_000):
                handle.write(f"line {index}\n")
            handle.write("Authorization: Bearer abcdefghijklmnop1234\n")
            handle.write("final api_key=sk-test-0123456789abcdefghij\n")
        with patch.object(Path, "read_text", side_effect=AssertionError("whole file read")):
            text = lr.tail_log(log, lines=3)
        self.assertIn("line 199999", text)
        self.assertNotIn("abcdefghijklmnop1234", text)
        self.assertNotIn("sk-test-0123456789abcdefghij", text)
        self.assertEqual(3, len(text.splitlines()))

    def test_logs_command_prints_redacted_tail_and_list(self) -> None:
        from mcp_server import cli

        (self.root / "mac-mcp.log").write_text("started\ntoken=abcdefgh12345678\n", encoding="utf-8")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(0, cli.main(["logs", "server", "-n", "5"]))
        self.assertIn("started", out.getvalue())
        self.assertNotIn("abcdefgh12345678", out.getvalue())
        listing = io.StringIO()
        with redirect_stdout(listing):
            self.assertEqual(0, cli.main(["logs", "--list"]))
        self.assertIn("bounds: 10 MB x 4 files per log", listing.getvalue())


class AuditLogTests(unittest.TestCase):
    def test_audit_log_rotates_and_is_owner_only(self) -> None:
        from mcp_server.security import BASE_DIR, setup_audit_logger

        logger = setup_audit_logger()
        handler = next(h for h in logger.handlers if isinstance(h, RotatingFileHandler))
        self.assertEqual(5 * 1024 * 1024, handler.maxBytes)
        self.assertEqual(3, handler.backupCount)
        self.assertEqual(0o600, stat.S_IMODE((BASE_DIR / "audit.log").stat().st_mode))

    def test_only_the_cli_managed_server_rotates_logs(self) -> None:
        main_source = (Path(__file__).resolve().parents[1] / "mcp_server" / "main.py").read_text(encoding="utf-8")
        self.assertIn('if os.getenv("MAC_MCP_MANAGED_SERVER") == "1":', main_source)
        cli_source = (Path(__file__).resolve().parents[1] / "mcp_server" / "cli.py").read_text(encoding="utf-8")
        self.assertIn('env["MAC_MCP_MANAGED_SERVER"] = "1"', cli_source)
        self.assertLess(cli_source.index("rotate_copy_truncate(LOG_FILE)"), cli_source.index('with LOG_FILE.open("a", encoding="utf-8") as log:'))
        self.assertNotIn("rotate_managed_logs()", cli_source)


if __name__ == "__main__":
    unittest.main()
