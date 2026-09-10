from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import mcp_server.tools_update as tools_update_module
import mcp_server.update_helper as update_helper_module
from mcp_server.tools_update import mac_mcp_update
from mcp_server.update_helper import UpdateError, apply_update, check_update, format_check
from mcp_server.update_state import migrate_completed_legacy_update


def run(*args: str, cwd: Path | None = None) -> str:
    return subprocess.check_output(list(args), cwd=str(cwd) if cwd else None, text=True).strip()


class UpdateHelperTests(unittest.TestCase):
    def setUp(self):
        self.update_dir = Path(tempfile.mkdtemp(prefix="mac-mcp-update-state-test-"))
        self.addCleanup(shutil.rmtree, self.update_dir, True)
        self.env_patcher = patch.dict(os.environ, {"MAC_MCP_UPDATE_DIR": str(self.update_dir)})
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)

    def test_detached_update_stages_helper_and_keeps_single_checkout_clean(self):
        root = Path(tempfile.mkdtemp(prefix="mac-mcp-detached-bootstrap-test-"))
        self.addCleanup(shutil.rmtree, root, True)
        repo = root / "checkout"
        runtime = repo
        isolated_cwd = root / "isolated-cwd"
        repo.mkdir()
        run("git", "init", "-q", "-b", "main", cwd=repo)
        run("git", "config", "user.email", "test@example.com", cwd=repo)
        run("git", "config", "user.name", "Test", cwd=repo)
        (repo / "mcp_server").mkdir()
        (repo / "mcp_server/main.py").write_text("VALUE = 'old'\n", encoding="utf-8")
        run("git", "add", ".", cwd=repo)
        run("git", "commit", "-q", "-m", "old", cwd=repo)
        remote = root / "remote.git"
        run("git", "clone", "-q", "--bare", str(repo), str(remote))
        run("git", "remote", "add", "origin", str(remote), cwd=repo)
        self.assertEqual("", run("git", "status", "--porcelain", cwd=repo))
        isolated_cwd.mkdir()
        info = SimpleNamespace(
            repo=str(repo),
            runtime=str(runtime),
            branch="main",
            remote="origin",
            deployed_commit="a" * 40,
            target_commit="b" * 40,
            behind_by=1,
            update_available=True,
            dirty=False,
        )
        captured = {}

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            kwargs["stdout"].close()
            return SimpleNamespace(pid=4242)

        with patch.dict(os.environ, {"MAC_MCP_LAUNCHD_LABEL": ""}), \
                patch("mcp_server.tools_update.resolve_paths", return_value=(repo, runtime)), \
                patch("mcp_server.tools_update.check_update", return_value=info), \
                patch("mcp_server.tools_update.subprocess.Popen", side_effect=fake_popen):
            result = mac_mcp_update(check_only=False)

        self.assertTrue(result["update_started"])
        status_path = Path(result["status_path"])
        log_path = Path(result["log_path"])
        self.assertEqual(self.update_dir / "state.json", status_path)
        self.assertTrue(status_path.is_file())
        self.assertNotIn(repo, status_path.parents)
        self.assertNotIn(runtime, status_path.parents)
        self.assertNotIn(repo, log_path.parents)
        self.assertNotIn(runtime, log_path.parents)
        self.assertFalse((repo / ".mac-mcp-update.json").exists())
        self.assertFalse((repo / ".updates").exists())
        self.assertEqual("", run("git", "status", "--porcelain", cwd=repo))
        started = json.loads(status_path.read_text(encoding="utf-8"))
        self.assertEqual("starting", started["status"])
        cmd = captured["cmd"]
        helper = Path(cmd[1])
        staged_state = helper.with_name("update_state.py")
        self.addCleanup(shutil.rmtree, helper.parent, True)
        self.assertEqual(cmd[0], sys.executable)
        self.assertTrue(helper.is_file())
        self.assertTrue(staged_state.is_file())
        self.assertEqual(helper.parent, staged_state.parent)
        self.assertNotIn(repo, helper.parents)
        self.assertNotIn(runtime, helper.parents)
        self.assertEqual(
            helper.read_bytes(),
            Path(tools_update_module.__file__).with_name("update_helper.py").read_bytes(),
        )
        self.assertEqual(
            staged_state.read_bytes(),
            Path(tools_update_module.__file__).with_name("update_state.py").read_bytes(),
        )
        self.assertEqual(captured["kwargs"]["cwd"], str(repo))
        self.assertIs(captured["kwargs"]["stdin"], subprocess.DEVNULL)
        self.assertIs(captured["kwargs"]["stderr"], subprocess.STDOUT)
        self.assertTrue(captured["kwargs"]["start_new_session"])
        self.assertTrue(captured["kwargs"]["close_fds"])
        self.assertIn("--deferred-seconds", cmd)
        self.assertEqual(["--cleanup-staging-dir", str(helper.parent)], cmd[-2:])

        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        bootstrap = subprocess.run(
            [sys.executable, "-I", str(helper), "--help"],
            cwd=str(isolated_cwd),
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(bootstrap.returncode, 0, bootstrap.stderr)
        self.assertIn("usage:", bootstrap.stdout)
        self.assertNotIn("ImportError", bootstrap.stderr)

        staged_check = subprocess.run(
            [
                sys.executable,
                "-I",
                str(helper),
                "--check",
                "--repo",
                str(repo),
                "--runtime",
                str(runtime),
                "--cleanup-staging-dir",
                str(helper.parent),
            ],
            cwd=str(isolated_cwd),
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(staged_check.returncode, 0, staged_check.stderr)
        self.assertFalse(helper.parent.exists())

        source_helper = Path(update_helper_module.__file__).resolve()
        source_check = subprocess.run(
            [
                sys.executable,
                str(source_helper),
                "--check",
                "--repo",
                str(repo),
                "--runtime",
                str(runtime),
                "--cleanup-staging-dir",
                str(source_helper.parent),
            ],
            cwd=str(isolated_cwd),
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(source_check.returncode, 0, source_check.stderr)
        self.assertTrue(source_helper.exists())
        self.assertTrue(source_helper.parent.is_dir())

    def test_handled_staged_update_failure_cleans_only_its_staging_dir(self):
        root = Path(tempfile.mkdtemp(prefix="mac-mcp-staged-failure-test-"))
        self.addCleanup(shutil.rmtree, root, True)
        staging = Path(tempfile.mkdtemp(prefix="mac-mcp-update-upd_deadbeef00-"))
        self.addCleanup(shutil.rmtree, staging, True)
        helper = staging / "update_helper.py"
        shutil.copy2(Path(update_helper_module.__file__), helper)
        shutil.copy2(Path(update_helper_module.__file__).with_name("update_state.py"), staging / "update_state.py")

        failed = subprocess.run(
            [
                sys.executable,
                "-I",
                str(helper),
                "--repo",
                str(root / "missing-repo"),
                "--runtime",
                str(root / "missing-runtime"),
                "--cleanup-staging-dir",
                str(staging),
            ],
            cwd=str(root),
            env=os.environ.copy(),
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(failed.returncode, 1)
        self.assertIn("mac-mcp update failed", failed.stderr)
        self.assertFalse(staging.exists())

    def make_fixture(self, conflict: bool = False, delete_old: bool = False):
        root = Path(tempfile.mkdtemp(prefix="mac-mcp-update-test-"))
        self.addCleanup(shutil.rmtree, root, True)
        source = root / "source"
        source.mkdir()
        run("git", "init", "-q", "-b", "main", cwd=source)
        run("git", "config", "user.email", "test@example.com", cwd=source)
        run("git", "config", "user.name", "Test", cwd=source)
        (source / "mcp_server").mkdir()
        (source / "mcp_server/main.py").write_text("VALUE = 'old'\n", encoding="utf-8")
        (source / "mcp_server/requirements.txt").write_text("", encoding="utf-8")
        (source / "mcp_server/security.py").write_text("SECURITY = True\n", encoding="utf-8")
        run("git", "add", ".", cwd=source)
        run("git", "commit", "-q", "-m", "old", cwd=source)
        old = run("git", "rev-parse", "HEAD", cwd=source)

        remote = root / "remote.git"
        run("git", "clone", "-q", "--bare", str(source), str(remote))
        (source / "mcp_server/main.py").write_text("VALUE = 'new'\nNEW_FEATURE = True\n", encoding="utf-8")
        (source / "mcp_server/new_tool.py").write_text("ENABLED = True\n", encoding="utf-8")
        if delete_old:
            (source / "mcp_server/security.py").unlink()
        run("git", "add", ".", cwd=source)
        run("git", "commit", "-q", "-m", "new", cwd=source)
        target = run("git", "rev-parse", "HEAD", cwd=source)
        run("git", "push", "-q", str(remote), "main", cwd=source)

        repo = root / "repo"
        run("git", "clone", "-q", str(remote), str(repo))
        run("git", "reset", "-q", "--hard", old, cwd=repo)
        runtime = root / "runtime"
        shutil.copytree(repo / "mcp_server", runtime / "mcp_server")
        main = runtime / "mcp_server/main.py"
        if conflict:
            main.write_text("VALUE = 'custom'\n", encoding="utf-8")
        elif not delete_old:
            security = runtime / "mcp_server/security.py"
            security.write_text(security.read_text(encoding="utf-8") + "# RUNTIME_CUSTOMIZATION\n", encoding="utf-8")
        (runtime / "mcp_server/.env").write_text("SECRET_SENTINEL=preserve-me\n", encoding="utf-8")
        return root, repo, runtime, old, target

    def test_check_and_update_preserve_runtime_overlay_and_env(self):
        _, repo, runtime, old, target = self.make_fixture()
        info = check_update(repo, runtime)
        self.assertTrue(info.update_available)
        self.assertEqual(1, info.behind_by)
        self.assertIn("Update available", format_check(info))

        result = apply_update(repo, runtime, skip_restart=True, skip_deps=True)
        self.assertTrue(result["updated"])
        self.assertEqual(target, run("git", "rev-parse", "HEAD", cwd=repo))
        text = (runtime / "mcp_server/main.py").read_text(encoding="utf-8")
        self.assertIn("VALUE = 'new'", text)
        self.assertNotIn("RUNTIME_CUSTOMIZATION", text)
        self.assertIn("RUNTIME_CUSTOMIZATION", (runtime / "mcp_server/security.py").read_text(encoding="utf-8"))
        self.assertTrue((runtime / "mcp_server/new_tool.py").exists())
        self.assertIn("preserve-me", (runtime / "mcp_server/.env").read_text(encoding="utf-8"))
        self.assertEqual(target, (self.update_dir / "deployed-commit").read_text().strip())
        self.assertTrue(Path(result["backup"]).exists())
        self.assertTrue(str(Path(result["backup"])).startswith(str(self.update_dir / "backups")))
        self.assertFalse((runtime / ".mac-mcp-deployed-commit").exists())
        self.assertFalse((runtime / ".mac-mcp-update.json").exists())
        self.assertFalse((runtime / "backups/updates").exists())

    def test_single_checkout_update_stays_clean_and_second_check_works(self):
        root, repo, _runtime, _old, target = self.make_fixture()
        # Public install shape: one Git checkout is both source repo and live runtime.
        single = root / "single"
        shutil.copytree(repo, single)
        run("git", "remote", "set-url", "origin", str(root / "remote.git"), cwd=single)

        before = check_update(single, single)
        self.assertTrue(before.update_available)
        result = apply_update(single, single, skip_restart=True, skip_deps=True)
        self.assertTrue(result["updated"])
        self.assertEqual(target, run("git", "rev-parse", "HEAD", cwd=single))
        self.assertEqual("", run("git", "status", "--porcelain", cwd=single))

        after = check_update(single, single)
        self.assertFalse(after.dirty)
        self.assertFalse(after.update_available)
        self.assertIn("up to date", format_check(after))
        self.assertTrue((self.update_dir / "deployed-commit").exists())
        self.assertTrue((self.update_dir / "state.json").exists())
        self.assertTrue((self.update_dir / "backups").exists())

    def test_health_failure_rolls_back_split_repo_runtime_and_marker(self):
        _, repo, runtime, old, target = self.make_fixture()
        with patch("mcp_server.update_helper._restart_service", return_value="http://127.0.0.1:8000/health") as restart, \
                patch("mcp_server.update_helper._health_ok", return_value=False):
            with self.assertRaisesRegex(UpdateError, "Health check failed after restart"):
                apply_update(repo, runtime, skip_deps=True)

        self.assertEqual(old, run("git", "rev-parse", "HEAD", cwd=repo))
        self.assertEqual("main", run("git", "branch", "--show-current", cwd=repo))
        self.assertEqual("", run("git", "status", "--porcelain", cwd=repo))
        self.assertEqual("VALUE = 'old'\n", (runtime / "mcp_server/main.py").read_text(encoding="utf-8"))
        self.assertIn("RUNTIME_CUSTOMIZATION", (runtime / "mcp_server/security.py").read_text(encoding="utf-8"))
        self.assertEqual(old, (self.update_dir / "deployed-commit").read_text(encoding="utf-8").strip())
        state = json.loads((self.update_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual("restored", state["repo_rollback"]["status"])
        self.assertTrue(state["repo_head_moved"])
        self.assertEqual(old, state["repo_pre_update_commit"])
        self.assertEqual(target, state["repo_post_merge_commit"])
        self.assertEqual(2, restart.call_count)
        self.assertEqual("restored", state["runtime_rollback"]["status"])

    def test_health_failure_rolls_back_single_checkout_and_keeps_it_clean(self):
        root, repo, _runtime, old, _target = self.make_fixture()
        single = root / "single-health-failure"
        shutil.copytree(repo, single)
        run("git", "remote", "set-url", "origin", str(root / "remote.git"), cwd=single)

        with patch("mcp_server.update_helper._restart_service", return_value="http://127.0.0.1:8000/health"), \
                patch("mcp_server.update_helper._health_ok", return_value=False):
            with self.assertRaisesRegex(UpdateError, "Health check failed after restart"):
                apply_update(single, single, skip_deps=True)

        self.assertEqual(old, run("git", "rev-parse", "HEAD", cwd=single))
        self.assertEqual("", run("git", "status", "--porcelain", cwd=single))
        state = json.loads((self.update_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual("restored", state["repo_rollback"]["status"])
        self.assertEqual("restored", state["runtime_rollback"]["status"])

    def test_health_failure_restores_pre_update_repo_head_not_deployed_marker(self):
        root, repo, runtime, deployed, repo_head = self.make_fixture()
        run("git", "reset", "--hard", "-q", repo_head, cwd=repo)
        (repo / "mcp_server/main.py").write_text("VALUE = 'latest'\n", encoding="utf-8")
        run("git", "add", ".", cwd=repo)
        run("git", "commit", "-q", "-m", "latest", cwd=repo)
        target = run("git", "rev-parse", "HEAD", cwd=repo)
        run("git", "push", "-q", "origin", "main", cwd=repo)
        run("git", "reset", "--hard", "-q", repo_head, cwd=repo)
        (self.update_dir / "deployed-commit").write_text(deployed + "\n", encoding="utf-8")

        with patch("mcp_server.update_helper._restart_service", return_value="http://127.0.0.1:8000/health"), \
                patch("mcp_server.update_helper._health_ok", return_value=False):
            with self.assertRaisesRegex(UpdateError, "Health check failed after restart"):
                apply_update(repo, runtime, skip_deps=True)

        self.assertEqual(repo_head, run("git", "rev-parse", "HEAD", cwd=repo))
        self.assertNotEqual(deployed, run("git", "rev-parse", "HEAD", cwd=repo))
        state = json.loads((self.update_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual("restored", state["repo_rollback"]["status"])
        self.assertEqual(repo_head, state["repo_pre_update_commit"])
        self.assertEqual(target, state["repo_post_merge_commit"])

    def test_health_failure_restores_new_and_deleted_runtime_files(self):
        _, repo, runtime, old, _target = self.make_fixture(delete_old=True)
        with patch("mcp_server.update_helper._restart_service", return_value="http://127.0.0.1:8000/health"), \
                patch("mcp_server.update_helper._health_ok", return_value=False):
            with self.assertRaisesRegex(UpdateError, "Health check failed after restart"):
                apply_update(repo, runtime, skip_deps=True)

        self.assertEqual(old, run("git", "rev-parse", "HEAD", cwd=repo))
        self.assertEqual("SECURITY = True\n", (runtime / "mcp_server/security.py").read_text(encoding="utf-8"))
        self.assertFalse((runtime / "mcp_server/new_tool.py").exists())
        state = json.loads((self.update_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual("restored", state["repo_rollback"]["status"])
        self.assertEqual("restored", state["runtime_rollback"]["status"])

    def test_health_failure_does_not_reset_when_repo_head_was_already_target(self):
        root, repo, runtime, old, target = self.make_fixture()
        run("git", "reset", "--hard", "-q", target, cwd=repo)
        (self.update_dir / "deployed-commit").write_text(old + "\n", encoding="utf-8")

        with patch.object(update_helper_module, "_run", wraps=update_helper_module._run) as run_command, \
                patch("mcp_server.update_helper._restart_service", return_value="http://127.0.0.1:8000/health"), \
                patch("mcp_server.update_helper._health_ok", return_value=False):
            with self.assertRaisesRegex(UpdateError, "Health check failed after restart"):
                apply_update(repo, runtime, skip_deps=True)

        reset_calls = [
            call.args[0]
            for call in run_command.call_args_list
            if call.args and len(call.args[0]) >= 4 and call.args[0][3] == "reset"
        ]
        self.assertEqual([], reset_calls)
        self.assertEqual(target, run("git", "rev-parse", "HEAD", cwd=repo))
        state = json.loads((self.update_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual("skipped", state["repo_rollback"]["status"])
        self.assertIn("did not move", state["repo_rollback"]["reason"])

    def test_user_edit_after_merge_skips_repo_rollback_but_restores_runtime(self):
        _, repo, runtime, old, target = self.make_fixture()
        restart_calls = 0

        def restart_with_user_edit(*_args):
            nonlocal restart_calls
            restart_calls += 1
            if restart_calls == 1:
                (repo / "mcp_server/main.py").write_text("USER_EDIT = True\n", encoding="utf-8")
            return "http://127.0.0.1:8000/health"

        with patch("mcp_server.update_helper._restart_service", side_effect=restart_with_user_edit), \
                patch("mcp_server.update_helper._health_ok", return_value=False):
            with self.assertRaisesRegex(UpdateError, "Health check failed after restart"):
                apply_update(repo, runtime, skip_deps=True)

        self.assertEqual(target, run("git", "rev-parse", "HEAD", cwd=repo))
        self.assertIn("USER_EDIT", (repo / "mcp_server/main.py").read_text(encoding="utf-8"))
        self.assertIn("mcp_server/main.py", run("git", "status", "--porcelain", cwd=repo))
        self.assertEqual("VALUE = 'old'\n", (runtime / "mcp_server/main.py").read_text(encoding="utf-8"))
        self.assertEqual(old, (self.update_dir / "deployed-commit").read_text(encoding="utf-8").strip())
        state = json.loads((self.update_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual("skipped", state["repo_rollback"]["status"])
        self.assertIn("not clean", state["repo_rollback"]["reason"])
        self.assertEqual(2, restart_calls)

    def test_untracked_file_after_merge_skips_repo_rollback(self):
        _, repo, runtime, old, target = self.make_fixture()
        restart_calls = 0

        def restart_with_untracked_file(*_args):
            nonlocal restart_calls
            restart_calls += 1
            if restart_calls == 1:
                (repo / "user-untracked.txt").write_text("preserve-me\n", encoding="utf-8")
            return "http://127.0.0.1:8000/health"

        with patch("mcp_server.update_helper._restart_service", side_effect=restart_with_untracked_file), \
                patch("mcp_server.update_helper._health_ok", return_value=False):
            with self.assertRaisesRegex(UpdateError, "Health check failed after restart"):
                apply_update(repo, runtime, skip_deps=True)

        self.assertEqual(target, run("git", "rev-parse", "HEAD", cwd=repo))
        self.assertEqual("preserve-me\n", (repo / "user-untracked.txt").read_text(encoding="utf-8"))
        self.assertEqual(old, (self.update_dir / "deployed-commit").read_text(encoding="utf-8").strip())
        state = json.loads((self.update_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual("skipped", state["repo_rollback"]["status"])
        self.assertIn("not clean", state["repo_rollback"]["reason"])

    def test_user_commit_after_merge_is_preserved(self):
        _, repo, runtime, old, target = self.make_fixture()
        restart_calls = 0
        user_commit = None

        def restart_with_user_commit(*_args):
            nonlocal restart_calls, user_commit
            restart_calls += 1
            if restart_calls == 1:
                (repo / "user-commit.txt").write_text("preserve-me\n", encoding="utf-8")
                run("git", "add", "user-commit.txt", cwd=repo)
                run("git", "commit", "-q", "-m", "user commit during update", cwd=repo)
                user_commit = run("git", "rev-parse", "HEAD", cwd=repo)
            return "http://127.0.0.1:8000/health"

        with patch("mcp_server.update_helper._restart_service", side_effect=restart_with_user_commit), \
                patch("mcp_server.update_helper._health_ok", return_value=False):
            with self.assertRaisesRegex(UpdateError, "Health check failed after restart"):
                apply_update(repo, runtime, skip_deps=True)

        self.assertIsNotNone(user_commit)
        self.assertEqual(user_commit, run("git", "rev-parse", "HEAD", cwd=repo))
        self.assertNotEqual(target, user_commit)
        self.assertEqual("preserve-me\n", (repo / "user-commit.txt").read_text(encoding="utf-8"))
        self.assertEqual("", run("git", "status", "--porcelain", cwd=repo))
        self.assertEqual(old, (self.update_dir / "deployed-commit").read_text(encoding="utf-8").strip())
        state = json.loads((self.update_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual("skipped", state["repo_rollback"]["status"])
        self.assertIn("no longer matches", state["repo_rollback"]["reason"])

    def test_same_checkout_user_edit_is_not_overwritten_when_repo_rollback_skips(self):
        root, repo, _runtime, old, target = self.make_fixture()
        single = root / "single-user-edit"
        shutil.copytree(repo, single)
        run("git", "remote", "set-url", "origin", str(root / "remote.git"), cwd=single)
        restart_calls = 0

        def restart_with_user_edit(*_args):
            nonlocal restart_calls
            restart_calls += 1
            if restart_calls == 1:
                (single / "mcp_server/main.py").write_text("USER_EDIT = True\n", encoding="utf-8")
            return "http://127.0.0.1:8000/health"

        with patch("mcp_server.update_helper._restart_service", side_effect=restart_with_user_edit), \
                patch("mcp_server.update_helper._health_ok", return_value=False):
            with self.assertRaisesRegex(UpdateError, "Health check failed after restart"):
                apply_update(single, single, skip_deps=True)

        self.assertEqual(target, run("git", "rev-parse", "HEAD", cwd=single))
        self.assertEqual("USER_EDIT = True\n", (single / "mcp_server/main.py").read_text(encoding="utf-8"))
        self.assertIn("mcp_server/main.py", run("git", "status", "--porcelain", cwd=single))
        state = json.loads((self.update_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual("skipped", state["repo_rollback"]["status"])
        self.assertEqual("skipped", state["runtime_rollback"]["status"])

    def test_completed_legacy_state_moves_out_of_checkout(self):
        runtime = Path(tempfile.mkdtemp(prefix="mac-mcp-legacy-update-test-"))
        self.addCleanup(shutil.rmtree, runtime, True)
        commit = "a" * 40
        (runtime / ".mac-mcp-deployed-commit").write_text(commit + "\n", encoding="utf-8")
        (runtime / ".mac-mcp-update.json").write_text(json.dumps({"status": "completed", "to_commit": commit}) + "\n", encoding="utf-8")
        old_backup = runtime / "backups" / "updates" / "legacy-backup"
        old_backup.mkdir(parents=True)
        (old_backup / "manifest.json").write_text("{}\n", encoding="utf-8")

        self.assertTrue(migrate_completed_legacy_update(runtime))
        self.assertFalse((runtime / ".mac-mcp-deployed-commit").exists())
        self.assertFalse((runtime / ".mac-mcp-update.json").exists())
        self.assertFalse((runtime / "backups").exists())
        self.assertEqual(commit, (self.update_dir / "deployed-commit").read_text().strip())
        self.assertTrue((self.update_dir / "backups" / "legacy-backup" / "manifest.json").exists())

    def test_conflicting_runtime_overlay_aborts_before_repo_or_runtime_change(self):
        _, repo, runtime, old, _ = self.make_fixture(conflict=True)
        before = (runtime / "mcp_server/main.py").read_text(encoding="utf-8")
        with self.assertRaises(UpdateError):
            apply_update(repo, runtime, skip_restart=True, skip_deps=True)
        self.assertEqual(old, run("git", "rev-parse", "HEAD", cwd=repo))
        self.assertEqual(before, (runtime / "mcp_server/main.py").read_text(encoding="utf-8"))
        self.assertFalse((self.update_dir / "backups").exists())
        run("git", "worktree", "prune", cwd=repo)

    def test_dirty_repo_is_reported(self):
        _, repo, runtime, _, _ = self.make_fixture()
        (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        info = check_update(repo, runtime)
        self.assertTrue(info.dirty)
        self.assertIn("Update blocked", format_check(info))


if __name__ == "__main__":
    unittest.main()
