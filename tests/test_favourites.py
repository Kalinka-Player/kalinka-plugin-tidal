from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from kalinka_plugin_sdk.datamodel import EntityType, FavoriteIds
from kalinka_plugin_sdk.filters import UnsupportedFilter
from kalinka_plugin_sdk.inputmodule import SearchType, SourceUnavailableError
from kalinka_plugin_tidal import tidal
from kalinka_plugin_tidal.client import TidalClient
from kalinka_plugin_tidal.tidal import TidalInputModule, entity_id

PLAYLIST = "36ea71a8-445e-41a4-82ab-6628c581535d"
MARKED = [
    (EntityType.TRACK, "11", "tracks", {"trackId": "11"}),
    (EntityType.ALBUM, "10", "albums", {"albumId": "10"}),
    (EntityType.ARTIST, "7", "artists", {"artistId": "7"}),
    (EntityType.PLAYLIST, PLAYLIST, "playlists", {"uuids": PLAYLIST}),
]


class Account:
    """User 5's favourites, answering what the v1 endpoints answer.

    Any other request fails the test: v2's playlist removal, which deletes
    a playlist the account owns, must never be sent.
    """

    def __init__(self, ids=None):
        self.ids = ids or {}
        self.calls = []

    def request(self, method, path, params=None, data=None, base_url=None, **kwargs):
        assert base_url is None, "favourites go through v1"
        assert path.startswith("users/5/favorites/")
        self.calls.append((method, path, data))
        return SimpleNamespace(json=lambda: self.ids)


@pytest.fixture
def account(session, monkeypatch):
    session.user = SimpleNamespace(id=5)
    fake = Account()
    monkeypatch.setattr(session.request, "request", fake.request)
    return fake


def favourite(row, created):
    return {"created": created, "item": row}


def test_favourites_are_offered_only_while_signed_in(module, monkeypatch):
    monkeypatch.setattr(tidal, "FAVORITES", "favorites")
    assert "favorites" in module.capabilities()
    module.client.ready = False
    assert "favorites" not in module.capabilities()


def test_favourites_are_not_offered_to_a_server_that_cannot_ask(module, monkeypatch):
    monkeypatch.setattr(tidal, "FAVORITES", None)
    assert None not in module.capabilities()


async def test_every_favourite_id_comes_from_one_request(module, account):
    account.ids = {
        "TRACK": ["11", "12"],
        "ALBUM": ["10"],
        "ARTIST": [],
        "PLAYLIST": [PLAYLIST],
        "VIDEO": ["3"],
    }
    assert await module.get_favorite_ids() == FavoriteIds(
        tracks=[entity_id(EntityType.TRACK, "11"), entity_id(EntityType.TRACK, "12")],
        albums=[entity_id(EntityType.ALBUM, "10")],
        artists=[],
        playlists=[entity_id(EntityType.PLAYLIST, PLAYLIST)],
    )
    assert account.calls == [("GET", "users/5/favorites/ids", None)]


@pytest.mark.parametrize("answer", [
    {"TRACK": [], "ALBUM": [], "ARTIST": []},
    {"TRACK": [], "ALBUM": [], "ARTIST": [], "PLAYLIST": None},
    [],
])
async def test_ids_tidal_answers_in_part_fail_rather_than_empty_hearts(
    module, account, answer
):
    account.ids = answer
    with pytest.raises(SourceUnavailableError):
        await module.get_favorite_ids()


async def test_signed_out_reads_answer_empty_without_asking_tidal(module, account):
    module.client.ready = False
    assert await module.get_favorite_ids() == FavoriteIds()
    page = await module.list_favorite(SearchType.track, "", offset=10, limit=5)
    assert (page.items, page.total, page.offset, page.limit) == ([], 0, 10, 5)
    assert account.calls == []


@pytest.mark.parametrize("kind,value,collection,form", MARKED)
async def test_each_kind_is_marked_through_its_own_v1_field(
    module, account, kind, value, collection, form
):
    await module.add_to_favorite(entity_id(kind, value).to_string)
    assert account.calls == [("POST", f"users/5/favorites/{collection}", form)]


@pytest.mark.parametrize("kind,value,collection,form", MARKED)
async def test_each_kind_is_unmarked_by_its_id(
    module, account, kind, value, collection, form
):
    await module.remove_from_favorite(entity_id(kind, value).to_string)
    assert account.calls == [
        ("DELETE", f"users/5/favorites/{collection}/{value}", None)
    ]


@pytest.mark.parametrize("item,error", [
    ("kalinka:qobuz:track:11", LookupError),
    ("kalinka:tidal:catalog:home", ValueError),
    ("kalinka:tidal:genre:1", ValueError),
    # TIDAL reads a comma as several items.
    ("kalinka:tidal:track:11,12", LookupError),
    ("kalinka:tidal:album:ten", LookupError),
    ("kalinka:tidal:artist:", LookupError),
    ("kalinka:tidal:playlist:list-1", LookupError),
    (f"kalinka:tidal:playlist:{PLAYLIST},{PLAYLIST}", LookupError),
])
async def test_an_id_that_names_no_single_favourite_is_refused_unsent(
    module, account, item, error
):
    for change in (module.add_to_favorite, module.remove_from_favorite):
        with pytest.raises(error):
            await change(item)
    assert account.calls == []


async def test_favourites_list_newest_first_with_when_each_was_marked(
    module, session, lists, track_json
):
    session.user = SimpleNamespace(id=5)
    lists.serve("users/5/favorites/tracks", [
        favourite({**track_json, "id": 12}, "2026-11-25T13:57:26.194+0000"),
        favourite({**track_json, "id": 11}, "2026-11-25T13:57:22.757+0000"),
        favourite({**track_json, "id": 13}, "2026-11-20T08:00:00.000+0000"),
    ])
    page = await module.list_favorite(SearchType.track, "", offset=0, limit=2)
    assert lists.calls == [(
        "users/5/favorites/tracks",
        {"order": "DATE", "orderDirection": "DESC", "offset": 0, "limit": 2},
    )]
    assert [(item.id.id, item.timestamp) for item in page.items] == [
        ("12", 1795615046), ("11", 1795615042),
    ]
    assert (page.total, page.offset, page.limit) == (3, 0, 2)


@pytest.mark.parametrize("type,fixture,name", [
    (SearchType.album, "album_json", "An Album"),
    (SearchType.artist, "artist_json", "An Artist"),
    (SearchType.playlist, "playlist_json", "A Playlist"),
])
async def test_each_kind_lists_from_its_own_favourites(
    module, session, lists, request, type, fixture, name
):
    session.user = SimpleNamespace(id=5)
    row = favourite(request.getfixturevalue(fixture), "2026-01-01T00:00:00.000+0000")
    lists.serve(f"users/5/favorites/{type.value}s", [row])
    page = await module.list_favorite(type, "")
    assert [(item.name, item.timestamp) for item in page.items] == [(name, 1767225600)]


async def test_a_search_of_favourites_is_refused_rather_than_ignored(module, account):
    with pytest.raises(UnsupportedFilter) as refused:
        await module.list_favorite(SearchType.track, "blue")
    assert refused.value.field == "filter"
    assert account.calls == []


@pytest.fixture
async def client(tmp_path):
    instance = TidalClient(tmp_path / "tidal" / "session.json")
    instance.session.user = SimpleNamespace(id=5)
    instance.session.token_type = "Bearer"
    instance.session.access_token = "private-access"
    yield instance
    await instance.close()


def answering(client, monkeypatch, status):
    response = requests.Response()
    response.status_code = status
    response._content = b"{}"
    response.request = requests.Request("GET", "https://api.tidal.com/").prepare()
    sent = Mock(return_value=response)
    monkeypatch.setattr(client.session.request_session, "request", sent)
    return sent


async def test_an_item_tidal_does_not_have_is_not_an_outage(client, monkeypatch):
    client.ready = True
    answering(client, monkeypatch, 404)
    with pytest.raises(LookupError):
        await TidalInputModule(client).add_to_favorite("kalinka:tidal:track:1")


async def test_a_refused_change_reports_tidal_unavailable(client, monkeypatch):
    client.ready = True
    answering(client, monkeypatch, 500)
    module = TidalInputModule(client)
    for change in (module.add_to_favorite, module.remove_from_favorite):
        with pytest.raises(SourceUnavailableError):
            await change("kalinka:tidal:album:10")


async def test_a_change_while_signed_out_fails_without_a_request(client, monkeypatch):
    sent = answering(client, monkeypatch, 200)
    module = TidalInputModule(client)
    for change in (module.add_to_favorite, module.remove_from_favorite):
        with pytest.raises(SourceUnavailableError):
            await change("kalinka:tidal:track:11")
    sent.assert_not_called()
