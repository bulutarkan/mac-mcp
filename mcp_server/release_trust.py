from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1
PRODUCT = "mac-mcp"
CHANNEL = "stable"
SIGNER_IDENTITY = "mac-mcp-release"
SIGNATURE_NAMESPACE = "mac-mcp-release"
MANIFEST_RELPATH = "release/stable-manifest.json"
SIGNATURE_RELPATH = "release/stable-manifest.json.sig"
TRUSTED_SIGNERS_FILENAME = "release_trusted_signers.txt"
EXCLUDED_PAYLOAD_PATHS = frozenset({MANIFEST_RELPATH, SIGNATURE_RELPATH})

_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_VERSION_RE = re.compile(rb'(?m)^version\s*=\s*"([^"]+)"\s*$')
_FINGERPRINT_RE = re.compile(r"(SHA256:[A-Za-z0-9+/=]+)")


class ReleaseVerificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class VerifiedRelease:
    release_id: str
    version: str
    base_commit: str
    target_commit: str
    branch: str
    generated_at: str
    payload_sha256: str
    signer_fingerprint: str | None
    file_count: int
    artifact_count: int

    def public_dict(self) -> dict[str, Any]:
        return {
            "release_verified": True,
            "release_id": self.release_id,
            "release_version": self.version,
            "release_base_commit": self.base_commit,
            "release_payload_sha256": self.payload_sha256,
            "release_signer_fingerprint": self.signer_fingerprint,
            "release_file_count": self.file_count,
            "release_artifact_count": self.artifact_count,
        }


def _run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    input_bytes: bytes | None = None,
    check: bool = True,
    timeout: int = 120,
) -> subprocess.CompletedProcess[bytes]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            input=input_bytes,
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseVerificationError(f"Could not run release verification command: {exc}") from exc
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or b"command failed").decode("utf-8", errors="replace").strip()
        raise ReleaseVerificationError(detail[:1200])
    return proc


def _git_bytes(repo: Path, *args: str, check: bool = True, timeout: int = 120) -> bytes:
    return _run(["git", "-C", str(repo), *args], check=check, timeout=timeout).stdout


def _git_text(repo: Path, *args: str, check: bool = True, timeout: int = 120) -> str:
    return _git_bytes(repo, *args, check=check, timeout=timeout).decode("utf-8", errors="strict").strip()


def trusted_signers_path() -> Path:
    override = os.getenv("MAC_MCP_RELEASE_TRUSTED_SIGNERS", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().with_name(TRUSTED_SIGNERS_FILENAME)


def _safe_release_path(value: Any) -> str:
    path = str(value or "")
    if not path or path.startswith("/") or "\x00" in path:
        raise ReleaseVerificationError("Release manifest contains an invalid path.")
    parts = Path(path).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise ReleaseVerificationError(f"Release manifest contains an unsafe path: {path}")
    return path


def _payload_digest(records: Iterable[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for record in records:
        line = (
            f"{record['mode']}\0{record['path']}\0{record['size']}\0{record['sha256']}\n"
        ).encode("utf-8")
        digest.update(line)
    return digest.hexdigest()


def _parse_version(pyproject_bytes: bytes) -> str:
    match = _VERSION_RE.search(pyproject_bytes)
    if not match:
        raise ReleaseVerificationError("Could not read the project version from pyproject.toml.")
    return match.group(1).decode("utf-8", errors="strict")


def _index_entries(repo: Path) -> list[tuple[str, str, str]]:
    raw = _git_bytes(repo, "ls-files", "-s", "-z")
    entries: list[tuple[str, str, str]] = []
    for item in raw.split(b"\x00"):
        if not item:
            continue
        meta, path_bytes = item.split(b"\t", 1)
        mode_b, oid_b, stage_b = meta.split(b" ", 2)
        if stage_b != b"0":
            raise ReleaseVerificationError("Release manifest cannot be built with unmerged index entries.")
        path = path_bytes.decode("utf-8", errors="strict")
        if path in EXCLUDED_PAYLOAD_PATHS:
            continue
        entries.append((path, mode_b.decode("ascii"), oid_b.decode("ascii")))
    entries.sort(key=lambda item: item[0])
    return entries


def _commit_entries(repo: Path, commit: str) -> list[tuple[str, str, str, str]]:
    raw = _git_bytes(repo, "ls-tree", "-r", "-z", commit)
    entries: list[tuple[str, str, str, str]] = []
    for item in raw.split(b"\x00"):
        if not item:
            continue
        meta, path_bytes = item.split(b"\t", 1)
        mode_b, type_b, oid_b = meta.split(b" ", 2)
        path = path_bytes.decode("utf-8", errors="strict")
        if path in EXCLUDED_PAYLOAD_PATHS:
            continue
        entries.append(
            (
                path,
                mode_b.decode("ascii"),
                type_b.decode("ascii"),
                oid_b.decode("ascii"),
            )
        )
    entries.sort(key=lambda item: item[0])
    return entries


def build_manifest_from_index(
    repo: str | Path,
    *,
    release_id: str,
    generated_at: str | None = None,
    branch: str | None = None,
    artifacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    repo_path = Path(repo).expanduser().resolve()
    if not (repo_path / ".git").exists():
        raise ReleaseVerificationError(f"Not a Git checkout: {repo_path}")
    release_id = str(release_id or "").strip()
    if not release_id or len(release_id) > 160:
        raise ReleaseVerificationError("release_id must be a non-empty string of at most 160 characters.")

    base_commit = _git_text(repo_path, "rev-parse", "HEAD")
    if not _HEX40_RE.fullmatch(base_commit):
        raise ReleaseVerificationError("Could not resolve a valid release base commit.")
    current_branch = str(branch or _git_text(repo_path, "branch", "--show-current") or "").strip()
    if not current_branch:
        raise ReleaseVerificationError("Release manifests must be built from a named Git branch.")

    records: list[dict[str, Any]] = []
    indexed_pyproject: bytes | None = None
    for path, mode, _oid in _index_entries(repo_path):
        if mode == "160000":
            raise ReleaseVerificationError(f"Git submodules are not supported in the verified release payload: {path}")
        data = _git_bytes(repo_path, "show", f":{path}")
        if path == "pyproject.toml":
            indexed_pyproject = data
        records.append(
            {
                "path": _safe_release_path(path),
                "mode": mode,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    if not records or indexed_pyproject is None:
        raise ReleaseVerificationError("Release payload is missing required tracked files.")

    artifact_rows: list[dict[str, Any]] = []
    for artifact in artifacts or []:
        name = str(artifact.get("name") or "").strip()
        sha256 = str(artifact.get("sha256") or "").strip().lower()
        size = artifact.get("size")
        if not name or "/" in name or "\\" in name:
            raise ReleaseVerificationError("Release artifact names must be plain filenames.")
        if not _SHA256_RE.fullmatch(sha256):
            raise ReleaseVerificationError(f"Invalid SHA-256 for release artifact: {name}")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ReleaseVerificationError(f"Invalid size for release artifact: {name}")
        artifact_rows.append({"name": name, "size": size, "sha256": sha256})
    artifact_rows.sort(key=lambda row: row["name"])

    timestamp = generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "product": PRODUCT,
        "channel": CHANNEL,
        "release_id": release_id,
        "version": _parse_version(indexed_pyproject),
        "generated_at": timestamp,
        "branch": current_branch,
        "base_commit": base_commit,
        "payload_sha256": _payload_digest(records),
        "files": records,
        "artifacts": artifact_rows,
        "signature": {
            "identity": SIGNER_IDENTITY,
            "namespace": SIGNATURE_NAMESPACE,
            "algorithm": "ssh-ed25519",
        },
    }
    return manifest


def canonical_manifest_bytes(manifest: dict[str, Any]) -> bytes:
    return (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def verify_manifest_signature(
    manifest_bytes: bytes,
    signature_bytes: bytes,
    *,
    signers_path: str | Path | None = None,
) -> str | None:
    trusted = Path(signers_path).expanduser().resolve() if signers_path else trusted_signers_path()
    if not trusted.is_file():
        raise ReleaseVerificationError(f"Trusted release signer file is missing: {trusted}")
    ssh_keygen = Path("/usr/bin/ssh-keygen")
    if not ssh_keygen.is_file():
        raise ReleaseVerificationError("ssh-keygen is required for verified releases but was not found.")

    with tempfile.TemporaryDirectory(prefix="mac-mcp-release-verify-") as td:
        sig_path = Path(td) / "manifest.sig"
        sig_path.write_bytes(signature_bytes)
        proc = _run(
            [
                str(ssh_keygen),
                "-Y",
                "verify",
                "-f",
                str(trusted),
                "-I",
                SIGNER_IDENTITY,
                "-n",
                SIGNATURE_NAMESPACE,
                "-s",
                str(sig_path),
            ],
            input_bytes=manifest_bytes,
            check=False,
            timeout=20,
        )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or b"signature verification failed").decode(
            "utf-8", errors="replace"
        ).strip()
        raise ReleaseVerificationError(f"Release manifest signature is invalid: {detail[:900]}")
    output = (proc.stdout + b"\n" + proc.stderr).decode("utf-8", errors="replace")
    match = _FINGERPRINT_RE.search(output)
    return match.group(1) if match else None


def _validate_manifest_shape(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise ReleaseVerificationError("Release manifest must be a JSON object.")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ReleaseVerificationError("Unsupported release manifest schema version.")
    if manifest.get("product") != PRODUCT or manifest.get("channel") != CHANNEL:
        raise ReleaseVerificationError("Release manifest product/channel does not match Mac MCP stable.")
    release_id = str(manifest.get("release_id") or "").strip()
    version = str(manifest.get("version") or "").strip()
    branch = str(manifest.get("branch") or "").strip()
    base_commit = str(manifest.get("base_commit") or "").strip().lower()
    payload_sha256 = str(manifest.get("payload_sha256") or "").strip().lower()
    generated_at = str(manifest.get("generated_at") or "").strip()
    if not release_id or len(release_id) > 160:
        raise ReleaseVerificationError("Release manifest has an invalid release_id.")
    if not version or len(version) > 80:
        raise ReleaseVerificationError("Release manifest has an invalid version.")
    if not branch or len(branch) > 120:
        raise ReleaseVerificationError("Release manifest has an invalid branch.")
    if not _HEX40_RE.fullmatch(base_commit):
        raise ReleaseVerificationError("Release manifest has an invalid base_commit.")
    if not _SHA256_RE.fullmatch(payload_sha256):
        raise ReleaseVerificationError("Release manifest has an invalid payload SHA-256.")
    if not generated_at or len(generated_at) > 80:
        raise ReleaseVerificationError("Release manifest has an invalid generated_at value.")

    signature = manifest.get("signature")
    if not isinstance(signature, dict):
        raise ReleaseVerificationError("Release manifest signature metadata is missing.")
    if signature.get("identity") != SIGNER_IDENTITY or signature.get("namespace") != SIGNATURE_NAMESPACE:
        raise ReleaseVerificationError("Release manifest signature identity/namespace is invalid.")
    if signature.get("algorithm") != "ssh-ed25519":
        raise ReleaseVerificationError("Release manifest signature algorithm is not allowed.")

    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ReleaseVerificationError("Release manifest does not contain a file inventory.")
    seen: set[str] = set()
    normalized_files: list[dict[str, Any]] = []
    for item in files:
        if not isinstance(item, dict):
            raise ReleaseVerificationError("Release manifest file entries must be JSON objects.")
        path = _safe_release_path(item.get("path"))
        if path in EXCLUDED_PAYLOAD_PATHS:
            raise ReleaseVerificationError("Release manifest must not include its own manifest/signature files.")
        if path in seen:
            raise ReleaseVerificationError(f"Release manifest contains a duplicate path: {path}")
        seen.add(path)
        mode = str(item.get("mode") or "")
        sha256 = str(item.get("sha256") or "").lower()
        size = item.get("size")
        if not re.fullmatch(r"[0-7]{6}", mode):
            raise ReleaseVerificationError(f"Invalid Git mode in release manifest: {path}")
        if not _SHA256_RE.fullmatch(sha256):
            raise ReleaseVerificationError(f"Invalid SHA-256 in release manifest: {path}")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ReleaseVerificationError(f"Invalid file size in release manifest: {path}")
        normalized_files.append({"path": path, "mode": mode, "size": size, "sha256": sha256})
    normalized_files.sort(key=lambda row: row["path"])
    if normalized_files != files:
        raise ReleaseVerificationError("Release manifest file inventory must be sorted canonically by path.")

    artifacts = manifest.get("artifacts", [])
    if not isinstance(artifacts, list):
        raise ReleaseVerificationError("Release manifest artifacts must be a JSON array.")
    artifact_names: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ReleaseVerificationError("Release artifact entries must be JSON objects.")
        name = str(artifact.get("name") or "").strip()
        sha256 = str(artifact.get("sha256") or "").strip().lower()
        size = artifact.get("size")
        if not name or "/" in name or "\\" in name or name in artifact_names:
            raise ReleaseVerificationError("Release manifest contains an invalid/duplicate artifact name.")
        artifact_names.add(name)
        if not _SHA256_RE.fullmatch(sha256):
            raise ReleaseVerificationError(f"Invalid release artifact SHA-256: {name}")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ReleaseVerificationError(f"Invalid release artifact size: {name}")

    return manifest


def verify_release_commit(
    repo: str | Path,
    target_commit: str,
    *,
    expected_branch: str | None = None,
    signers_path: str | Path | None = None,
) -> VerifiedRelease:
    repo_path = Path(repo).expanduser().resolve()
    target = str(target_commit or "").strip().lower()
    if not _HEX40_RE.fullmatch(target):
        raise ReleaseVerificationError("Target release commit is invalid.")
    if _git_text(repo_path, "cat-file", "-t", target, check=False) != "commit":
        raise ReleaseVerificationError(f"Target release commit is unavailable: {target}")

    try:
        manifest_bytes = _git_bytes(repo_path, "show", f"{target}:{MANIFEST_RELPATH}")
        signature_bytes = _git_bytes(repo_path, "show", f"{target}:{SIGNATURE_RELPATH}")
    except ReleaseVerificationError as exc:
        raise ReleaseVerificationError("Target commit does not contain a signed release manifest.") from exc

    fingerprint = verify_manifest_signature(
        manifest_bytes,
        signature_bytes,
        signers_path=signers_path,
    )
    try:
        manifest = _validate_manifest_shape(json.loads(manifest_bytes.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseVerificationError("Release manifest JSON is invalid.") from exc

    branch = str(manifest["branch"])
    if expected_branch is not None and branch != expected_branch:
        raise ReleaseVerificationError(
            f"Release manifest branch '{branch}' does not match expected branch '{expected_branch}'."
        )

    parent_line = _git_text(repo_path, "rev-list", "--parents", "-n", "1", target)
    parent_parts = parent_line.split()
    if len(parent_parts) != 2:
        raise ReleaseVerificationError("Verified release commits must have exactly one parent.")
    parent = parent_parts[1].lower()
    base_commit = str(manifest["base_commit"]).lower()
    if parent != base_commit:
        raise ReleaseVerificationError(
            f"Release manifest base commit {base_commit[:8]} does not match target parent {parent[:8]}."
        )

    manifest_files = {row["path"]: row for row in manifest["files"]}
    tree_entries = _commit_entries(repo_path, target)
    tree_files: dict[str, tuple[str, str, str]] = {}
    for path, mode, object_type, oid in tree_entries:
        if object_type != "blob":
            raise ReleaseVerificationError(f"Unsupported non-blob payload entry: {path}")
        tree_files[path] = (mode, object_type, oid)

    if set(tree_files) != set(manifest_files):
        missing = sorted(set(tree_files) - set(manifest_files))
        extra = sorted(set(manifest_files) - set(tree_files))
        detail = []
        if missing:
            detail.append("unsigned paths: " + ", ".join(missing[:8]))
        if extra:
            detail.append("manifest-only paths: " + ", ".join(extra[:8]))
        raise ReleaseVerificationError("Release file inventory mismatch (" + "; ".join(detail) + ").")

    verified_records: list[dict[str, Any]] = []
    pyproject_bytes: bytes | None = None
    for path in sorted(tree_files):
        mode, _object_type, _oid = tree_files[path]
        expected = manifest_files[path]
        if mode != expected["mode"]:
            raise ReleaseVerificationError(f"Git mode mismatch for signed release file: {path}")
        data = _git_bytes(repo_path, "show", f"{target}:{path}")
        if path == "pyproject.toml":
            pyproject_bytes = data
        digest = hashlib.sha256(data).hexdigest()
        if len(data) != expected["size"] or digest != expected["sha256"]:
            raise ReleaseVerificationError(f"SHA-256/size mismatch for signed release file: {path}")
        verified_records.append(
            {"path": path, "mode": mode, "size": len(data), "sha256": digest}
        )

    payload_sha256 = _payload_digest(verified_records)
    if payload_sha256 != manifest["payload_sha256"]:
        raise ReleaseVerificationError("Signed release aggregate payload digest does not match.")
    if pyproject_bytes is None or _parse_version(pyproject_bytes) != manifest["version"]:
        raise ReleaseVerificationError("Signed release version does not match pyproject.toml.")

    return VerifiedRelease(
        release_id=str(manifest["release_id"]),
        version=str(manifest["version"]),
        base_commit=base_commit,
        target_commit=target,
        branch=branch,
        generated_at=str(manifest["generated_at"]),
        payload_sha256=payload_sha256,
        signer_fingerprint=fingerprint,
        file_count=len(verified_records),
        artifact_count=len(manifest.get("artifacts", [])),
    )


def verify_artifact(path: str | Path, artifact: dict[str, Any]) -> None:
    target = Path(path).expanduser().resolve(strict=True)
    if not target.is_file():
        raise ReleaseVerificationError(f"Release artifact is not a regular file: {target}")
    digest = hashlib.sha256()
    size = 0
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    expected_size = artifact.get("size")
    expected_sha = str(artifact.get("sha256") or "").lower()
    if size != expected_size or digest.hexdigest() != expected_sha:
        raise ReleaseVerificationError(f"Release artifact verification failed: {target.name}")
