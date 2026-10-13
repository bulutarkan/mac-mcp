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
            if path.name == PAGES_WORKFLOW:
                text = _without_pages_deploy_job(self, text)
            with self.subTest(workflow=path.name):
                self.assertRegex(text, r"(?m)^permissions:\n\s+contents:\s*read\s*$")
                self.assertNotRegex(text, r":\s*write\b")


# GitHub Pages can only be published with `pages: write` and `id-token: write`. The product page
# workflow (#101) is the one place allowed to ask for them, and only in a `deploy` job whose single
# step is the pinned deploy-pages action; everything else in that file stays read-only.
PAGES_WORKFLOW = "pages.yml"


def _without_pages_deploy_job(test: unittest.TestCase, text: str) -> str:
    match = re.search(r"(?ms)^  deploy:\n(.*?)(?=^  \S|\Z)", text)
    test.assertIsNotNone(match, "pages.yml must keep the write permissions inside its deploy job")
    job = match.group(1)
    permissions = re.search(r"(?m)^    permissions:\n((?:^      [^\n]*\n)+)", job)
    test.assertIsNotNone(permissions)
    test.assertEqual(["id-token: write", "pages: write"],
                     sorted(line.strip() for line in permissions.group(1).splitlines()))
    test.assertEqual(["actions/deploy-pages"],
                     [ref.split("@")[0] for ref in USES.findall(job)])
    test.assertEqual(1, len(re.findall(r"(?m)^      - ", job)), "the deploy job runs only deploy-pages")
    test.assertNotIn("run:", job)
    return text[:match.start()] + text[match.end():]


if __name__ == "__main__":
    unittest.main()
