#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import io
import json
import re
import sys
import tokenize
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

MATRIX_REL = Path("docs/security-assurance.md")
CHANGELOG_REL = Path("CHANGELOG.md")
ENTRY_RE = re.compile(r"^##\s+(SEC-[A-Z0-9-]+)\s+—\s+(.+?)\s*$")
FIELD_RE = re.compile(r"^- \*\*(.+?):\*\*\s*(.*?)\s*$")
BACKTICK_RE = re.compile(r"`([^`]+)`")
ASSURANCE_TAG_RE = re.compile(r"ASSURANCE:\s*([^\n]+)")
ASSURANCE_ID_RE = re.compile(r"\bSEC-[A-Z0-9-]+\b")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
ALLOWED_STATUSES = {"Verified", "Monitoring", "Mitigated"}
REQUIRED_FIELDS = {
    "Risk class", "Current status", "Introduced / fixed release", "Control", "Control paths", "Regression tests",
}
SECRET_PATTERNS = (
    (re.compile(r"/Users/[^/\s]+/"), "absolute user-home path"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private-key material"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"), "API-key-like value"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._~-]{16,}\b", re.IGNORECASE), "bearer-token-like value"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}"), "JWT-like value"),
)


@dataclass(frozen=True)
class Entry:
    assurance_id: str
    title: str
    fields: dict[str, str]
    line: int

    @property
    def control_paths(self) -> list[str]:
        return [item for item in BACKTICK_RE.findall(self.fields.get("Control paths", "")) if "::" not in item]

    @property
    def tests(self) -> list[str]:
        return [item for item in BACKTICK_RE.findall(self.fields.get("Regression tests", "")) if "::" in item]


def _repo_relative(value: str) -> bool:
    path = Path(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts


def parse_matrix(text: str) -> tuple[list[Entry], list[str]]:
    entries: list[Entry] = []
    errors: list[str] = []
    current_id: str | None = None
    current_title = ""
    current_fields: dict[str, str] = {}
    current_line = 0

    def finish() -> None:
        nonlocal current_id, current_title, current_fields, current_line
        if current_id is None:
            return
        entries.append(Entry(current_id, current_title, dict(current_fields), current_line))
        current_id = None
        current_title = ""
        current_fields = {}
        current_line = 0

    for lineno, line in enumerate(text.splitlines(), start=1):
        heading = ENTRY_RE.match(line)
        if heading:
            finish()
            current_id, current_title, current_line = heading.group(1), heading.group(2), lineno
            continue
        if current_id is None:
            continue
        field = FIELD_RE.match(line)
        if field:
            name, value = field.group(1).strip(), field.group(2).strip()
            if name in current_fields:
                errors.append(f"{current_id}: duplicate field {name!r} at line {lineno}")
            current_fields[name] = value
    finish()

    seen: set[str] = set()
    for entry in entries:
        if entry.assurance_id in seen:
            errors.append(f"duplicate assurance id: {entry.assurance_id}")
        seen.add(entry.assurance_id)
        missing = sorted(REQUIRED_FIELDS - set(entry.fields))
        if missing:
            errors.append(f"{entry.assurance_id}: missing required fields: {', '.join(missing)}")
    if not entries:
        errors.append("no assurance entries found")
    return entries, errors


def _ast_test_exists(path: Path, symbol: str) -> tuple[bool, int | None]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):
        return False, None
    parts = symbol.split(".")
    if len(parts) == 1:
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == parts[0]:
                return True, node.lineno
        return False, None
    if len(parts) != 2:
        return False, None
    class_name, method_name = parts
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == method_name:
                    return True, child.lineno
    return False, None


def _comment_tokens(path: Path) -> list[tuple[int, str]]:
    try:
        text = path.read_text(encoding="utf-8")
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        return [(token.start[0], token.string) for token in tokens if token.type == tokenize.COMMENT]
    except (OSError, tokenize.TokenError):
        return []


def _test_has_assurance_tag(path: Path, lineno: int, assurance_id: str) -> bool:
    for comment_line, comment in _comment_tokens(path):
        if max(1, lineno - 4) <= comment_line < lineno and assurance_id in ASSURANCE_ID_RE.findall(comment):
            return True
    return False


def _all_test_tags(root: Path) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    tests_dir = root / "tests"
    if not tests_dir.exists():
        return found
    for path in sorted(tests_dir.rglob("*.py")):
        for _line, comment in _comment_tokens(path):
            match = ASSURANCE_TAG_RE.search(comment)
            if not match:
                continue
            for assurance_id in ASSURANCE_ID_RE.findall(match.group(1)):
                found.setdefault(assurance_id, []).append(str(path.relative_to(root)))
    return found


def _changelog_section(text: str, version: str) -> str | None:
    heading = re.compile(rf"^##\s+\[{re.escape(version)}\].*$", re.MULTILINE)
    match = heading.search(text)
    if not match:
        return None
    next_heading = re.search(r"^##\s+", text[match.end():], re.MULTILINE)
    end = match.end() + next_heading.start() if next_heading else len(text)
    return text[match.start():end]


def verify_repo(root: Path) -> dict[str, object]:
    root = Path(root).resolve()
    matrix_path = root / MATRIX_REL
    changelog_path = root / CHANGELOG_REL
    errors: list[str] = []
    if not matrix_path.is_file():
        return {"ok": False, "entries": 0, "test_references": 0, "errors": [f"missing {MATRIX_REL}"]}
    if not changelog_path.is_file():
        return {"ok": False, "entries": 0, "test_references": 0, "errors": [f"missing {CHANGELOG_REL}"]}

    matrix_text = matrix_path.read_text(encoding="utf-8")
    changelog_text = changelog_path.read_text(encoding="utf-8")
    entries, parse_errors = parse_matrix(matrix_text)
    errors.extend(parse_errors)

    for pattern, label in SECRET_PATTERNS:
        if pattern.search(matrix_text):
            errors.append(f"public matrix contains {label}")

    by_id = {entry.assurance_id: entry for entry in entries}
    total_tests = 0
    for entry in entries:
        status = entry.fields.get("Current status", "")
        if status not in ALLOWED_STATUSES:
            errors.append(f"{entry.assurance_id}: unsupported current status {status!r}")
        release = entry.fields.get("Introduced / fixed release", "")
        if not SEMVER_RE.fullmatch(release):
            errors.append(f"{entry.assurance_id}: introduced/fixed release must be X.Y.Z")
        else:
            section = _changelog_section(changelog_text, release)
            if section is None:
                errors.append(f"{entry.assurance_id}: release {release} not found in CHANGELOG.md")
            elif f"[{entry.assurance_id}]" not in section:
                errors.append(f"{entry.assurance_id}: CHANGELOG {release} section does not reference [{entry.assurance_id}]")

        control = entry.fields.get("Control", "").strip()
        if not control:
            errors.append(f"{entry.assurance_id}: control description is empty")
        paths = entry.control_paths
        if not paths:
            errors.append(f"{entry.assurance_id}: no control paths declared")
        for rel in paths:
            if not _repo_relative(rel):
                errors.append(f"{entry.assurance_id}: control path must be repository-relative: {rel}")
                continue
            if not (root / rel).is_file():
                errors.append(f"{entry.assurance_id}: missing control path: {rel}")

        tests = entry.tests
        total_tests += len(tests)
        if not tests:
            errors.append(f"{entry.assurance_id}: no regression tests declared")
        for ref in tests:
            if ref.count("::") != 1:
                errors.append(f"{entry.assurance_id}: invalid test reference: {ref}")
                continue
            rel, symbol = ref.split("::", 1)
            if not _repo_relative(rel) or not rel.startswith("tests/"):
                errors.append(f"{entry.assurance_id}: test path must be repository-relative under tests/: {rel}")
                continue
            path = root / rel
            if not path.is_file():
                errors.append(f"{entry.assurance_id}: missing test file: {rel}")
                continue
            exists, lineno = _ast_test_exists(path, symbol)
            if not exists or lineno is None:
                errors.append(f"{entry.assurance_id}: stale test reference: {ref}")
                continue
            if not _test_has_assurance_tag(path, lineno, entry.assurance_id):
                errors.append(f"{entry.assurance_id}: referenced test lacks nearby '# ASSURANCE: {entry.assurance_id}' tag: {ref}")

    tags = _all_test_tags(root)
    for assurance_id, files in sorted(tags.items()):
        if assurance_id not in by_id:
            errors.append(f"orphan assurance test tag {assurance_id}: {', '.join(sorted(set(files)))}")
    for assurance_id in sorted(by_id):
        if assurance_id not in tags:
            errors.append(f"{assurance_id}: matrix record has no ASSURANCE-tagged regression test")

    return {
        "ok": not errors,
        "matrix": str(MATRIX_REL),
        "entries": len(entries),
        "test_references": total_tests,
        "assurance_ids": sorted(by_id),
        "errors": errors,
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the public Mac MCP security assurance matrix.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json", action="store_true", help="Emit a machine-readable verification report.")
    args = parser.parse_args(list(argv) if argv is not None else None)
    report = verify_repo(args.root)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    elif report["ok"]:
        print(f"security assurance verified: {report['entries']} controls, {report['test_references']} regression references")
    else:
        for error in report["errors"]:
            print(f"ERROR: {error}", file=sys.stderr)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
