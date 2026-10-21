"""#59: task templates are reachable from New Task, not only from the empty list.

The Weekly report, Morning digest and Evening script templates showed only while no task existed.
New Task is now a split button: a click starts a blank task, its arrow lists the same templates (and one
for files arriving in a folder). A template only fills the form; what is still missing is said in words.
"""
from __future__ import annotations

import unittest
from pathlib import Path

PANE = (Path(__file__).resolve().parents[1] / "menu_app" / "Sources" / "ScheduledTasksPane.swift").read_text(encoding="utf-8")


def block(start: str, end: str) -> str:
    begin = PANE.index(start)
    return PANE[begin:PANE.index(end, begin)]


class TaskTemplateTests(unittest.TestCase):
    def test_new_task_offers_the_templates_whatever_the_list_holds(self) -> None:
        header = block("private var list: some View {", "if loaded, !message.isEmpty")
        self.assertIn("ForEach(TaskTemplate.all)", header)
        self.assertIn("primaryAction: {\n                        editor = TaskEditorModel()", header)
        self.assertIn('Button("Blank task")', header)
        # The empty list shows the same templates, from the same list.
        self.assertIn("ForEach(TaskTemplate.all) { template in templateButton(template) }", block(
            "private var emptyState: some View {", "private func templateButton"))

    def test_templates_fill_the_form_and_include_new_files(self) -> None:
        templates = block("struct TaskTemplate: Identifiable {", "enum TaskWhen")
        for title in ("Weekly report", "Morning digest", "Evening script", "New files in a folder"):
            self.assertIn(f'title: "{title}"', templates)
        files = templates[templates.index('id: "files"'):]
        self.assertIn("model.when = .file", files)
        self.assertNotIn("model.watchFolder =", files)  # the person picks the folder
        opener = block("private func open(_ template: TaskTemplate) {", "\n    }\n")
        self.assertIn("editor = model", opener)
        self.assertNotIn("save(", opener)

    def test_what_still_blocks_saving_is_said(self) -> None:
        needed = block("private var stillNeeded: String? {", "private var canSave: Bool {")
        for text in ("Choose the folder to watch", "Choose a script or type a command", "Say what the AI should do",
                     "Give the task a name"):
            self.assertIn(text, needed)
        self.assertIn("if !saving, let missing = stillNeeded {", PANE)


if __name__ == "__main__":
    unittest.main()
