"""#102: edit_file on a file that is not UTF-8 says so instead of an internal error.

read_file shows such a file (with replacement characters), so an agent could try to edit it and got a
bare UnicodeDecodeError. Writing the replacement characters back would also have changed its bytes.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from mcp_server import tools_files
from mcp_server.security import load_settings


class EditFileEncodingTests(unittest.TestCase):
    def test_a_file_that_is_not_utf8_is_refused_and_left_alone(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "latin1.txt"
            path.write_bytes(b"caf\xe9 menu\n")
            with self.assertRaises(HTTPException) as refused:
                tools_files.edit_file(load_settings(), str(path), "menu", "list")
            self.assertEqual(400, refused.exception.status_code)
            self.assertEqual("not_utf8_text", refused.exception.detail["error"])
            self.assertIn("byte 3", refused.exception.detail["message"])
            self.assertEqual(b"caf\xe9 menu\n", path.read_bytes())

    def test_utf8_text_is_still_edited(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "menu.txt"
            path.write_text("café menu\n", encoding="utf-8")
            self.assertEqual(1, tools_files.edit_file(load_settings(), str(path), "menu", "list")["replacements"])
            self.assertEqual("café list\n", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
