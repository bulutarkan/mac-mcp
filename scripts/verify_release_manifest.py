#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "mcp_server" / "release_trust.py"
SPEC = importlib.util.spec_from_file_location("mac_mcp_release_trust", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise SystemExit("Could not load release trust module.")
release_trust = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = release_trust
SPEC.loader.exec_module(release_trust)


def _marker_state(commit: str) -> tuple[bool, bool]:
    import subprocess
    raw = subprocess.check_output(
        [
            "git", "-C", str(ROOT), "diff-tree", "--no-commit-id",
            "--name-only", "-r", commit, "--",
            release_trust.MANIFEST_RELPATH,
            release_trust.SIGNATURE_RELPATH,
        ],
        text=True,
    )
    paths = {line.strip() for line in raw.splitlines() if line.strip()}
    return (
        release_trust.MANIFEST_RELPATH in paths,
        release_trust.SIGNATURE_RELPATH in paths,
    )


def _latest_release(start: str, branch: str, max_commits: int = 512) -> str:
    import subprocess
    commits = subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-list", "--first-parent", f"--max-count={max_commits}", start],
        text=True,
    ).splitlines()
    for commit in commits:
        has_manifest, has_signature = _marker_state(commit)
        if not has_manifest and not has_signature:
            continue
        if has_manifest != has_signature:
            raise release_trust.ReleaseVerificationError(
                f"release marker pair is incomplete at {commit[:8]}"
            )
        release_trust.verify_release_commit(
            ROOT,
            commit,
            expected_branch=branch,
            signers_path=ROOT / "mcp_server" / release_trust.TRUSTED_SIGNERS_FILENAME,
        )
        return commit
    raise release_trust.ReleaseVerificationError(
        f"no verified stable release found within the newest {len(commits)} commits"
    )


def _require_checkpoint(commit: str) -> None:
    has_manifest, has_signature = _marker_state(commit)
    if not (has_manifest and has_signature):
        raise release_trust.ReleaseVerificationError(
            f"{commit[:8]} is not a signed stable checkpoint itself "
            "(it does not add a new signed release manifest)"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a committed Mac MCP stable release payload.")
    parser.add_argument("--commit", default=None, help="Verify this exact commit.")
    parser.add_argument("--latest-from", default=None, help="Find and verify the newest signed release at/under this revision.")
    parser.add_argument(
        "--checkpoint-at",
        default=None,
        help="Require this revision (e.g. a tag) to BE a signed stable checkpoint and verify it; "
        "a descendant of a checkpoint fails.",
    )
    parser.add_argument("--expect-version", default=None, help="Fail unless the verified release serves this version.")
    parser.add_argument("--branch", default="main")
    args = parser.parse_args()
    import subprocess

    if sum(bool(value) for value in (args.commit, args.latest_from, args.checkpoint_at)) > 1:
        parser.error("use only one of --commit, --latest-from or --checkpoint-at")
    revision = args.commit or args.latest_from or "HEAD"
    if args.checkpoint_at:
        revision = args.checkpoint_at + "^{commit}"  # an annotated tag names its commit
    resolved = subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "--verify", revision],
        text=True,
    ).strip()
    try:
        if args.checkpoint_at:
            _require_checkpoint(resolved)
        target = _latest_release(resolved, args.branch) if args.latest_from else resolved
        verified = release_trust.verify_release_commit(
            ROOT,
            target,
            expected_branch=args.branch,
            signers_path=ROOT / "mcp_server" / release_trust.TRUSTED_SIGNERS_FILENAME,
        )
        if args.expect_version is not None and verified.version != args.expect_version:
            raise release_trust.ReleaseVerificationError(
                f"{verified.release_id} at {target[:8]} serves {verified.version}, not {args.expect_version}"
            )
    except release_trust.ReleaseVerificationError as exc:
        print(f"release verification failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"verified {verified.release_id} v{verified.version} "
        f"{verified.target_commit} files={verified.file_count} "
        f"payload={verified.payload_sha256[:16]} signer={verified.signer_fingerprint or 'unknown'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
