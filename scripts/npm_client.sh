#!/bin/bash
# Fetch the npm client the publish workflow uses and check it by digest before anything runs it.
# The tarball is pinned by version and SHA-256 (the same bytes as the registry's sha512 integrity);
# nothing is installed globally and no install script runs: npm is unpacked and called through a
# small wrapper. Prints the directory to put first on PATH.
# Usage: scripts/npm_client.sh <dest-dir> [tarball]   (tarball: an already downloaded copy to check)
set -Eeuo pipefail

NPM_VERSION="11.21.0"
NPM_SHA256="783e7c92bf73b442fb800c2d6ef3921e86da8894a700fed45140e37916877482"
NPM_URL="https://registry.npmjs.org/npm/-/npm-${NPM_VERSION}.tgz"

DEST="${1:?usage: npm_client.sh <dest-dir> [tarball]}"
mkdir -p "$DEST"
DEST="$(cd "$DEST" && pwd -P)"
TARBALL="${2:-$DEST/npm-${NPM_VERSION}.tgz}"
[[ $# -ge 2 ]] || curl -fsSL --retry 3 -o "$TARBALL" "$NPM_URL"

actual="$(shasum -a 256 "$TARBALL" | awk '{print $1}')"
if [[ "$actual" != "$NPM_SHA256" ]]; then
  echo "npm ${NPM_VERSION} digest mismatch: got ${actual}, expected ${NPM_SHA256}" >&2
  exit 1
fi

rm -rf "$DEST/package" "$DEST/bin"
tar -xzf "$TARBALL" -C "$DEST"
mkdir -p "$DEST/bin"
printf '#!/bin/sh\nexec node "%s/package/bin/npm-cli.js" "$@"\n' "$DEST" > "$DEST/bin/npm"
chmod 755 "$DEST/bin/npm"
version="$("$DEST/bin/npm" --version)"
[[ "$version" == "$NPM_VERSION" ]] || { echo "unpacked npm reports ${version}, expected ${NPM_VERSION}" >&2; exit 1; }
echo "$DEST/bin"
