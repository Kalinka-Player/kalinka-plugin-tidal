# Changelog

## Unreleased

- Expand albums, playlists and mixes past the first 50 tracks when adding them
  to the queue, retaining metadata for the server's subsequent lookup.
- Fail a bulk metadata lookup on timeout or service errors instead of silently
  queueing a subset; genuinely missing items still follow the SDK contract.
- Preserve V1 page section types and use content fingerprints to distinguish
  ambiguous shelves without retargeting saved catalog IDs after reordering.

## 0.1.0

First experimental release of the TIDAL plugin for Kalinka Player.

- Browse Home, Explore, Genres, Moods, My Mixes, and My Collection using tidalapi.
- Search albums, artists, tracks, and playlists; browse artist releases and top tracks.
- Sign in through a browser link and retain the session across server restarts.
- Resolve direct, unencrypted FLAC/MP3 streams when playback starts.
- Build and test Debian packages on Debian 13 and Ubuntu 24.04 for ARM64 and amd64.

TIDAL collections are read-only. Segmented DASH, encrypted streams, and AAC/MP4
playback are not supported. Automated tests use fake API responses; live account
sign-in and playback remain unverified. This integration uses an unofficial
client and is not affiliated with or endorsed by TIDAL.
