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


class TidalLists:
    """TIDAL's offset-addressed lists, answered from rows held per path.

    ``cap(offset)`` bounds an answer the way TIDAL may, short of the limit
    asked for; an offset in ``empty_at`` is answered empty once.
    """

    def __init__(self):
        self.lists = {}
        self.calls = []

    def serve(self, path, rows, *, count=None, counted=True, cap=None, empty_at=()):
        self.lists[path] = SimpleNamespace(
            rows=rows,
            count=len(rows) if count is None else count,
            counted=counted,
            cap=cap,
            empty_at=set(empty_at),
        )

    def request(self, method, path, params, **kwargs):
        assert method == "GET"
        self.calls.append((path, dict(params)))
        served = self.lists[path]
        offset, limit = params["offset"], params["limit"]
        if served.cap:
            limit = min(limit, served.cap(offset))
        items = served.rows[offset:offset + limit]
        if offset in served.empty_at:
            served.empty_at.discard(offset)
            items = []
        body = {"limit": limit, "offset": offset, "items": items}
        if served.counted:
            body["totalNumberOfItems"] = served.count
        return SimpleNamespace(json=lambda: body)


@pytest.fixture
def lists(session, monkeypatch):
    fake = TidalLists()
    monkeypatch.setattr(session.request, "request", fake.request)
    return fake


@pytest.fixture
def artist_json():
    return {"id": 7, "name": "An Artist", "picture": "a-b-c"}


@pytest.fixture
def artist(session, artist_json):
    return session.parse_artist(artist_json)


@pytest.fixture
def album_json():
    return {
        "id": 10,
        "title": "An Album",
        "artists": [{"id": 7, "name": "An Artist"}],
        "cover": "a-b-c",
        "videoCover": None,
        "duration": 600,
        "numberOfTracks": 3,
        "streamReady": True,
    }


@pytest.fixture
def album(session, album_json):
    return session.parse_album(album_json)


@pytest.fixture
def track_json(album_json):
    return {
        "id": 11,
        "title": "A Song",
        "version": "Live",
        "duration": 200,
        "popularity": 50,
        "artists": [{"id": 7, "name": "An Artist"}],
        "album": album_json,
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
    }


@pytest.fixture
def track(session, album, track_json):
    return session.parse_track(track_json, album=album)


@pytest.fixture
def playlist_json():
    return {
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


@pytest.fixture
def playlist(session, playlist_json):
    return session.parse_playlist(playlist_json)
