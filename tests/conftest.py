from types import SimpleNamespace

import pytest
import requests
import tidalapi

from kalinka_plugin_tidal.tidal import TidalInputModule
from kalinka_plugin_tidal.client import CatalogSession


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Tests must not contact TIDAL")

    monkeypatch.setattr(requests.Session, "request", blocked)


@pytest.fixture
def session():
    value = CatalogSession(tidalapi.Config())
    yield value
    value.request_session.close()


@pytest.fixture
def module(session):
    async def run(operation, **kwargs):
        return operation()

    return TidalInputModule(SimpleNamespace(session=session, run=run))


@pytest.fixture
def artist(session):
    return session.parse_artist({"id": 7, "name": "An Artist", "picture": "a-b-c"})


@pytest.fixture
def album(session):
    return session.parse_album(
        {
            "id": 10,
            "title": "An Album",
            "artists": [{"id": 7, "name": "An Artist"}],
            "cover": "a-b-c",
            "videoCover": None,
            "duration": 600,
            "numberOfTracks": 3,
            "streamReady": True,
        }
    )


@pytest.fixture
def track(session, album):
    return session.parse_track(
        {
            "id": 11,
            "title": "A Song",
            "version": "Live",
            "duration": 200,
            "popularity": 50,
            "artists": [{"id": 7, "name": "An Artist"}],
            "explicit": False,
            "allowStreaming": True,
            "streamReady": True,
            "stemReady": False,
            "djReady": False,
            "adSupportedStreamReady": False,
            "trackNumber": 7,
            "volumeNumber": 2,
            "replayGain": -5.4,
            "peak": 0.9,
        },
        album=album,
    )


@pytest.fixture
def playlist(session):
    return session.parse_playlist(
        {
            "uuid": "list-1",
            "title": "A Playlist",
            "numberOfTracks": 3,
            "numberOfVideos": 0,
            "description": "Curated music",
            "duration": 600,
            "type": "EDITORIAL",
            "image": "a-b-c",
            "squareImage": "a-b-c",
            "promotedArtists": [],
        }
    )
