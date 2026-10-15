#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
. scripts/deb_version.sh

PYTHON="${PYTHON:-python3}"
VERSION=$("$PYTHON" -m setuptools_scm)
DEB_VERSION=$(deb_version "$VERSION")
./scripts/build_wheel.sh
WHEEL="dist/kalinka_plugin_tidal-${VERSION}-py3-none-any.whl"

# Each build owns its temporary staging directory, even in a reused checkout.
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/opt/kalinka/wheels" "$STAGE/DEBIAN"
cp "$WHEEL" "$STAGE/opt/kalinka/wheels/"
sed "s/@VERSION@/${DEB_VERSION}/g" debian/control.in > "$STAGE/DEBIAN/control"
cp debian/triggers debian/prerm "$STAGE/DEBIAN/"
chmod 755 "$STAGE/DEBIAN/prerm"
dpkg-deb --root-owner-group --build "$STAGE" "kalinka-plugin-tidal_${DEB_VERSION}_all.deb"
