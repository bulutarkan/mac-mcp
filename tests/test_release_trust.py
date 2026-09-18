from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mcp_server.release_trust as release_trust
from mcp_server.update_helper import UpdateError, apply_update, check_update


def run(*args: str, cwd: Path | None = None) -> str:
    return subprocess.check_output(list(args), cwd=str(cwd) if cwd else None, text=True).strip()


class VerifiedReleaseChannelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="mac-mcp-release-channel-test-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.update_dir = self.root / "update-state"
        self.update_dir.mkdir()
        self.signing_key = self.root / "release-key"
        subprocess.check_call(
            [
                "/usr/bin/ssh-keygen", "-q", "-t", "ed25519",
                "-N", "", "-C", "release-channel-test", "-f", str(self.signing_key),
            ]
        )
        pub = self.signing_key.with_suffix(".pub").read_text(encoding="utf-8").split()
        self.trusted_signers = self.root / "trusted-signers"
        self.trusted_signers.write_text(
            f"{release_trust.SIGNER_IDENTITY} {pub[0]} {pub[1]}\n",
            encoding="utf-8",
        )
        self.env = patch.dict(
            os.environ,
            {
                "MAC_MCP_UPDATE_DIR": str(self.update_dir),
                "MAC_MCP_RELEASE_TRUSTED_SIGNERS": str(self.trusted_signers),
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def init_repo(self) -> tuple[Path, str]:
        repo = self.root / "source"
        repo.mkdir()
        run("git", "init", "-q", "-b", "main", cwd=repo)
        run("git", "config", "user.email", "release-test@example.com", cwd=repo)
        run("git", "config", "user.name", "Release Test", cwd=repo)
        (repo / "mcp_server").mkdir()
        (repo / "mcp_server/main.py").write_text("VALUE = 'old'\n", encoding="utf-8")
        (repo / "mcp_server/requirements.txt").write_text("", encoding="utf-8")
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "mac-mcp-test"\nversion = "9.9.9"\n',
            encoding="utf-8",
        )
        run("git", "add", ".", cwd=repo)
        run("git", "commit", "-q", "-m", "base", cwd=repo)
        return repo, run("git", "rev-parse", "HEAD", cwd=repo)

    def sign_index(self, repo: Path, release_id: str = "stable-test") -> None:
        manifest = release_trust.build_manifest_from_index(
            repo,
            release_id=release_id,
            generated_at="2026-09-18T12:00:00Z",
            branch="main",
        )
        manifest_path = repo / release_trust.MANIFEST_RELPATH
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_bytes(release_trust.canonical_manifest_bytes(manifest))
        signature_path = repo / release_trust.SIGNATURE_RELPATH
        signature_path.unlink(missing_ok=True)
        subprocess.check_call(
            [
                "/usr/bin/ssh-keygen", "-Y", "sign",
                "-f", str(self.signing_key),
                "-n", release_trust.SIGNATURE_NAMESPACE,
                str(manifest_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        run("git", "add", release_trust.MANIFEST_RELPATH, release_trust.SIGNATURE_RELPATH, cwd=repo)

    def commit_signed_release(self, repo: Path, *, release_id: str = "stable-test") -> str:
        run("git", "add", ".", cwd=repo)
        self.sign_index(repo, release_id)
        run("git", "commit", "-q", "-m", f"signed release {release_id}", cwd=repo)
        return run("git", "rev-parse", "HEAD", cwd=repo)

    def make_updater_fixture(self, target_commit: str, base_commit: str, source: Path) -> tuple[Path, Path]:
        remote = self.root / "remote.git"
        run("git", "clone", "-q", "--bare", str(source), str(remote))
        repo = self.root / "repo"
        run("git", "clone", "-q", str(remote), str(repo))
        run("git", "reset", "-q", "--hard", base_commit, cwd=repo)
        runtime = self.root / "runtime"
        shutil.copytree(repo / "mcp_server", runtime / "mcp_server")
        self.assertEqual(target_commit, run("git", "--git-dir", str(remote), "rev-parse", "main"))
        return repo, runtime

    def assert_update_blocked_without_mutation(
        self,
        source: Path,
        base_commit: str,
        target_commit: str,
        pattern: str,
    ) -> None:
        repo, runtime = self.make_updater_fixture(target_commit, base_commit, source)
        before_repo = run("git", "rev-parse", "HEAD", cwd=repo)
        before_runtime = (runtime / "mcp_server/main.py").read_bytes()
        with self.assertRaisesRegex(UpdateError, pattern):
            check_update(repo, runtime)
        self.assertEqual(before_repo, run("git", "rev-parse", "HEAD", cwd=repo))
        self.assertEqual(before_runtime, (runtime / "mcp_server/main.py").read_bytes())
        self.assertFalse((self.update_dir / "backups").exists())

    def test_valid_signed_release_verifies_and_updates(self) -> None:
        source, base = self.init_repo()
        (source / "mcp_server/main.py").write_text("VALUE = 'verified'\n", encoding="utf-8")
        target = self.commit_signed_release(source)
        verified = release_trust.verify_release_commit(
            source,
            target,
            expected_branch="main",
            signers_path=self.trusted_signers,
        )
        self.assertEqual("stable-test", verified.release_id)
        self.assertEqual("9.9.9", verified.version)
        self.assertEqual(base, verified.base_commit)
        self.assertGreater(verified.file_count, 0)

        repo, runtime = self.make_updater_fixture(target, base, source)
        info = check_update(repo, runtime)
        self.assertTrue(info.release_verified)
        self.assertEqual("stable-test", info.release_id)
        result = apply_update(repo, runtime, skip_restart=True, skip_deps=True)
        self.assertTrue(result["updated"])
        self.assertTrue(result["release_verified"])
        self.assertEqual(target, run("git", "rev-parse", "HEAD", cwd=repo))
        self.assertEqual(b"VALUE = 'verified'\n", (runtime / "mcp_server/main.py").read_bytes())

    def test_unsigned_manifest_is_blocked_before_repo_or_runtime_change(self) -> None:
        source, base = self.init_repo()
        (source / "mcp_server/main.py").write_text("VALUE = 'unsigned'\n", encoding="utf-8")
        run("git", "add", ".", cwd=source)
        manifest = release_trust.build_manifest_from_index(
            source,
            release_id="unsigned-manifest",
            generated_at="2026-09-18T12:00:00Z",
            branch="main",
        )
        manifest_path = source / release_trust.MANIFEST_RELPATH
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_bytes(release_trust.canonical_manifest_bytes(manifest))
        run("git", "add", release_trust.MANIFEST_RELPATH, cwd=source)
        run("git", "commit", "-q", "-m", "unsigned release manifest", cwd=source)
        target = run("git", "rev-parse", "HEAD", cwd=source)
        self.assert_update_blocked_without_mutation(source, base, target, "pair is incomplete")

    def test_plain_development_commit_is_ignored_until_signed_release(self) -> None:
        source, base = self.init_repo()
        (source / "mcp_server/main.py").write_text("VALUE = 'development'\n", encoding="utf-8")
        run("git", "add", ".", cwd=source)
        run("git", "commit", "-q", "-m", "ordinary development commit", cwd=source)
        tip = run("git", "rev-parse", "HEAD", cwd=source)
        repo, runtime = self.make_updater_fixture(tip, base, source)
        info = check_update(repo, runtime)
        self.assertFalse(info.update_available)
        self.assertEqual(base, info.target_commit)
        self.assertEqual(tip, info.branch_tip_commit)
        self.assertEqual(1, info.unverified_ahead)
        self.assertEqual(b"VALUE = 'old'\n", (runtime / "mcp_server/main.py").read_bytes())

    def test_tampered_signature_is_blocked_before_repo_or_runtime_change(self) -> None:
        source, base = self.init_repo()
        (source / "mcp_server/main.py").write_text("VALUE = 'signed'\n", encoding="utf-8")
        run("git", "add", ".", cwd=source)
        self.sign_index(source)
        signature = source / release_trust.SIGNATURE_RELPATH
        data = bytearray(signature.read_bytes())
        data[len(data) // 2] ^= 0x01
        signature.write_bytes(bytes(data))
        run("git", "add", release_trust.SIGNATURE_RELPATH, cwd=source)
        run("git", "commit", "-q", "-m", "tampered signature", cwd=source)
        target = run("git", "rev-parse", "HEAD", cwd=source)
        self.assert_update_blocked_without_mutation(source, base, target, "signature is invalid")

    def test_wrong_hash_in_validly_signed_manifest_is_blocked(self) -> None:
        source, base = self.init_repo()
        (source / "mcp_server/main.py").write_text("VALUE = 'wrong-hash'\n", encoding="utf-8")
        run("git", "add", ".", cwd=source)
        manifest = release_trust.build_manifest_from_index(
            source,
            release_id="wrong-hash",
            generated_at="2026-09-18T12:00:00Z",
            branch="main",
        )
        row = next(item for item in manifest["files"] if item["path"] == "mcp_server/main.py")
        row["sha256"] = "0" * 64
        # Keep aggregate digest intentionally stale too: a correctly signed manifest
        # with a false per-file hash must still fail against the committed payload.
        manifest_path = source / release_trust.MANIFEST_RELPATH
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_bytes(release_trust.canonical_manifest_bytes(manifest))
        subprocess.check_call(
            [
                "/usr/bin/ssh-keygen", "-Y", "sign",
                "-f", str(self.signing_key),
                "-n", release_trust.SIGNATURE_NAMESPACE,
                str(manifest_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        run("git", "add", release_trust.MANIFEST_RELPATH, release_trust.SIGNATURE_RELPATH, cwd=source)
        run("git", "commit", "-q", "-m", "signed wrong hash", cwd=source)
        target = run("git", "rev-parse", "HEAD", cwd=source)
        self.assert_update_blocked_without_mutation(source, base, target, "SHA-256/size mismatch")

    def test_payload_changed_after_signing_is_blocked(self) -> None:
        source, base = self.init_repo()
        (source / "mcp_server/main.py").write_text("VALUE = 'before-sign'\n", encoding="utf-8")
        run("git", "add", ".", cwd=source)
        self.sign_index(source, "tampered-payload")
        (source / "mcp_server/main.py").write_text("VALUE = 'after-sign'\n", encoding="utf-8")
        run("git", "add", "mcp_server/main.py", cwd=source)
        run("git", "commit", "-q", "-m", "payload changed after signature", cwd=source)
        target = run("git", "rev-parse", "HEAD", cwd=source)
        self.assert_update_blocked_without_mutation(source, base, target, "SHA-256/size mismatch")

    def test_base_commit_binding_blocks_manifest_replay(self) -> None:
        source, base = self.init_repo()
        (source / "mcp_server/main.py").write_text("VALUE = 'first'\n", encoding="utf-8")
        first = self.commit_signed_release(source, release_id="first")
        # A new commit that merely reuses the old manifest/signature has a different
        # parent and must be rejected even when all other payload bytes are unchanged.
        (source / "unrelated.txt").write_text("unsigned lineage extension\n", encoding="utf-8")
        run("git", "add", "unrelated.txt", cwd=source)
        run("git", "commit", "-q", "-m", "replay old manifest", cwd=source)
        replay = run("git", "rev-parse", "HEAD", cwd=source)
        self.assertNotEqual(first, replay)
        with self.assertRaisesRegex(release_trust.ReleaseVerificationError, "base commit"):
            release_trust.verify_release_commit(
                source,
                replay,
                expected_branch="main",
                signers_path=self.trusted_signers,
            )
        self.assertEqual(base, run("git", "rev-parse", f"{first}^", cwd=source))


if __name__ == "__main__":
    unittest.main()
