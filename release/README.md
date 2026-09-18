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

## Key rotation

Rotation is intentionally two-phase so a target release cannot introduce and trust its own key.

1. While the old key is still trusted, add the new public key to `mcp_server/release_trusted_signers.txt`.
2. Publish that trust-store change in a release signed by the old key.
3. After installed clients have received the expanded trust store, future releases may be signed by the new key.
4. Publish a later release, signed by a still-trusted key, that removes the old public key after the migration window.
5. Keep revoked/retired private keys offline and never reintroduce them to CI.

If the active private key is suspected compromised, stop publishing updates until a trust-root recovery path has been established. Do not silently replace the pinned signer in an unsigned commit.

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
