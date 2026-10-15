# Contributing

Install a supported Kalinka Player SDK and this repository's development extras
as described in the README. Run `python -m pytest` and
`python -m flake8 --select=F src tests scripts/check_package.py` before submitting
a change. Tests must not contact TIDAL or require account credentials.

Keep SDK requirements consistent in `pyproject.toml`, `module_setup.py`, Debian
control metadata, package inspection, documentation, and the plugin hub entry.

To release, update `CHANGELOG.md`, commit, and push a
`kalinka-plugin-tidal-vX.Y.Z` tag. Release CI tests Python 3.11/3.13, builds and
tests packages on all supported target systems, and publishes one
architecture-independent `.deb` with its SHA-256 checksum. Published assets are
not overwritten; changed bytes require a new version. Add the release's exact
URL, digest, size and source commit to
[kalinka-plugins](https://github.com/Kalinka-Player/kalinka-plugins).

Code is licensed under GPL-3.0-or-later. The original implementation was
developed in the KalinkaPlayer workspace; this repository maintains its
independent releases. The plugin uses tidalapi under LGPL-3.0-or-later.
