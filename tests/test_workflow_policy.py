from __future__ import annotations

import re
import unittest
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)", re.M)


class WorkflowPolicyTests(unittest.TestCase):
    def test_actions_are_pinned_to_commit_shas(self) -> None:
        files = sorted(WORKFLOWS.glob("*.yml"))
        self.assertTrue(files)
        for path in files:
            for ref in USES.findall(path.read_text(encoding="utf-8")):
                with self.subTest(workflow=path.name, ref=ref):
                    if ref.startswith("./"):
                        continue
                    self.assertRegex(ref, r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")

    def test_checkout_steps_do_not_persist_credentials(self) -> None:
        for path in sorted(WORKFLOWS.glob("*.yml")):
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r"uses:\s*actions/checkout@\S+[^\n]*\n((?:\s{8,}.*\n)*)", text):
                with self.subTest(workflow=path.name):
                    self.assertRegex(match.group(1), r"persist-credentials:\s*false")

    def test_workflow_token_is_read_only(self) -> None:
        for path in sorted(WORKFLOWS.glob("*.yml")):
            text = path.read_text(encoding="utf-8")
            with self.subTest(workflow=path.name):
                self.assertRegex(text, r"(?m)^permissions:\n\s+contents:\s*read\s*$")
                self.assertNotRegex(text, r":\s*write\b")


if __name__ == "__main__":
    unittest.main()
