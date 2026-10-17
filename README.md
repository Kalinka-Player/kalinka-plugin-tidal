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

TIDAL is enabled by default and starts sign-in after the installation restart.
Device authorisation uses a TIDAL link and approval in your browser:

1. Open **Sign in to TIDAL** under **TIDAL account** in the plugin settings.
2. Sign in to TIDAL and approve this player.
3. Kalinka detects approval automatically and saves the session.

There is no callback URL to paste and no restart to finish signing in.
If the status still says "Connecting", reopen the Plugins screen to refresh it.
Expired links are replaced automatically. Existing valid sessions remain signed
in until you reset authorisation, including sessions from earlier browser sign-in.

No password or developer API key is required in Kalinka's settings. Playback
requires a TIDAL streaming subscription. An explicitly saved disabled setting
is respected; enable TIDAL and restart if you previously disabled it.

The session is stored with owner-only permissions at
`<state_dir>/tidal/session.json` (normally `/var/lib/kalinka/tidal/session.json`;
development uses `$KALINKA_PREFIX/var/lib/kalinka/tidal/session.json`). Refreshed
credentials are persisted automatically. Existing files created by
`tidalapi.Session.save_session_to_file()` can also be used there.

### Change accounts

In **Plugins → TIDAL**, turn on **Unlink Tidal**, then **Apply & Restart**.
The switch turns off automatically. Open **Sign in to TIDAL** to link another
account. If the browser remembers the previous account, sign out of TIDAL there
first.

For older plugin versions without this setting, stop Kalinka, delete
`<state_dir>/tidal/session.json`, and start Kalinka again. Stop it before deleting
the file so the running plugin cannot save the old session again. Forgetting an
account on this player does not sign it out on other devices.

## Catalogs

- **Home** exposes the recommendations and audio discovery sections returned
  by TIDAL for your account and region.
- **Explore**, **Genres** and **Moods** expose TIDAL's pages of the same names,
  with their shelves and the pages those link to.
- **My Collection** exposes favorite tracks, albums, artists, and your created
  and saved playlists.
- **My Mixes** exposes personalized mixes and their tracks.
- Albums and playlists expose tracks; artists expose albums, EPs and singles,
  top tracks, and similar artists. Search supports all four entity types.

Catalog IDs survive restarts. Discovery pages are cached for five minutes;
album, playlist, search, and collection listings use TIDAL's pagination, capped
at 50 items per request. Queue additions fetch successive pages up to the
server's requested limit. A timeout or service failure aborts the addition;
large cold requests may need retrying within the server's time budget.
Search combines all four entity kinds in one request, reusing the response for
five seconds to stay within the server's call budget. It follows tidalapi's
300-result limit. Discovery
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

**Maximum audio quality** offers:

- **Lossless — 16-bit / 44.1 kHz**: requests `LOSSLESS`.
- **Max — up to 24-bit / 192 kHz** (default): requests `HI_RES_LOSSLESS`.

Apply and restart after changing quality. This controls the quality requested
from TIDAL, not the format of the renderer's output. TIDAL chooses the actual
codec, container and resolution for the track and session; the API does not offer
an independent FLAC/MP3/WAV output-format selector. Device authorisation may
return AAC even for a lossless request; such tracks play as AAC.

The plugin resolves a fresh stream URL when a track starts playing. Direct,
unencrypted FLAC, MP3 and MP4 streams are supported; MP4 carries AAC or FLAC
audio. DASH manifests are served to the renderer through Kalinka's content
endpoint, with fresh signed segment URLs on each manifest request; their
segments carry FLAC or AAC. Segmented streams need a native renderer built with
MPEG-DASH support, and AAC or MP4 streams one with AAC support; older renderers report
these tracks as unsupported. Encrypted streams and other codecs such as Dolby
Atmos are rejected with a readable error.
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
