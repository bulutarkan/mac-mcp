#!/usr/bin/env python3
"""Build catalog/catalog.json from catalog/packs and sign it in the mac-mcp-catalog namespace.

Run with the runtime Python from the repository root:
    ~/mac-mcp/.venv/bin/python scripts/sign_catalog.py --key ~/.mac-mcp/release-signing/mac-mcp-release-ed25519
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcp_server import catalog, release_trust  # noqa: E402


def run(cmd: list[str]) -> subprocess.CompletedProcess[bytes]:
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise SystemExit((proc.stderr or proc.stdout or b"command failed").decode("utf-8", errors="replace").strip())
    return proc


def main() -> int:
    parser = argparse.ArgumentParser(description="Build and sign the Mac MCP skill and recipe catalog.")
    parser.add_argument("--key", required=True, help="Ed25519 private key listed in mcp_server/catalog_trusted_signers.txt; outside the repository.")
    parser.add_argument("--root", default=str(ROOT / "catalog"), help="Catalog directory (default: catalog/).")
    args = parser.parse_args()

    key = Path(args.key).expanduser().resolve(strict=True)
    try:
        key.relative_to(ROOT)
    except ValueError:
        pass
    else:
        raise SystemExit("The signing key must not live inside the repository.")
    if key.stat().st_mode & 0o077:
        raise SystemExit("The signing key must not be group/world accessible (expected mode 0600).")
    public = run(["/usr/bin/ssh-keygen", "-y", "-f", str(key)]).stdout.decode("utf-8").split()
    trusted = catalog.trusted_signers_path().read_text(encoding="utf-8").split("\n")
    if not any(line.split()[:3] == [catalog.SIGNER_IDENTITY, public[0], public[1]] for line in trusted if line.strip()):
        raise SystemExit("The signing key is not in the pinned catalog signer set.")

    root = Path(args.root).resolve()
    manifest = catalog.build_catalog(root)
    manifest_path, signature_path = root / catalog.CATALOG_FILE, root / catalog.SIGNATURE_FILE
    manifest_path.write_bytes(release_trust.canonical_manifest_bytes(manifest))
    signature_path.unlink(missing_ok=True)
    run(["/usr/bin/ssh-keygen", "-Y", "sign", "-f", str(key), "-n", catalog.SIGNATURE_NAMESPACE, str(manifest_path)])
    loaded = catalog.load_catalog(root)
    for pack in manifest["packs"]:
        catalog._pack_files(root, pack)
    print(f"packs={len(manifest['packs'])}")
    print(f"catalog_sha256={loaded['digest']}")
    print(f"signer={loaded['signer']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
