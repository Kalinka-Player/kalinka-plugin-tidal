# TIDAL plugin for Kalinka Player

Uses the existing [tidalapi](https://github.com/EbbLabs/python-tidal) Python client
for authentication, catalog metadata, and stream manifests. Requires Python 3.11+
and Kalinka Player 5.9.1+ with Plugin SDK 3.11–3.x.

## Install and sign in

Download the `.deb` and `SHA256SUMS` from the
[latest release](https://github.com/Kalinka-Player/kalinka-plugin-tidal/releases/latest).
On a Debian-packaged Kalinka Player server, verify and install them:

```sh
sha256sum -c SHA256SUMS --ignore-missing
sudo apt install ./kalinka-plugin-tidal_<version>_all.deb
```

The architecture-independent package supports Debian 13 and Ubuntu 24.04 on
ARM64 and amd64. CI builds and tests the packaged wheel on all four targets.
Kalinka Player restarts to discover the plugin after installation.

Enable **TIDAL** in module settings and restart the server. Open the **Sign in to
TIDAL** link in the module's status and authorize your account in the browser.
The status becomes ready when authorization completes. Expired sign-in links
are replaced automatically. Authentication and retries happen in the background.

The plugin is disabled by default. No password or developer API key is required
in Kalinka's settings. Playback requires a TIDAL streaming subscription.

The session is stored with owner-only permissions at
`<state_dir>/tidal/session.json` (normally `/var/lib/kalinka/tidal/session.json`;
development uses `$KALINKA_PREFIX/var/lib/kalinka/tidal/session.json`). Refreshed
credentials are persisted automatically. To change accounts, stop Kalinka,
remove that file, and start it again. Existing files created by
`tidalapi.Session.save_session_to_file()` can also be used there.

## Catalogs

- **Home** and **Explore** expose the sections returned for your account and region.
- **Genres** and **Moods** open TIDAL's nested discovery pages.
- **My Mixes** exposes personalized mixes and their tracks.
- **My Collection** exposes favorite tracks, albums, artists, and your created
  and saved playlists.
- Albums and playlists expose tracks; artists expose albums, EPs and singles,
  top tracks, and similar artists. Search supports all four entity types.

Catalog IDs survive restarts. Discovery pages are cached for five minutes;
album, playlist, search, and collection listings use TIDAL's pagination, capped
at 50 items per request. Queue additions fetch successive pages up to the
server's requested limit. A timeout or service failure aborts the addition;
large cold requests may need retrying within the server's time budget.
Search follows tidalapi's 300-result limit. Discovery
shelves expose the items returned by TIDAL and a further page when TIDAL supplies
a supported `pages/` link. Videos, articles, and unsupported link types are omitted.
No catalog filters are advertised; unsupported filters are rejected.

Sections without upstream identifiers use their type and title. Ambiguous
same-title sections also use their content; a saved link expires if that
content changes, so reopening its parent is necessary. Older V1 section links
from 0.1.0 may also need to be reopened after upgrading.

TIDAL collections are read-only in Kalinka. Edit TIDAL favorites and playlists
in the TIDAL app; Kalinka's own collections can still reference TIDAL tracks.

## Playback and limitations

The plugin requests lossless audio and resolves a fresh stream URL when a track
starts playing. Direct, unencrypted FLAC and MP3 streams are supported. Segmented
DASH, encrypted streams, and AAC/MP4 streams are rejected with a readable error;
they require playback capabilities the native Kalinka renderer does not provide.
There is no transcoding or downloading of whole tracks.

tidalapi is an unofficial client. Availability depends on the account, region,
subscription, and TIDAL's API. Tests use the installed library with fake API
responses; live account sign-in and playback need verification with an account.

## Development

The SDK is installed explicitly from the Kalinka Player repository, not PyPI:

```sh
git clone https://github.com/Kalinka-Player/kalinka-plugin-tidal.git
cd kalinka-plugin-tidal
python3 -m venv .venv
. .venv/bin/activate
python -m pip install \
  "kalinka-plugin-sdk @ git+https://github.com/Kalinka-Player/KalinkaPlayer@kalinka-v5.9.1#subdirectory=packages/kalinka-plugin-sdk" \
  -e '.[dev]'
python -m pytest
./scripts/build_deb.sh
```

Versions come from `kalinka-plugin-tidal-vX.Y.Z` git tags. Development builds use
Debian `~dev` versions so they sort below the corresponding release. Releases
publish one `_all.deb` after the Python and target-platform test matrices pass.
See [CONTRIBUTING.md](CONTRIBUTING.md) for release instructions.

## License

GPL-3.0-or-later; see [LICENSE](LICENSE). The runtime dependency
[tidalapi](https://github.com/EbbLabs/python-tidal) is LGPL-3.0-or-later.
This integration is not affiliated with or endorsed by TIDAL.
