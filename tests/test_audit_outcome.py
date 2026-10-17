"""#237: the audit log keeps each tool call's status and error code, never the error message, which can
hold paths, arguments or other caller text."""
from __future__ import annotations

import json
import unittest

from fastapi import HTTPException

from mcp_server import main


class _Collect:
    def __init__(self) -> None:
        self.lines = []

    def info(self, line: str) -> None:
        self.lines.append(json.loads(line))


class AuditOutcomeTests(unittest.TestCase):
    def run_tool(self, fn):
        logger = _Collect()
        try:
            main._log(logger, "read_file", fn)
        except HTTPException:
            pass
        return logger.lines[-1]

    def test_paths_and_messages_stay_out_of_the_audit_log(self) -> None:
        def missing():
            raise HTTPException(404, "File not found: /Users/someone/Secret Plans/salary.xlsx")
        line = self.run_tool(missing)
        self.assertEqual("error:404", line["outcome"])
        self.assertNotIn("salary", json.dumps(line))

    def test_a_leading_error_code_is_kept(self) -> None:
        def refused():
            raise HTTPException(400, "MEMORY_CONTAINS_SECRET: memories never keep passwords, sk-ant-api03-xyz")
        self.assertEqual("error:400:MEMORY_CONTAINS_SECRET", self.run_tool(refused)["outcome"])

    def test_an_unexpected_error_keeps_only_its_type(self) -> None:
        def broken():
            raise ValueError("token=abc123 leaked in a message")
        line = self.run_tool(broken)
        self.assertEqual("error:500:ValueError", line["outcome"])
        self.assertNotIn("abc123", json.dumps(line))

    def test_success_is_unchanged(self) -> None:
        self.assertEqual("ok", self.run_tool(lambda: {"ok": True})["outcome"])


if __name__ == "__main__":
    unittest.main()
