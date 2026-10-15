#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"
git rev-parse --git-dir >/dev/null
VERSION=$("$PYTHON" -m setuptools_scm)
echo "Building kalinka-plugin-tidal $VERSION"
"$PYTHON" -m build --wheel
test -f "dist/kalinka_plugin_tidal-${VERSION}-py3-none-any.whl"
