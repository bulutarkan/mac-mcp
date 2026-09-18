from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import mcp_server.release_trust as release_trust

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"
BOOTSTRAP_VERIFIER = ROOT / "scripts/installer_release_verify.py"


def run(*args: str, cwd: Path | None = None) -> str:
    return subprocess.check_output(list(args), cwd=str(cwd) if cwd else None, text=True).strip()


class InstallerReleaseChannelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="mac-mcp-installer-release-test-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.signing_key = self.root / "release-key"
        subprocess.check_call(
            [
                "/usr/bin/ssh-keygen", "-q", "-t", "ed25519",
                "-N", "", "-C", "installer-release-test", "-f", str(self.signing_key),
            ]
        )
        pub = self.signing_key.with_suffix(".pub").read_text(encoding="utf-8").split()
        self.signer_line = f"{release_trust.SIGNER_IDENTITY} {pub[0]} {pub[1]}"
        self.verifier_sha = hashlib.sha256(BOOTSTRAP_VERIFIER.read_bytes()).hexdigest()

    def sign_index(self, repo: Path, release_id: str = "installer-stable") -> None:
        manifest = release_trust.build_manifest_from_index(
            repo,
            release_id=release_id,
            generated_at="2026-09-18T12:00:00Z",
            branch="main",
        )
        manifest_path = repo / release_trust.MANIFEST_RELPATH
        signature_path = repo / release_trust.SIGNATURE_RELPATH
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_bytes(release_trust.canonical_manifest_bytes(manifest))
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

    def make_repo(
        self,
        *,
        dev_after_release: bool = False,
        corrupt_signature: bool = False,
    ) -> tuple[Path, str, str]:
        repo = self.root / "source"
        repo.mkdir()
        run("git", "init", "-q", "-b", "main", cwd=repo)
        run("git", "config", "user.email", "installer-test@example.com", cwd=repo)
        run("git", "config", "user.name", "Installer Test", cwd=repo)
        (repo / "mcp_server").mkdir()
        (repo / "scripts").mkdir()
        shutil.copy2(BOOTSTRAP_VERIFIER, repo / "scripts/installer_release_verify.py")
        (repo / "mcp_server/main.py").write_text("VALUE = 'base'\n", encoding="utf-8")
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "mac-mcp-installer-test"\nversion = "3.0.0"\n',
            encoding="utf-8",
        )
        run("git", "add", ".", cwd=repo)
        run("git", "commit", "-q", "-m", "base", cwd=repo)

        (repo / "mcp_server/main.py").write_text("VALUE = 'signed-release'\n", encoding="utf-8")
        run("git", "add", ".", cwd=repo)
        self.sign_index(repo)
        if corrupt_signature:
            sig = repo / release_trust.SIGNATURE_RELPATH
            data = bytearray(sig.read_bytes())
            data[len(data) // 2] ^= 1
            sig.write_bytes(bytes(data))
            run("git", "add", release_trust.SIGNATURE_RELPATH, cwd=repo)
        run("git", "commit", "-q", "-m", "signed stable release", cwd=repo)
        signed = run("git", "rev-parse", "HEAD", cwd=repo)

        if dev_after_release:
            (repo / "DEV_ONLY.txt").write_text("not part of stable release\n", encoding="utf-8")
            run("git", "add", "DEV_ONLY.txt", cwd=repo)
            run("git", "commit", "-q", "-m", "development after release", cwd=repo)
        tip = run("git", "rev-parse", "HEAD", cwd=repo)
        return repo, signed, tip

    def bash(self, body: str) -> subprocess.CompletedProcess[str]:
        install_tmp = self.root / "install-tmp"
        install_tmp.mkdir(exist_ok=True)
        prefix = (
            f'export MAC_MCP_INSTALLER_LIBRARY_ONLY=1; source "{INSTALLER}"; '
            f'INSTALL_TMP="{install_tmp}"; '
            f'GIT_BIN="$(command -v git)"; PYTHON_BIN="{sys.executable}"; BRANCH=main; '
            f'RELEASE_TRUSTED_SIGNER="{self.signer_line}"; '
            f'RELEASE_BOOTSTRAP_VERIFIER_SHA256="{self.verifier_sha}"; '
        )
        return subprocess.run(
            ["/bin/bash", "-c", prefix + body],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_selects_signed_release_beneath_unsigned_development_tip(self) -> None:
        repo, signed, tip = self.make_repo(dev_after_release=True)
        self.assertNotEqual(signed, tip)
        proc = self.bash(
            f'select_verified_release_commit "{repo}" "{tip}"; '
            'printf "\nRESULT=%s|%s|%s\n" "$VERIFIED_RELEASE_COMMIT" "$VERIFIED_RELEASE_ID" "$VERIFIED_RELEASE_VERSION"'
        )
        self.assertEqual(0, proc.returncode, proc.stderr)
        result = next(line for line in proc.stdout.splitlines() if line.startswith("RESULT="))
        self.assertEqual(f"RESULT={signed}|installer-stable|3.0.0", result)

    def test_tampered_signature_blocks_selection(self) -> None:
        repo, _signed, tip = self.make_repo(corrupt_signature=True)
        proc = self.bash(f'select_verified_release_commit "{repo}" "{tip}"')
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("signed release verification failed", (proc.stdout + proc.stderr).lower())

    def test_wrong_pinned_verifier_hash_blocks_before_execution(self) -> None:
        repo, _signed, tip = self.make_repo()
        proc = self.bash(
            'RELEASE_BOOTSTRAP_VERIFIER_SHA256="' + ("0" * 64) + '"; '
            f'select_verified_release_commit "{repo}" "{tip}"'
        )
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("verifier hash mismatch", (proc.stdout + proc.stderr).lower())

    def test_clone_installs_verified_release_not_development_tip(self) -> None:
        repo, signed, tip = self.make_repo(dev_after_release=True)
        remote = self.root / "remote.git"
        run("git", "clone", "-q", "--bare", str(repo), str(remote))
        source_dir = self.root / "installed-source"
        runtime_dir = self.root / "installed-runtime"
        proc = self.bash(
            f'REPO_URL="{remote}"; SOURCE_DIR="{source_dir}"; RUNTIME_DIR="{runtime_dir}"; '
            f'clone_source_and_runtime; printf "\nRESULT=%s\n" "$INSTALLED_COMMIT"'
        )
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertTrue(source_dir.is_dir())
        self.assertTrue(runtime_dir.is_dir())
        self.assertEqual(signed, run("git", "rev-parse", "HEAD", cwd=source_dir))
        self.assertEqual(tip, run("git", "rev-parse", "origin/main", cwd=source_dir))
        self.assertFalse((source_dir / "DEV_ONLY.txt").exists())
        self.assertFalse((runtime_dir / "DEV_ONLY.txt").exists())
        self.assertEqual(
            "VALUE = 'signed-release'\n",
            (runtime_dir / "mcp_server/main.py").read_text(encoding="utf-8"),
        )

    def test_clone_failure_leaves_source_and_runtime_absent(self) -> None:
        repo, _signed, _tip = self.make_repo(corrupt_signature=True)
        remote = self.root / "remote-bad.git"
        run("git", "clone", "-q", "--bare", str(repo), str(remote))
        source_dir = self.root / "bad-source"
        runtime_dir = self.root / "bad-runtime"
        proc = self.bash(
            f'REPO_URL="{remote}"; SOURCE_DIR="{source_dir}"; RUNTIME_DIR="{runtime_dir}"; '
            'clone_source_and_runtime'
        )
        self.assertNotEqual(0, proc.returncode)
        self.assertFalse(source_dir.exists())
        self.assertFalse(runtime_dir.exists())


if __name__ == "__main__":
    unittest.main()
