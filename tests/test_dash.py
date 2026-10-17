import base64
from unittest.mock import Mock

import pytest
from tidalapi.media import Stream

from kalinka_plugin_sdk.inputmodule import ModuleAsset, SourceUnavailableError


MANIFEST = '''<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"
     mediaPresentationDuration="PT2M21.733S">
  <Period><AdaptationSet contentType="audio" mimeType="audio/mp4" group="main">
    <Representation id="FLAC,44100,16" codecs="flac" audioSamplingRate="44100">
      <SegmentTemplate timescale="44100" startNumber="1"
        initialization="https://cdn.example/init.mp4?token=private"
        media="https://cdn.example/$Number$.mp4?token=private">
        <SegmentTimeline><S d="176128" r="34"/><S d="85960"/></SegmentTimeline>
      </SegmentTemplate>
    </Representation>
  </AdaptationSet></Period>
</MPD>'''
AAC_MANIFEST = MANIFEST.replace(
    'id="FLAC,44100,16" codecs="flac"', 'id="AAC,44100" codecs="mp4a.40.2"'
)


def stream(xml=MANIFEST):
    return Stream().parse({
        "trackId": 11, "audioQuality": "LOSSLESS",
        "manifestMimeType": "application/dash+xml",
        "manifest": base64.b64encode(xml.encode()).decode(),
    })


@pytest.mark.parametrize("xml", [MANIFEST, AAC_MANIFEST])
async def test_dash_is_served_as_a_manifest_for_the_renderer(module, session, track, xml):
    track.get_stream = Mock(return_value=stream(xml))
    session.track = Mock(return_value=track)
    source = await module.get_track_source("11")
    assert source.source == ModuleAsset(module="tidal", asset_id="dash-11")
    assert source.format == "application/dash+xml"
    assert not source.sequential
    assert "private" not in source.model_dump_json()
    info = await module.get_content_info(source.source.asset_id)
    assert info.mime_type == "application/dash+xml"
    assert info.size == len(xml.encode())
    assert not info.cacheable
    with info.reader() as reader:
        reader.seek(10)
        assert reader.read() == xml.encode()[10:]
    with info.reader() as reader:
        assert reader.read() == xml.encode()


async def test_manifest_urls_are_refreshed_when_requested(module, session, track):
    track.get_stream = Mock(side_effect=[stream(), stream(MANIFEST.replace("private", "fresh"))])
    session.track = Mock(return_value=track)
    await module.get_track_source("11")
    info = await module.get_content_info("dash-11")
    with info.reader() as reader:
        assert b"token=fresh" in reader.read()


@pytest.mark.parametrize("asset", ["other-11", "dash-", "dash-../11", "dash-１１", "dash-11?token=private"])
async def test_invalid_asset_never_reaches_tidal(module, session, asset):
    session.track = Mock(side_effect=AssertionError("No request expected"))
    assert await module.get_content_info(asset) is None


@pytest.mark.parametrize("xml", [
    "<MPD>", "<not-mpd/>",
    '<!DOCTYPE MPD [<!ENTITY secret SYSTEM "file:///etc/passwd">]>' + MANIFEST,
    MANIFEST.replace("<Period>", '<Period><ContentProtection schemeIdUri="private"/>'),
    MANIFEST + " " * (1024 * 1024),
])
async def test_invalid_or_encrypted_manifest_is_rejected(module, session, track, xml):
    track.get_stream = Mock(return_value=stream(xml))
    session.track = Mock(return_value=track)
    for operation in (lambda: module.get_track_source("11"), lambda: module.get_content_info("dash-11")):
        with pytest.raises(SourceUnavailableError) as error:
            await operation()
        assert "private" not in str(error.value)


async def test_changed_stream_format_requires_a_fresh_source(module, session, track):
    track.get_stream = Mock(return_value=Mock(is_mpd=False))
    session.track = Mock(return_value=track)
    with pytest.raises(SourceUnavailableError, match="changed the stream format"):
        await module.get_content_info("dash-11")
