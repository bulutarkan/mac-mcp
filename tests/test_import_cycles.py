"""#72: no new import cycles inside mcp_server, counting imports made inside functions.

A function-local import hides a cycle from module load order but not from the design:
the modules still cannot be moved or tested apart. This test builds the package import
graph from the source (module-level and local relative imports) and fails when a new
cycle appears or a known one grows. Known cycles may shrink; remove them from KNOWN once
they are gone.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "mcp_server"

# No cycle is allowed any more. Lower layers reach upper ones through providers the upper
# module registers when it loads (native_focus for tools_ui, context_handoff readers for
# tools_browser and tools_snapshot).
KNOWN: list[frozenset[str]] = []


def import_graph() -> dict[str, set[str]]:
    modules = {path.stem for path in PACKAGE.glob("*.py")}
    graph: dict[str, set[str]] = {name: set() for name in modules}
    for path in PACKAGE.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ImportFrom) or node.level < 1:
                continue
            targets = [node.module] if node.module else [alias.name for alias in node.names]
            for target in targets:
                head = (target or "").split(".")[0]
                if head in modules and head != path.stem:
                    graph[path.stem].add(head)
    return graph


def cycles(graph: dict[str, set[str]]) -> list[frozenset[str]]:
    """Strongly connected components with more than one module (Tarjan, iterative)."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    found: list[frozenset[str]] = []
    counter = 0
    for root in sorted(graph):
        if root in index:
            continue
        work = [(root, iter(sorted(graph[root])))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, children = work[-1]
            child = next(children, None)
            if child is not None:
                if child not in index:
                    index[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(sorted(graph[child]))))
                elif child in on_stack:
                    low[node] = min(low[node], index[child])
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                component = set()
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.add(member)
                    if member == node:
                        break
                if len(component) > 1:
                    found.append(frozenset(component))
    return found


class ImportCycleTests(unittest.TestCase):
    def test_no_new_or_growing_import_cycles(self) -> None:
        for component in cycles(import_graph()):
            with self.subTest(cycle=sorted(component)):
                self.assertTrue(
                    any(component <= known for known in KNOWN),
                    f"new or larger import cycle: {sorted(component)}; break it with a leaf module "
                    "or by passing the dependency in, rather than a function-local import",
                )

    def test_the_cycle_finder_sees_local_imports(self) -> None:
        graph = {"a": {"b"}, "b": {"c"}, "c": {"a"}, "d": {"a"}}
        self.assertEqual([frozenset({"a", "b", "c"})], cycles(graph))
        # change_summary no longer needs observability (sanitize_value moved to a leaf module).
        self.assertNotIn("observability", import_graph()["change_summary"])


class ProviderRegistrationTests(unittest.TestCase):
    def test_upper_modules_register_their_providers_when_they_load(self) -> None:
        from mcp_server import context_handoff, native_focus, tools_browser, tools_snapshot, tools_ui

        self.assertIs(tools_ui, native_focus._provider)
        self.assertIs(tools_browser, context_handoff._READERS["browser"])
        self.assertIs(tools_snapshot, context_handoff._READERS["snapshot"])

    def test_a_missing_provider_is_a_clear_error(self) -> None:
        from unittest.mock import patch

        from mcp_server import context_handoff, native_focus

        with patch.object(native_focus, "_provider", None), self.assertRaises(RuntimeError):
            native_focus.capture_focus_context()
        with patch.dict(context_handoff._READERS, {}, clear=True), self.assertRaises(context_handoff.HandoffError):
            context_handoff._reader("browser", "_browser_execute_js_read")


if __name__ == "__main__":
    unittest.main()
