#!/usr/bin/env bash
# Vendor the ddmp library into the plugin bundle.
#
#   scripts/sync_ddmp.sh <path-to-dometic-ddmp-checkout> <git ref (tag or commit)>
#
# Copies src/ddmp/ from the given ref of the library repo into
# "Dometic CFX.indigoPlugin/Contents/Server Plugin/ddmp/" (replacing it) and records the
# ref + library version in Server Plugin/VENDORED_DDMP_VERSION. tests/test_vendored_ddmp.py
# fails when the copy and the record disagree.
set -euo pipefail

LIB="${1:?path to dometic-ddmp checkout}"
REF="${2:?git ref (tag or commit)}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$HERE/Dometic CFX.indigoPlugin/Contents/Server Plugin"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

git -C "$LIB" archive --format=tar "$REF" src/ddmp | tar -x -C "$TMP"
COMMIT="$(git -C "$LIB" rev-parse "$REF")"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$TMP/src/ddmp/__init__.py")"

rm -rf "$DEST/ddmp"
cp -R "$TMP/src/ddmp" "$DEST/ddmp"
find "$DEST/ddmp" -name '__pycache__' -type d -prune -exec rm -rf {} +
printf 'version=%s\nref=%s\ncommit=%s\n' "$VERSION" "$REF" "$COMMIT" > "$DEST/VENDORED_DDMP_VERSION"
echo "vendored ddmp $VERSION ($REF @ ${COMMIT:0:12}) into $DEST/ddmp"
