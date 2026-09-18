from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.verify_security_assurance import verify_repo


VALID_MATRIX = """# Security Assurance Matrix

## SEC-TEST-001 — Fixture control

- **Risk class:** Fixture risk
- **Current status:** Verified
- **Introduced / fixed release:** 1.2.3
- **Control:** The fixture control fails closed.
- **Control paths:** `mcp_server/control.py`
- **Regression tests:** `tests/test_example.py::ExampleTests.test_control`
"""

VALID_TEST = """import unittest

class ExampleTests(unittest.TestCase):
    # ASSURANCE: SEC-TEST-001
    def test_control(self):
        self.assertTrue(True)
"""

VALID_CHANGELOG = """## Unreleased

- next

## [1.2.3] - 2026-01-01

- [SEC-TEST-001] Added the fixture control.

## [1.2.2] - 2025-12-01

- older
"""


class SecurityAssuranceVerifierTests(unittest.TestCase):
    def fixture(self) -> Path:
        root = Path(tempfile.mkdtemp(prefix="mac-mcp-assurance-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        (root / "docs").mkdir()
        (root / "scripts").mkdir()
        (root / "tests").mkdir()
        (root / "mcp_server").mkdir()
        (root / "docs/security-assurance.md").write_text(VALID_MATRIX, encoding="utf-8")
        (root / "tests/test_example.py").write_text(VALID_TEST, encoding="utf-8")
        (root / "mcp_server/control.py").write_text("CONTROL = True\n", encoding="utf-8")
        (root / "CHANGELOG.md").write_text(VALID_CHANGELOG, encoding="utf-8")
        return root

    def test_valid_matrix_links_control_test_tag_and_release(self) -> None:
        report = verify_repo(self.fixture())
        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(1, report["entries"])
        self.assertEqual(1, report["test_references"])

    def test_renamed_or_deleted_test_reference_is_rejected(self) -> None:
        root = self.fixture()
        matrix = root / "docs/security-assurance.md"
        matrix.write_text(VALID_MATRIX.replace("test_control`", "test_missing`"), encoding="utf-8")
        report = verify_repo(root)
        self.assertFalse(report["ok"])
        self.assertTrue(any("stale test reference" in item for item in report["errors"]))

    def test_control_without_regression_test_is_rejected(self) -> None:
        root = self.fixture()
        matrix = root / "docs/security-assurance.md"
        matrix.write_text(VALID_MATRIX.replace(
            "- **Regression tests:** `tests/test_example.py::ExampleTests.test_control`",
            "- **Regression tests:** none",
        ), encoding="utf-8")
        report = verify_repo(root)
        self.assertFalse(report["ok"])
        self.assertTrue(any("no regression tests declared" in item for item in report["errors"]))

    def test_assurance_tag_without_matrix_record_is_rejected(self) -> None:
        root = self.fixture()
        test_path = root / "tests/test_example.py"
        test_path.write_text(VALID_TEST + "\n# ASSURANCE: SEC-ORPHAN-999\n", encoding="utf-8")
        report = verify_repo(root)
        self.assertFalse(report["ok"])
        self.assertTrue(any("orphan assurance test tag SEC-ORPHAN-999" in item for item in report["errors"]))

    def test_missing_release_note_linkage_is_rejected(self) -> None:
        root = self.fixture()
        (root / "CHANGELOG.md").write_text(VALID_CHANGELOG.replace("[SEC-TEST-001] ", ""), encoding="utf-8")
        report = verify_repo(root)
        self.assertFalse(report["ok"])
        self.assertTrue(any("does not reference [SEC-TEST-001]" in item for item in report["errors"]))

    def test_missing_control_path_is_rejected(self) -> None:
        root = self.fixture()
        (root / "mcp_server/control.py").unlink()
        report = verify_repo(root)
        self.assertFalse(report["ok"])
        self.assertTrue(any("missing control path" in item for item in report["errors"]))

    def test_public_matrix_redaction_rejects_private_path_and_secret_like_value(self) -> None:
        root = self.fixture()
        matrix = root / "docs/security-assurance.md"
        matrix.write_text(VALID_MATRIX + "\nEvidence: /Users/example/private and sk-exampleSecretValue12345\n", encoding="utf-8")
        report = verify_repo(root)
        self.assertFalse(report["ok"])
        self.assertTrue(any("absolute user-home path" in item for item in report["errors"]))
        self.assertTrue(any("API-key-like value" in item for item in report["errors"]))


if __name__ == "__main__":
    unittest.main()
