"""#115: npm gets only the exact payload of a signed stable checkpoint. A public tag on a later,
unsigned commit must not publish, and the npm client the OIDC job runs is checked by digest."""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import mcp_server.release_trust as release_trust

ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "scripts" / "verify_release_manifest.py"
NPM_CLIENT = ROOT / "scripts" / "npm_client.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "npm-publish.yml"


def run(*args: str, cwd: Path | None = None) -> str:
    return subprocess.check_output(list(args), cwd=str(cwd) if cwd else None, text=True).strip()


class CheckpointTagTests(unittest.TestCase):
    """The workflow's gate, `verify_release_manifest.py --checkpoint-at <tag>`, on a fixture repo signed
    with a throwaway key."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="mac-mcp-npm-gate-test-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        key = self.root / "release-key"
        subprocess.check_call(["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "npm-gate-test", "-f", str(key)])
        pub = key.with_suffix(".pub").read_text(encoding="utf-8").split()

        repo = self.repo = self.root / "source"
        (repo / "scripts").mkdir(parents=True)
        (repo / "mcp_server").mkdir()
        run("git", "init", "-q", "-b", "main", cwd=repo)
        run("git", "config", "user.email", "npm-gate-test@example.com", cwd=repo)
        run("git", "config", "user.name", "npm Gate Test", cwd=repo)
        shutil.copy2(VERIFIER, repo / "scripts" / VERIFIER.name)
        shutil.copy2(ROOT / "mcp_server" / "release_trust.py", repo / "mcp_server" / "release_trust.py")
        (repo / "mcp_server" / release_trust.TRUSTED_SIGNERS_FILENAME).write_text(
            f"{release_trust.SIGNER_IDENTITY} {pub[0]} {pub[1]}\n", encoding="utf-8")
        (repo / "pyproject.toml").write_text('[project]\nname = "mac-mcp-npm-gate-test"\nversion = "3.1.0"\n', encoding="utf-8")
        (repo / "payload.txt").write_text("base\n", encoding="utf-8")
        run("git", "add", ".", cwd=repo)
        run("git", "commit", "-q", "-m", "base", cwd=repo)

        (repo / "payload.txt").write_text("signed\n", encoding="utf-8")
        run("git", "add", ".", cwd=repo)
        manifest = release_trust.build_manifest_from_index(
            repo, release_id="stable-3.1.0-r1", generated_at="2026-10-26T12:00:00Z", branch="main")
        manifest_path = repo / release_trust.MANIFEST_RELPATH
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_bytes(release_trust.canonical_manifest_bytes(manifest))
        subprocess.check_call(
            ["/usr/bin/ssh-keygen", "-Y", "sign", "-f", str(key), "-n", release_trust.SIGNATURE_NAMESPACE, str(manifest_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        run("git", "add", release_trust.MANIFEST_RELPATH, release_trust.SIGNATURE_RELPATH, cwd=repo)
        run("git", "commit", "-q", "-m", "Release 3.1.0", cwd=repo)
        self.checkpoint = run("git", "rev-parse", "HEAD", cwd=repo)
        run("git", "tag", "-a", "v3.1.0", "-m", "v3.1.0", cwd=repo)  # annotated, like a real release tag

        (repo / "payload.txt").write_text("unsigned change after the checkpoint\n", encoding="utf-8")
        run("git", "commit", "-q", "-am", "development after the checkpoint", cwd=repo)
        self.descendant = run("git", "rev-parse", "HEAD", cwd=repo)
        run("git", "tag", "v3.1.0-on-descendant", cwd=repo)

    def gate(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(self.repo / "scripts" / VERIFIER.name), *args, "--branch", "main"],
                              capture_output=True, text=True, timeout=60)

    def test_a_tag_on_the_checkpoint_commit_passes(self) -> None:
        ran = self.gate("--checkpoint-at", "v3.1.0", "--expect-version", "3.1.0")
        self.assertEqual(0, ran.returncode, ran.stderr)
        fields = ran.stdout.split()
        self.assertEqual(["verified", "stable-3.1.0-r1", "v3.1.0", self.checkpoint], fields[:4])

    def test_a_tag_on_a_descendant_of_the_checkpoint_fails(self) -> None:
        ran = self.gate("--checkpoint-at", "v3.1.0-on-descendant", "--expect-version", "3.1.0")
        self.assertEqual(1, ran.returncode)
        self.assertIn(f"{self.descendant[:8]} is not a signed stable checkpoint itself", ran.stderr)
        self.assertEqual("", ran.stdout)

    def test_the_old_history_check_alone_would_have_let_the_descendant_through(self) -> None:
        ran = self.gate("--latest-from", "v3.1.0-on-descendant", "--expect-version", "3.1.0")
        self.assertEqual(0, ran.returncode, ran.stderr)
        self.assertEqual(self.checkpoint, ran.stdout.split()[3])

    def test_a_checkpoint_for_another_version_fails(self) -> None:
        ran = self.gate("--checkpoint-at", "v3.1.0", "--expect-version", "3.1.1")
        self.assertEqual(1, ran.returncode)
        self.assertIn("serves 3.1.0, not 3.1.1", ran.stderr)

    def test_only_one_mode_at_a_time(self) -> None:
        ran = self.gate("--checkpoint-at", "v3.1.0", "--latest-from", "HEAD")
        self.assertEqual(2, ran.returncode)


class NpmClientTests(unittest.TestCase):
    def test_the_client_is_pinned_by_version_and_sha256(self) -> None:
        script = NPM_CLIENT.read_text(encoding="utf-8")
        self.assertRegex(script, r'(?m)^NPM_VERSION="\d+\.\d+\.\d+"$')
        self.assertRegex(script, r'(?m)^NPM_SHA256="[0-9a-f]{64}"$')
        self.assertNotIn("install -g", script)

    def test_a_digest_mismatch_fails_before_anything_is_unpacked(self) -> None:
        work = Path(tempfile.mkdtemp(prefix="mac-mcp-npm-client-test-"))
        self.addCleanup(shutil.rmtree, work, True)
        fake = work / "npm.tgz"
        fake.write_bytes(b"not the pinned npm tarball\n")
        dest = work / "client"
        ran = subprocess.run(["/bin/bash", str(NPM_CLIENT), str(dest), str(fake)], capture_output=True, text=True, timeout=60)
        self.assertEqual(1, ran.returncode)
        self.assertIn("digest mismatch", ran.stderr)
        self.assertEqual("", ran.stdout)
        self.assertFalse((dest / "package").exists())
        self.assertFalse((dest / "bin").exists())


class PublishWorkflowGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = WORKFLOW.read_text(encoding="utf-8")
        self.jobs = dict(re.findall(r"(?ms)^  (\w[\w-]*):\n(.*?)(?=^  \w|\Z)", self.text.split("\njobs:\n", 1)[1]))

    def test_a_publish_requires_the_tag_to_be_the_checkpoint_and_packs_that_commit(self) -> None:
        verify = self.jobs["verify"]
        self.assertIn('target="$(git rev-parse --verify "refs/tags/$REF_NAME^{commit}")"', verify)
        self.assertIn('--checkpoint-at "$target" --expect-version "$product" --branch main', verify)
        self.assertIn('echo "commit=$4" >> "$GITHUB_OUTPUT"', verify)
        self.assertIn("COMMIT: ${{ steps.checkpoint.outputs.commit }}", verify)
        self.assertIn('git archive "$COMMIT" | tar -x -C "$src"', verify)
        self.assertLess(verify.index("--checkpoint-at"), verify.index("build_npm_package.sh"))

    def test_id_token_is_only_in_the_publish_job(self) -> None:
        self.assertEqual({"verify", "publish"}, set(self.jobs))
        self.assertEqual(1, self.text.count("id-token: write"))
        self.assertNotIn("id-token", self.jobs["verify"])
        self.assertIn("    permissions:\n      contents: read\n      id-token: write\n", self.jobs["publish"])
        self.assertRegex(self.text, r"(?m)^permissions:\n  contents: read\n\n")

    def test_the_publish_job_runs_only_a_checked_npm_on_the_verified_package(self) -> None:
        publish = self.jobs["publish"]
        self.assertIn("needs: verify", publish)
        uses = [ref.split("@")[0] for ref in re.findall(r"(?m)^\s*(?:-\s+)?uses:\s*(\S+)", publish)]
        self.assertEqual(["actions/checkout", "actions/setup-node", "actions/download-artifact"], uses)
        self.assertIn("sparse-checkout: scripts/npm_client.sh", publish)
        for absent in ("unittest", "build_npm_package", "npm install", "npm pack ", "git "):
            with self.subTest(absent=absent):
                self.assertNotIn(absent, publish)
        self.assertIn('scripts/npm_client.sh "$RUNNER_TEMP/npm-client"', publish)
        self.assertIn('!= "$TARBALL_SHA256"', publish)
        self.assertNotIn("npm install -g", self.text)
        self.assertEqual(2, self.text.count('scripts/npm_client.sh "$RUNNER_TEMP/npm-client"'))

    def test_every_action_is_pinned_to_a_full_commit_sha(self) -> None:
        refs = re.findall(r"(?m)^\s*(?:-\s+)?uses:\s*(\S+)", self.text)
        self.assertTrue(refs)
        for ref in refs:
            with self.subTest(ref=ref):
                self.assertRegex(ref, r"^actions/[\w.-]+@[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()
