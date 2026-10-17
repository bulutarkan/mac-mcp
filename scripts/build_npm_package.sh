#!/bin/zsh
# Pack @bulutarkan/mac-mcp (packaging/npm): the npm wrapper plus a copy of this repository's
# install.sh and LICENSE, so the package installs exactly what the repository's installer does.
# Usage: scripts/build_npm_package.sh [output-dir]   (default: dist/npm). Publishing is separate:
#   npm publish dist/npm/bulutarkan-mac-mcp-<version>.tgz --access public
set -euo pipefail
ROOT="${0:A:h:h}"
PKG="${ROOT}/packaging/npm"
OUT="${1:-${ROOT}/dist/npm}"

# One version for the product and the npm package.
product="$(/usr/bin/sed -n 's/^version = "\(.*\)"$/\1/p' "${ROOT}/pyproject.toml" | /usr/bin/head -n 1)"
package="$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "${PKG}/package.json")"
[[ -n "${product}" && "${product}" == "${package}" ]] || {
  print -u2 "npm package version ${package} does not match pyproject.toml ${product}; update packaging/npm/package.json."
  exit 1
}
# The package must never run anything when installed.
/usr/bin/python3 -c 'import json,sys; sys.exit(1 if json.load(open(sys.argv[1])).get("scripts") else 0)' "${PKG}/package.json" || {
  print -u2 "packaging/npm/package.json must not have npm scripts (no install-time code)."
  exit 1
}
/bin/bash -n "${ROOT}/install.sh"
/bin/cp "${ROOT}/install.sh" "${PKG}/install.sh"
/bin/cp "${ROOT}/LICENSE" "${PKG}/LICENSE"
/bin/mkdir -p "${OUT}"
cd "${PKG}"
tarball="$(npm pack --silent --pack-destination "${OUT}")"
print "${OUT}/${tarball}"
