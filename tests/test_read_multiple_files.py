from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mcp_server import tools_files


class ReadMultipleFilesTests(unittest.TestCase):
    def test_records_report_truncation_and_missing_files_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            small = Path(td) / "small.txt"
            large = Path(td) / "large.txt"
            small.write_text("hello", encoding="utf-8")
            large.write_text("x" * 60_000, encoding="utf-8")
            missing = Path(td) / "missing.txt"

            result = tools_files.read_multiple_files(None, [str(small), str(missing), str(large)])

        self.assertTrue(result["ok"])
        files = result["files"]
        self.assertEqual([str(small), str(missing), str(large)], [item["path"] for item in files])
        self.assertEqual({"path": str(small), "content": "hello", "truncated": False, "status": "ok"}, files[0])
        self.assertEqual("error", files[1]["status"])
        self.assertTrue(files[2]["truncated"])
        self.assertLessEqual(len(files[2]["content"]), 50_000)


if __name__ == "__main__":
    unittest.main()
