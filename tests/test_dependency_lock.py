from __future__ import annotations

import re
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10, the declared floor
    tomllib = None

from mcp_server import update_helper

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "requirements.lock"


def locked_requirements() -> dict[str, tuple[str, list[str]]]:
    """name -> (version, hashes) for every requirement in the lock."""
    text = LOCK.read_text(encoding="utf-8")
    entries: dict[str, tuple[str, list[str]]] = {}
    for block in re.split(r"\n(?=[A-Za-z0-9])", text):
        match = re.match(r"([A-Za-z0-9._-]+)(?:\[[^\]]*\])?==([^ \;\n]+)", block)
        if match:
            entries[match.group(1).lower().replace("_", "-")] = (
                match.group(2), re.findall(r"--hash=sha256:[0-9a-f]{64}", block),
            )
    return entries


class DependencyLockTests(unittest.TestCase):
    def test_every_locked_package_is_hash_pinned(self) -> None:
        entries = locked_requirements()
        self.assertGreater(len(entries), 50)
        missing = [name for name, (_version, hashes) in entries.items() if not hashes]
        self.assertEqual([], missing)
        self.assertIn("setuptools", entries, "build tools are locked too")
        self.assertIn("wheel", entries)

    @unittest.skipIf(tomllib is None, "tomllib needs Python 3.11+")
    def test_lock_matches_pyproject_pins(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        entries = locked_requirements()
        for requirement in project["dependencies"]:
            name, version = re.match(r"([A-Za-z0-9._-]+)(?:\[[^\]]*\])?==(.+)", requirement).groups()
            key = name.lower().replace("_", "-")
            self.assertIn(key, entries, requirement)
            self.assertEqual(version, entries[key][0], f"{requirement} differs in requirements.lock")

    def test_legacy_requirements_file_points_at_the_lock(self) -> None:
        text = (ROOT / "mcp_server" / "requirements.txt").read_text(encoding="utf-8")
        lines = [line for line in text.splitlines() if line.strip() and not line.startswith("#")]
        self.assertEqual(["-r ../requirements.lock"], lines)

    def test_installer_installs_with_hashes(self) -> None:
        text = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn('--require-hashes \\\n      -r "$RUNTIME_DIR/requirements.lock"', text)
        self.assertIn("--no-deps --no-build-isolation", text)

    def test_updater_watches_and_installs_the_lock(self) -> None:
        with patch.object(update_helper, "_git", return_value="requirements.lock\n") as git:
            self.assertTrue(update_helper._deps_changed(ROOT, "a", "b"))
        self.assertIn("requirements.lock", git.call_args.args)
        commands = []
        with patch.object(update_helper, "_clone_dependency_environment",
                          side_effect=lambda src, dst: (dst / "bin").mkdir(parents=True) or (dst / "bin" / "python").touch()), \
             patch.object(update_helper, "_run", side_effect=lambda cmd, **kw: commands.append(cmd)):
            import tempfile
            with tempfile.TemporaryDirectory() as td:
                runtime = Path(td) / "runtime"
                (runtime / ".venv" / "bin").mkdir(parents=True)
                (runtime / ".venv" / "bin" / "python").touch()
                lock = runtime / "requirements.lock"
                lock.write_text("", encoding="utf-8")
                update_helper._prepare_dependency_environment(runtime, lock, "c" * 40)
        self.assertIn("--require-hashes", commands[-1])
        self.assertEqual(str(lock), commands[-1][-1])


if __name__ == "__main__":
    unittest.main()
