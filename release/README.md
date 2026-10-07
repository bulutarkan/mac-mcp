# Verified Release Channel

Mac MCP stable updates use a pinned Ed25519 trust root, a detached SSH signature, and a complete Git payload inventory.

## Security model

A stable release commit changes both `release/stable-manifest.json` and `release/stable-manifest.json.sig`. The manifest is signed with the dedicated Mac MCP release key using the SSH signature namespace `mac-mcp-release`.

The signed manifest covers every tracked file in the release commit except the manifest and detached signature themselves. For each path it records Git mode, byte size, and SHA-256, plus an aggregate payload digest. The manifest also binds to the release commit's exact single parent through `base_commit`, preventing a valid old manifest from being replayed on a different commit.

Normal development commits may exist after a stable release. They inherit the old manifest files but do not modify them, so installer/updater discovery skips them. If a commit changes only one release marker, or changes both but fails signature/hash verification, the verified channel fails closed.

The current release-signer fingerprint is:

`SHA256:0EbY9BbB3d8gvkZSHWWi1n+TBmN4btnbvfWaQ2km+78`

Keep a trusted copy of this fingerprint outside the repository if you need an independent bootstrap check.

## Signing a stable release

The private signing key must stay outside the repository and must never be added to CI, Git, runtime config, logs, or release artifacts. The local maintainer key is expected to be owner-only (`0600`).

Prepare all release changes first, then stage the complete payload. Do not create unrelated commits between signing and the release commit.

```bash
git status --short --branch
git add <all release payload files except release/stable-manifest.json*>

python3 scripts/sign_release_manifest.py \
  --key ~/.mac-mcp/release-signing/mac-mcp-release-ed25519 \
  --release-id stable-YYYYMMDD-description

git add release/stable-manifest.json release/stable-manifest.json.sig
git diff --cached --check
git commit -m "Release <description>"
python3 scripts/verify_release_manifest.py --commit HEAD --branch main
```

The signing script hashes the Git index rather than arbitrary working-tree bytes. If anything in the staged payload changes after signing, regenerate the manifest/signature before committing.

Optional external distribution artifacts can be bound into the signed manifest with repeated `--artifact NAME=PATH` arguments. Their SHA-256 and size are then part of the signed provenance record.

## Trust roots and key rotation

There are two release trust roots and one pinned bootstrap verifier:

- `install.sh:RELEASE_TRUSTED_SIGNERS` is the fresh-install bootstrap signer set. It is embedded in the installer, is not environment-overridable, and is restricted to the `mac-mcp-release` identity with `ssh-ed25519` keys.
- `mcp_server/release_trusted_signers.txt` is the runtime/updater signer set. The release signing helper also refuses to sign with a key that is absent from this file.
- Both trust stores are deliberately bounded to at most two unique release keys: the old and new signer during an overlap. Wildcard identities, duplicate keys, other key algorithms, or a third signer fail closed.
- `install.sh:RELEASE_BOOTSTRAP_VERIFIER_SHA256` pins `scripts/installer_release_verify.py`. The installer verifies that small verifier before allowing it to validate a release manifest.

Normal signing-key rotation uses a bounded overlap. A release must never introduce a key and rely on that same release being trusted by the new key.

### Normal rotation runbook

1. Generate the new Ed25519 private key outside the repository, keep it owner-only, and record its public fingerprint through an independent trusted channel.
2. Prepare an **overlap release** that adds the new public key to both `install.sh:RELEASE_TRUSTED_SIGNERS` and `mcp_server/release_trusted_signers.txt` while retaining the old key in both places.
3. Do **not** change `scripts/installer_release_verify.py` in this overlap release. An older installer pins the old verifier hash and must be able to verify the overlap release before it can obtain a newer installer. If the verifier itself needs rotation, first publish a separate old-key-signed bridge release that keeps the old verifier bytes but updates the installer for the later verifier transition.
4. Sign and publish the overlap release with the **old** key. Verify that an old-only installer accepts this release and that the installed runtime contains both trusted signers.
5. Allow a migration window for installed clients to receive the overlap release. Do not publish a new-key-only stable release before this window: lagging clients correctly fail closed on an unknown newest signer rather than falling back to an older stable release.
6. Publish the cutover release with the **new** key while both signer sets still contain old + new. Updated runtimes and the overlap installer must both accept it.
7. After the migration window, publish a release signed by the **new** key that removes the old public key from both `install.sh:RELEASE_TRUSTED_SIGNERS` and `mcp_server/release_trusted_signers.txt`.
8. Confirm that a release signed with the retired old key is rejected. Keep retired private keys offline and never reintroduce them to CI, runtime config, or release artifacts.

An installer copy from before the overlap intentionally cannot install a new-key-only release after cutover. It must first consume the old-key-signed overlap release or be replaced through an independently authenticated bootstrap path. This is fail-closed behavior, not a reason to fall back to an older release or weaken signature verification.

### Emergency compromise

If the old key is suspected compromised **before** a trustworthy overlap release has propagated, stop the stable channel. Repository history alone cannot safely bootstrap a replacement key because the compromised key could authorize the transition. Recover with an independently authenticated installer/public-key distribution path and publish the new fingerprint out of band. Never disable manifest verification, silently replace the embedded signer, or add an unbounded fallback signer.

If compromise is discovered **after** the overlap release has propagated and the new key is already trusted, publish an emergency release signed by the new key that removes the compromised key from both bootstrap and runtime signer sets. Clients that missed the overlap still require the independently authenticated bootstrap path.

### Rotation release checks

For every overlap, cutover, or retirement release:

```bash
git status --short --branch
/bin/bash -n install.sh
python3 -m unittest tests.test_installer_release_channel tests.test_release_trust -v
git diff --check
python3 scripts/verify_release_manifest.py --commit HEAD --branch main
```

Before signing, confirm the installer signer set and `mcp_server/release_trusted_signers.txt` contain exactly the intended bounded overlap. After retirement, confirm the old key is absent from both. Keep the private keys outside the repository throughout the procedure.

## Installer and updater behavior

The updater scans the first-parent history of `origin/main` for the newest valid stable-release commit after the deployed commit. Ordinary development commits are not offered as updates. A malformed or cryptographically invalid release marker blocks the channel instead of falling back to an older release.

The installer similarly finds the newest verified stable release and checks it before moving anything into the persistent source/runtime paths. It first verifies a small standalone bootstrap verifier against a SHA-256 pinned in `install.sh`, then uses the embedded public signer to verify the signed manifest and complete payload.

The existing runtime backup, overlay merge, health check, and rollback logic remains unchanged after verification succeeds.

## macOS app distribution

Local/source installs continue to use ad-hoc signing for the menu app. A public binary distribution should use an Apple Developer ID Application identity with hardened runtime and timestamping, then notarize and staple the app before publication:

```bash
MAC_MCP_CODESIGN_IDENTITY="Developer ID Application: ..." menu_app/build_app.sh /tmp/mac-mcp-release-build
codesign --verify --deep --strict --verbose=2 "/tmp/mac-mcp-release-build/Mac MCP.app"
xcrun notarytool submit <archive> --keychain-profile <profile> --wait
xcrun stapler staple "/tmp/mac-mcp-release-build/Mac MCP.app"
spctl --assess --type execute --verbose=4 "/tmp/mac-mcp-release-build/Mac MCP.app"
```

The notarized ZIP/PKG/DMG should then be included with `--artifact` when generating the signed release manifest. Apple code signing/notarization and the Mac MCP Ed25519 release signature are complementary controls: one establishes Apple-distribution provenance, the other binds the Mac MCP update payload and channel.
