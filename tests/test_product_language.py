from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ProductLanguageTests(unittest.TestCase):
    def test_canonical_glossary_answers_security_expectation_checks(self):
        text = (ROOT / "docs" / "TERMINOLOGY.md").read_text(encoding="utf-8")
        for phrase in (
            "Visible, non-focus-stealing browser automation",
            "does **not** imply that a human confirmation prompt will appear",
            "Machine-local transport, not user isolation",
            "is **not** the raw provider session/account identifier",
            "not** a complete sandbox",
        ):
            self.assertIn(phrase, text)

    def test_ui_and_docs_use_the_same_core_semantics(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        menu_readme = (ROOT / "menu_app" / "README.md").read_text(encoding="utf-8")
        menu_view = (ROOT / "menu_app" / "Sources" / "MenuBarView.swift").read_text(encoding="utf-8")
        security = (ROOT / "docs" / "LOCAL_API_SECURITY.md").read_text(encoding="utf-8")

        self.assertIn("visible, non-focus-stealing", readme.lower())
        self.assertIn("visible, non-focus-stealing", menu_readme.lower())
        self.assertIn("Visible, non-focus-stealing browser automation", menu_view)
        self.assertIn("machine-local transport, not same-user isolation", readme.lower())
        self.assertIn("machine-local transport rather than same-user isolation", menu_readme.lower())
        self.assertIn("machine-local transport, not user isolation", security.lower())
        self.assertIn("Capability allowed = server permits it. Approval is separate", menu_view)
        self.assertIn("Mac MCP logical session", menu_view)

    def test_copy_does_not_reintroduce_known_misleading_shortcuts(self):
        corpus = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (
                ROOT / "README.md",
                ROOT / "menu_app" / "README.md",
                ROOT / "docs" / "LOCAL_API_SECURITY.md",
                ROOT / "docs" / "TERMINOLOGY.md",
            )
        ).lower()
        self.assertNotIn("localhost-only dashboard apis", corpus)
        self.assertNotIn("background = invisible", corpus)
        self.assertNotIn("standard = every write prompts", corpus)
        self.assertNotIn("localhost = secure sandbox", corpus)


if __name__ == "__main__":
    unittest.main()
