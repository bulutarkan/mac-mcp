#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

MANIFEST = "release/stable-manifest.json"
SIGNATURE = "release/stable-manifest.json.sig"
EXCLUDED = {MANIFEST, SIGNATURE}
IDENTITY = "mac-mcp-release"
NAMESPACE = "mac-mcp-release"


class VerifyError(RuntimeError):
    pass


def run(cmd: list[str], *, input_bytes: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    proc = subprocess.run(cmd, input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or b"command failed").decode("utf-8", errors="replace").strip()
        raise VerifyError(detail[:1200])
    return proc


def git_bytes(repo: Path, *args: str) -> bytes:
    return run(["git", "-C", str(repo), *args]).stdout


def safe_path(value: object) -> str:
    path = str(value or "")
    if not path or path.startswith("/") or "\x00" in path:
        raise VerifyError("invalid release path")
    if any(part in {"", ".", ".."} for part in Path(path).parts):
        raise VerifyError("unsafe release path")
    return path


def verify(repo: Path, commit: str, signers: Path, branch: str) -> dict[str, object]:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise VerifyError("invalid target commit")

    manifest_bytes = git_bytes(repo, "show", f"{commit}:{MANIFEST}")
    signature_bytes = git_bytes(repo, "show", f"{commit}:{SIGNATURE}")
    with tempfile.TemporaryDirectory(prefix="mac-mcp-installer-release-") as td:
        sig_path = Path(td) / "manifest.sig"
        sig_path.write_bytes(signature_bytes)
        signature_check = run(
            [
                "/usr/bin/ssh-keygen", "-Y", "verify",
                "-f", str(signers),
                "-I", IDENTITY,
                "-n", NAMESPACE,
                "-s", str(sig_path),
            ],
            input_bytes=manifest_bytes,
            check=False,
        )
    if signature_check.returncode != 0:
        raise VerifyError("release manifest signature verification failed")

    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerifyError("invalid release manifest JSON") from exc
    if not isinstance(manifest, dict):
        raise VerifyError("release manifest must be an object")
    if manifest.get("schema_version") != 1 or manifest.get("product") != "mac-mcp" or manifest.get("channel") != "stable":
        raise VerifyError("release manifest product/channel/schema mismatch")
    if manifest.get("branch") != branch:
        raise VerifyError("release manifest branch mismatch")
    signature = manifest.get("signature")
    if not isinstance(signature, dict):
        raise VerifyError("release signature metadata missing")
    if (
        signature.get("identity") != IDENTITY
        or signature.get("namespace") != NAMESPACE
        or signature.get("algorithm") != "ssh-ed25519"
    ):
        raise VerifyError("release signature metadata mismatch")

    base = str(manifest.get("base_commit") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{40}", base):
        raise VerifyError("invalid release base commit")
    parents = git_bytes(repo, "rev-list", "--parents", "-n", "1", commit).decode("ascii").strip().split()
    if len(parents) != 2 or parents[1].lower() != base:
        raise VerifyError("release manifest base commit does not match target parent")

    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise VerifyError("release file inventory missing")
    manifest_files: dict[str, dict[str, object]] = {}
    canonical: list[dict[str, object]] = []
    for item in files:
        if not isinstance(item, dict):
            raise VerifyError("invalid release file record")
        path = safe_path(item.get("path"))
        if path in EXCLUDED or path in manifest_files:
            raise VerifyError("invalid/duplicate release file path")
        mode = str(item.get("mode") or "")
        sha = str(item.get("sha256") or "").lower()
        size = item.get("size")
        if not re.fullmatch(r"[0-7]{6}", mode):
            raise VerifyError("invalid release file mode")
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise VerifyError("invalid release file SHA-256")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise VerifyError("invalid release file size")
        row = {"path": path, "mode": mode, "size": size, "sha256": sha}
        manifest_files[path] = row
        canonical.append(row)
    if canonical != sorted(canonical, key=lambda row: str(row["path"])):
        raise VerifyError("release file inventory not canonical")

    artifacts = manifest.get("artifacts", [])
    if not isinstance(artifacts, list):
        raise VerifyError("invalid release artifact inventory")
    names: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise VerifyError("invalid release artifact record")
        name = str(artifact.get("name") or "")
        sha = str(artifact.get("sha256") or "").lower()
        size = artifact.get("size")
        if not name or "/" in name or "\\" in name or name in names:
            raise VerifyError("invalid/duplicate release artifact")
        names.add(name)
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise VerifyError("invalid release artifact SHA-256")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise VerifyError("invalid release artifact size")

    tree: dict[str, tuple[str, str]] = {}
    raw = git_bytes(repo, "ls-tree", "-r", "-z", commit)
    for entry in raw.split(b"\x00"):
        if not entry:
            continue
        meta, path_bytes = entry.split(b"\t", 1)
        mode_b, type_b, oid_b = meta.split(b" ", 2)
        path = path_bytes.decode("utf-8")
        if path in EXCLUDED:
            continue
        if type_b != b"blob":
            raise VerifyError("unsupported non-blob release entry")
        tree[path] = (mode_b.decode("ascii"), oid_b.decode("ascii"))
    if set(tree) != set(manifest_files):
        raise VerifyError("release file inventory does not match committed payload")

    aggregate = hashlib.sha256()
    pyproject: bytes | None = None
    for path in sorted(tree):
        mode, oid = tree[path]
        expected = manifest_files[path]
        if mode != expected["mode"]:
            raise VerifyError(f"release mode mismatch: {path}")
        data = git_bytes(repo, "cat-file", "blob", oid)
        sha = hashlib.sha256(data).hexdigest()
        if len(data) != expected["size"] or sha != expected["sha256"]:
            raise VerifyError(f"release SHA-256/size mismatch: {path}")
        aggregate.update(f"{mode}\0{path}\0{len(data)}\0{sha}\n".encode("utf-8"))
        if path == "pyproject.toml":
            pyproject = data
    payload = aggregate.hexdigest()
    if payload != str(manifest.get("payload_sha256") or "").lower():
        raise VerifyError("release aggregate payload digest mismatch")
    if pyproject is None:
        raise VerifyError("release payload missing pyproject.toml")
    version_match = re.search(rb'(?m)^version\s*=\s*"([^"]+)"\s*$', pyproject)
    if not version_match:
        raise VerifyError("release project version missing")
    version = version_match.group(1).decode("utf-8")
    if version != str(manifest.get("version") or ""):
        raise VerifyError("release version mismatch")
    release_id = str(manifest.get("release_id") or "").strip()
    if not release_id or len(release_id) > 160:
        raise VerifyError("invalid release id")

    return {
        "release_id": release_id,
        "version": version,
        "base_commit": base,
        "payload_sha256": payload,
        "file_count": len(tree),
        "artifact_count": len(artifacts),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--signers", required=True)
    parser.add_argument("--branch", default="main")
    args = parser.parse_args()
    try:
        result = verify(
            Path(args.repo).expanduser().resolve(),
            args.commit.strip().lower(),
            Path(args.signers).expanduser().resolve(),
            args.branch,
        )
    except (VerifyError, OSError) as exc:
        print(f"release verification failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
