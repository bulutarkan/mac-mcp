#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import subprocess
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


def run(cmd: list[str], *, input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    proc = subprocess.run(cmd, input=input_bytes, capture_output=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or b"command failed").decode("utf-8", errors="replace").strip()
        raise SystemExit(detail)
    return proc


def public_key_fields(private_key: Path) -> tuple[str, str]:
    public = run(["/usr/bin/ssh-keygen", "-y", "-f", str(private_key)]).stdout.decode("utf-8").strip()
    fields = public.split()
    if len(fields) < 2:
        raise SystemExit("Could not derive the release signing public key.")
    return fields[0], fields[1]


def trusted_key_present(private_key: Path) -> bool:
    key_type, key_data = public_key_fields(private_key)
    trusted = (ROOT / "mcp_server" / release_trust.TRUSTED_SIGNERS_FILENAME).read_text(encoding="utf-8")
    for raw in trusted.splitlines():
        parts = raw.split()
        if len(parts) >= 3 and parts[0] == release_trust.SIGNER_IDENTITY and parts[1] == key_type and parts[2] == key_data:
            return True
    return False


def artifact_record(spec: str) -> dict[str, object]:
    if "=" not in spec:
        raise SystemExit("--artifact must use NAME=/absolute/or/relative/path")
    name, raw_path = spec.split("=", 1)
    name = name.strip()
    path = Path(raw_path).expanduser().resolve(strict=True)
    if not name or "/" in name or "\\" in name or not path.is_file():
        raise SystemExit(f"Invalid artifact: {spec}")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return {"name": name, "size": size, "sha256": digest.hexdigest()}


def main() -> int:
    parser = argparse.ArgumentParser(description="Build and sign the Mac MCP stable release manifest from the Git index.")
    parser.add_argument("--key", required=True, help="Dedicated Ed25519 private signing key; must be outside the repository.")
    parser.add_argument("--release-id", required=True, help="Human-readable stable channel release identifier.")
    parser.add_argument("--artifact", action="append", default=[], help="Optional external artifact NAME=PATH to hash into the manifest.")
    args = parser.parse_args()

    key = Path(args.key).expanduser().resolve(strict=True)
    if not key.is_file():
        raise SystemExit("Release signing key is not a regular file.")
    try:
        key.relative_to(ROOT)
    except ValueError:
        pass
    else:
        raise SystemExit("Release signing private key must not live inside the repository.")
    if key.stat().st_mode & 0o077:
        raise SystemExit("Release signing private key must not be group/world accessible (expected mode 0600).")
    if not trusted_key_present(key):
        raise SystemExit("Release signing key is not present in the pinned trusted signer set.")

    staged = run(["git", "-C", str(ROOT), "diff", "--cached", "--name-only", "--diff-filter=U"]).stdout
    if staged.strip():
        raise SystemExit("Cannot sign a release with unresolved merge conflicts.")

    artifacts = [artifact_record(spec) for spec in args.artifact]
    manifest = release_trust.build_manifest_from_index(
        ROOT,
        release_id=args.release_id,
        artifacts=artifacts,
    )
    manifest_path = ROOT / release_trust.MANIFEST_RELPATH
    signature_path = ROOT / release_trust.SIGNATURE_RELPATH
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_bytes(release_trust.canonical_manifest_bytes(manifest))
    signature_path.unlink(missing_ok=True)

    run(
        [
            "/usr/bin/ssh-keygen",
            "-Y",
            "sign",
            "-f",
            str(key),
            "-n",
            release_trust.SIGNATURE_NAMESPACE,
            str(manifest_path),
        ]
    )
    if not signature_path.is_file():
        raise SystemExit("ssh-keygen did not produce the expected detached signature.")
    fingerprint = release_trust.verify_manifest_signature(
        manifest_path.read_bytes(),
        signature_path.read_bytes(),
        signers_path=ROOT / "mcp_server" / release_trust.TRUSTED_SIGNERS_FILENAME,
    )
    print(f"release_id={manifest['release_id']}")
    print(f"version={manifest['version']}")
    print(f"base_commit={manifest['base_commit']}")
    print(f"payload_sha256={manifest['payload_sha256']}")
    print(f"files={len(manifest['files'])}")
    print(f"artifacts={len(manifest['artifacts'])}")
    if fingerprint:
        print(f"signer={fingerprint}")
    print("Next: git add release/stable-manifest.json release/stable-manifest.json.sig")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
