# Changelog

## 0.4.0

- Keep each shelf to one kind of item. A shelf with more than it shows
  ended in a catalog card leading to the rest, so an album shelf ended in
  one card unlike its albums. Opened, the shelf now goes on to the rest of
  its items in place.
- Report the shape of covers and promotion banners, so Kalinka app 0.21.0
  lays out a listing that mixes albums, playlists and promotions before
  their pictures load.
- Include track and disc numbers in track details.
- Needs plugin SDK 3.16, which ships with Kalinka Player 5.13.0.

## 0.3.0

- Offer TIDAL as a home source. The plugin reports that its browse root leads
  to catalogs, so the Kalinka app can list TIDAL among the music sources on
  Home and pin its catalogs. Needs plugin SDK 3.14, which ships with Kalinka
  Player 5.12.0.
- Show artist pictures. The plugin asked for a size TIDAL does not offer for
  artists, so every artist was sent without one.
- Show featured promotion banners in the sizes TIDAL serves, instead of
  square sizes that do not exist. Genre, mood and decade links no longer
  point at images TIDAL does not serve, so apps show their own artwork.

## 0.2.2

- Ask TIDAL for an empty page a second time before ending an album or
  playlist add early, so a one-off empty reply does not cut the add short.
- Check only an idle TIDAL session every 10 minutes; successful requests
  already show that the sign-in is valid.

## 0.2.1

- Warn when TIDAL rejects the saved sign-in and offer a new sign-in link that
  says the sign-in expired, instead of the first-time prompt. Browsing reports
  the expiry too. The session is checked every 10 minutes, so an idle player
  notices revocation. An outage of TIDAL's sign-in server keeps the sign-in,
  and browsing during a reconnect says TIDAL is connecting. A track TIDAL
  refuses to play reports the refusal instead of interrupting the sign-in.
- Add albums and playlists whose track count includes tracks TIDAL no longer
  lists, instead of failing with "incomplete track list". Listed tracks that
  cannot be streamed are still added and fail when played.

## 0.2.0

- Require Kalinka Player 5.11.0 or newer (below 6). AAC, MP4 and segmented
  streams play on Kalinka renderer 0.10.0 or newer.
- Pass DASH manifests to the renderer for native segmented FLAC playback,
  instead of rejecting every segmented stream. Requires a renderer with DASH support.
- Send explicit track and artist action flags so existing apps can decode album
  listings and search results.
- Combine concurrent search categories into one TIDAL request to avoid timing
  out the last category and failing the entire search.
- Use device authorisation: approve the TIDAL link and connect automatically.
  Remove browser-response fields and add an Unlink Tidal switch using
  Apply & Restart. Existing valid sessions remain signed in until reset.
- Add requested Lossless and Max / hi-res quality choices; default to Max.
  TIDAL controls the returned codec and resolution.
- Play AAC and MP4 streams, direct or segmented, as `audio/mp4`. Requires a
  renderer with AAC support. Other codecs are refused, naming the codec.
  Test direct FLAC/MP3/AAC responses through tidalapi's real manifest parser.
- Restore Home, Explore, Genres and Moods with their shelves alongside
  My Collection and My Mixes.
- Enable TIDAL by default so the installation restart also starts sign-in,
  without a second restart to enable the plugin. Explicit disable settings
  remain respected.

## 0.1.1

- Add a one-shot setting to forget TIDAL authorization on the next restart,
  with sign-in status visible in plugin settings.
- Show the automatically requested CD-quality lossless tier and explain that
  TIDAL Max / hi-res playback is not supported yet.
- Reduce the catalog root to My Collection and My Mixes, while retaining search
  and previously saved discovery links.
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
