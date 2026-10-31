import asyncio
import base64
import json
import time
from importlib.metadata import entry_points
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from tidalapi.exceptions import ObjectNotFound
from tidalapi.media import Stream
from tidalapi.page import PageItem, PageLink

from kalinka_plugin_sdk.datamodel import EntityId, EntityType
from kalinka_plugin_sdk.filters import FilterQuery, UnsupportedFilter
from kalinka_plugin_sdk.inputmodule import (
    ModuleCapability,
    SearchType,
    SourceUnavailableError,
)
from kalinka_plugin_tidal.config_model import TidalConfig
from kalinka_plugin_tidal.client import CatalogPage, TidalClient
from kalinka_plugin_tidal.paging import MAX_REQUESTS
from kalinka_plugin_tidal.tidal import TidalInputModule, entity_id, route


def cat(value):
    return entity_id(EntityType.CATALOG, value)


def numbered(row, count):
    return [dict(row, id=number) for number in range(count)]


def favorite(row):
    return {"created": "2026-01-01T00:00:00.000+0000", "item": row}


def test_plugin_discovery():
    plugin = next(
        ep
        for ep in entry_points(group="kalinka.plugins")
        if ep.name == "kalinka_plugin_tidal"
    ).load()
    assert plugin.PLUGIN_ID == "tidal"
    assert plugin.CONFIG_MODEL is TidalConfig
    assert TidalConfig().enabled


def test_tidal_offers_its_catalogs_as_a_home_source(module):
    assert module.capabilities() == frozenset({ModuleCapability.CATALOGS})


async def test_root_is_paginated_and_available_before_sign_in(module):
    module.client.run = Mock(side_effect=AssertionError("No network for the root"))
    first = await module.browse(cat("root"), limit=1)
    rest = await module.browse(cat("root"), offset=1)
    assert first.total == rest.total == 6
    assert [item.name for item in first.items + rest.items] == [
        "Home",
        "Explore",
        "Genres",
        "Moods",
        "My Mixes",
        "My Collection",
    ]
    for item in first.items + rest.items:
        assert await module.get(item.id) == item


@pytest.mark.parametrize("page,title", [
    ("home", "Home"), ("explore", "Explore"), ("genres", "Genres"), ("moods", "Moods"),
    ("mixes", "My Mixes"),
])
async def test_discovery_pages_load_their_tidal_page(module, session, page, title):
    getattr_page = Mock(return_value=SimpleNamespace(categories=[]))
    setattr(session, page, getattr_page)
    assert (await module.get(cat(page))).name == title
    assert (await module.browse(cat(page))).items == []
    getattr_page.assert_called_once_with()


@pytest.mark.parametrize("version", [1, 2])
async def test_real_page_parsers_expose_catalogs_and_stable_routes(
    module, session, version
):
    track_json = {
        "id": 10,
        "title": "Album",
        "artists": [{"id": 7, "name": "Artist"}],
        "cover": None,
        "videoCover": None,
        "numberOfTracks": 3,
        "streamReady": True,
    }
    if version == 1:
        data = {
            "title": "Explore",
            "rows": [
                {
                    "modules": [
                        {
                            "type": "ALBUM_LIST",
                            "title": "New Releases",
                            "pagedList": {"items": [track_json]},
                            "showMore": {
                                "apiPath": "pages/new",
                                "title": "All releases",
                            },
                        }
                    ]
                }
            ],
        }
    else:
        data = {
            "items": [
                {
                    "type": "HORIZONTAL_LIST",
                    "moduleId": "new-albums",
                    "title": "New Releases",
                    "items": [{"type": "ALBUM", "data": track_json}],
                }
            ]
        }
    page = CatalogPage(session, "").parse(data)
    session.home = Mock(return_value=page)
    home = next(item for item in (await module.browse(cat("root"))).items
                if item.id == cat("home"))
    shelves = await module.browse(home.id)
    assert [item.name for item in shelves.items] == ["New Releases"]
    shelf = shelves.items[0]
    assert EntityId.from_string(shelf.id.to_string) == shelf.id
    result = await module.browse(shelf.id, limit=1)
    assert result.items[0].album.title == "Album"
    assert result.items[0].can_add
    assert (await module.get(shelf.id)).name == "New Releases"
    # Recreate the module with an empty cache, as on restart.
    restarted = TidalInputModule(module.client)
    assert (await restarted.browse(shelf.id, limit=1)).items == result.items
    if version == 1:
        # The preview stays inside the shelf; paging on reads its full list.
        assert result.total == 2
        second = {**track_json, "id": 12, "title": "Second"}
        full = {"title": "All releases", "rows": [{"modules": [{
            "type": "ALBUM_LIST",
            "title": "All releases",
            "pagedList": {"items": [track_json, second]},
        }]}]}
        session.request.request = Mock(return_value=SimpleNamespace(json=lambda: full))
        rest = await restarted.browse(shelf.id, offset=1)
        assert [item.album.title for item in rest.items] == ["Second"]
        assert rest.total == 2
        assert session.request.request.call_args.args[:2] == ("GET", "pages/new")
    else:
        assert result.total == 1


async def test_a_more_page_of_several_shelves_stays_a_link(module, session, album):
    shelf = SimpleNamespace(
        title="New Releases",
        items=[album],
        _more=SimpleNamespace(api_path="pages/new", title="All releases"),
    )
    session.explore = Mock(return_value=SimpleNamespace(categories=[shelf]))
    more = SimpleNamespace(categories=[
        SimpleNamespace(title="Albums", items=[album]),
        SimpleNamespace(title="EPs", items=[album]),
    ])
    module._page = Mock(side_effect=lambda value: (
        more if value != "explore" else session.explore()
    ))
    section = (await module.browse(cat("explore"))).items[0]
    result = await module.browse(section.id)
    assert [item.name for item in result.items] == ["An Album", "All releases"]
    assert result.items[1].catalog is not None


async def test_duplicate_shelf_titles_have_distinct_ids_and_reordering_is_safe(
    module, session, album, track
):
    # Use the real V1 parser: its categories originally lost their type.
    session.parse_album = Mock(return_value=album)
    session.parse_track = Mock(return_value=track)
    rows = [
        {"modules": [{"type": kind, "title": "Featured", "pagedList": {"items": [{}]}}]}
        for kind in ("ALBUM_LIST", "TRACK_LIST")
    ]
    data = {"title": "Explore", "rows": rows}
    session.request.request = Mock(return_value=SimpleNamespace(json=lambda: data))
    # Recreate the page after the parser mocks are installed.
    session.page = CatalogPage(session, "")
    shelves = (await module.browse(cat("explore"))).items
    assert shelves[0].id != shelves[1].id
    rows.reverse()
    module._cache.clear()
    assert (await module.browse(shelves[0].id)).items[0].album is not None
    assert (await module.browse(shelves[1].id)).items[0].track is not None


async def test_identical_shelf_titles_and_types_never_retarget_old_routes(module, session):
    def row(id):
        return {"modules": [{
            "type": "ALBUM_LIST", "title": "Featured", "pagedList": {"items": [{
                "id": id, "title": str(id), "artists": [{"id": 7, "name": "Artist"}],
                "cover": None, "videoCover": None, "numberOfTracks": 1, "streamReady": True,
            }]},
        }]}

    rows = [row(10), row(20)]
    data = {"title": "Explore", "rows": rows}
    session.request.request = Mock(return_value=SimpleNamespace(json=lambda: data))
    shelves = (await module.browse(cat("explore"))).items
    rows.reverse()
    module._cache.clear()
    assert (await module.browse(shelves[0].id)).items[0].id.id == "10"
    assert (await module.browse(shelves[1].id)).items[0].id.id == "20"
    rows[1] = row(30)
    module._cache.clear()
    with pytest.raises(LookupError, match="section has changed"):
        await module.browse(shelves[0].id)


async def test_account_change_invalidates_personalized_pages(module, session, track):
    page = SimpleNamespace(categories=[SimpleNamespace(title="For You", items=[track])])
    session.home = Mock(return_value=page)
    session.user = SimpleNamespace(id=1)
    await module.browse(cat("home"))
    await module.browse(cat("home"))
    session.home.assert_called_once()
    session.user = SimpleNamespace(id=2)
    await module.browse(cat("home"))
    assert session.home.call_count == 2


async def test_nested_genre_links_and_promotions_are_lazy(
    module, session, lists, album
):
    link = PageLink(
        session,
        {
            "title": "Jazz",
            "icon": "jazz",
            "apiPath": "pages/genre/jazz",
            "imageId": "jazz",
        },
    )
    promotion = PageItem(
        session,
        {
            "header": "New album",
            "shortHeader": "New",
            "shortSubHeader": "",
            "imageId": "abc-def",
            "type": "ALBUM",
            "artifactId": "10",
            "text": "",
            "featured": True,
        },
    )
    session.genres = Mock(
        return_value=SimpleNamespace(
            categories=[SimpleNamespace(title="Genres", items=[link, promotion, None])]
        )
    )
    section = (await module.browse(cat("genres"))).items[0]
    result = await module.browse(section.id)
    assert [item.name for item in result.items] == ["Jazz", "New"]
    assert result.items[0].catalog.image is None
    assert result.items[1].catalog.image.large == (
        "https://resources.tidal.com/images/abc/def/1100x800.jpg"
    )
    assert result.items[1].catalog.image.aspect_ratio == 1100 / 800
    lists.serve("albums/10/tracks", [])
    session.album = Mock(return_value=album)
    await module.browse(result.items[1].id)
    session.album.assert_called_once_with("10")
    assert lists.calls == [("albums/10/tracks", {"offset": 0, "limit": 50})]


async def test_album_and_playlist_tracks_keep_metadata_and_order(
    module, lists, album, playlist, track_json
):
    rows = numbered(track_json, 2) + [track_json]
    lists.serve("albums/10/tracks", rows)
    lists.serve("playlists/list-1/tracks", rows)
    for parent in (album, playlist):
        item = module._item(parent)
        result = await module.browse(item.id, offset=2, limit=1)
        assert result.total == 3
        assert result.items[0].track.title == "A Song (Live)"
        assert result.items[0].track.duration == 200
        assert result.items[0].track.performer.name == "An Artist"
        assert result.items[0].track.album.image.large.endswith("640x640.jpg")
        assert result.items[0].track.replaygain_gain == -5.4
        payload = result.items[0].track.model_dump(mode="json")
        assert (payload["track_number"], payload["disc_number"]) == (7, 2)
        assert (
            await module.browse(item.sections[0].id, offset=2, limit=1)
        ).items == result.items
        assert lists.calls[-1][1] == {"offset": 2, "limit": 1}


async def test_artist_sections_use_the_corresponding_api(
    module, lists, artist, album_json, track_json
):
    lists.serve("artists/7/albums", [album_json] * 5)
    lists.serve("artists/7/toptracks", [track_json] * 5)
    artist.get_similar = Mock(return_value=[])
    item = module._item(artist)
    for section in item.sections:
        await module.browse(section.id, offset=2, limit=3)
    assert lists.calls == [
        ("artists/7/toptracks", {"offset": 2, "limit": 3}),
        ("artists/7/albums", {"offset": 2, "limit": 3}),
        ("artists/7/albums", {"filter": "EPSANDSINGLES", "offset": 2, "limit": 3}),
    ]
    artist.get_similar.assert_called_once_with()


async def test_album_response_includes_flags_required_by_existing_apps(
    module, lists, album, track_json
):
    lists.serve("albums/10/tracks", [track_json])
    item = module._item(album)
    result = await module.browse(item.id, filter=FilterQuery({}))
    payload = result.model_dump(mode="json", exclude_unset=True)
    assert payload["items"][0]["can_browse"] is False
    assert payload["items"][0]["can_add"] is True
    assert payload["items"][0]["track"]["id"] == "kalinka:tidal:track:11"
    assert await module.tracks_to_add(item.id, 50) == [track_id := result.items[0].id]
    assert (await module.get(track_id)).track.title == payload["items"][0]["track"]["title"]


def test_artist_response_includes_flags_required_by_existing_apps(module, artist):
    payload = module._item(artist).model_dump(mode="json", exclude_unset=True)
    assert payload["can_add"] is False
    assert payload["can_browse"] is True


def test_artist_pictures_use_sizes_tidal_serves(module, artist, track):
    image = module._item(artist).artist.image
    assert image.small.endswith("/a/b/c/320x320.jpg")
    assert image.large.endswith("/a/b/c/750x750.jpg")
    assert image.aspect_ratio == 1.0
    assert module._item(track).track.performer.image.large.endswith("750x750.jpg")


def test_a_playlist_without_a_square_picture_reports_its_wide_one(module, playlist):
    assert module._item(playlist).playlist.image.aspect_ratio == 1.0
    playlist.square_picture = None
    assert module._item(playlist).playlist.image.aspect_ratio == 1080 / 720


def search_results(rows, count):
    return {
        f"{kind}s": {"totalNumberOfItems": count, "items": [row]}
        for kind, row in rows.items()
    }


@pytest.fixture
def search_rows(track_json, album_json, artist_json, playlist_json):
    return {
        "track": track_json,
        "album": album_json,
        "artist": artist_json,
        "playlist": playlist_json,
    }


@pytest.mark.parametrize("kind", list(SearchType))
async def test_search_selects_requested_kind_and_reports_tidal_totals(
    module, session, kind, search_rows
):
    body = search_results(search_rows, 120)
    session.request.request = Mock(return_value=SimpleNamespace(json=lambda: body))
    result = await module.search(kind, "Music", offset=50, limit=1)
    assert result.total == 120
    assert result.items[0].id.type.value == kind.value
    session.request.request.assert_called_once_with(
        "GET",
        "search",
        params={
            "query": "Music",
            "types": "tracks,albums,artists,playlists",
            "offset": 50,
            "limit": 1,
        },
    )
    assert (await module.search(kind, "", offset=0)).total == 0
    assert (await module.search(kind, "Music", offset=300)).total == 0
    assert session.request.request.call_count == 1


@pytest.mark.parametrize(
    "count,items,total",
    [(5000, 1, 300), (None, 1, 52), (None, 0, 50), (80, 0, 50)],
)
async def test_search_total_is_capped_and_ends_where_results_do(
    module, session, track_json, count, items, total
):
    found = {"items": [track_json] * items}
    if count is not None:
        found["totalNumberOfItems"] = count
    session.request.request = Mock(
        return_value=SimpleNamespace(json=lambda: {"tracks": found})
    )
    result = await module.search(SearchType.track, "Music", offset=50, limit=10)
    assert (result.total, len(result.items)) == (total, items)


async def test_concurrent_search_kinds_share_one_upstream_request(tmp_path, search_rows):
    client = TidalClient(tmp_path / "session.json")
    client.ready = True
    module = TidalInputModule(client)

    def search(*args, **kwargs):
        time.sleep(0.85)
        body = search_results(search_rows, 1)
        return SimpleNamespace(json=lambda: body)

    client.session.request.request = Mock(side_effect=search)
    try:
        results = await asyncio.gather(*(module.search(kind, "Music") for kind in SearchType))
        assert [result.items[0].id.type.value for result in results] == [kind.value for kind in SearchType]
        client.session.request.request.assert_called_once()
    finally:
        await client.close()


async def test_search_cache_expires_and_is_scoped_to_account_and_page(
    module, session, search_rows
):
    body = search_results(search_rows, 1)
    session.request.request = Mock(return_value=SimpleNamespace(json=lambda: body))
    await module.search(SearchType.track, "Music", limit=1)
    key = ("search", "Music", 0, 1)
    expires, data = module._cache[key]
    assert 0 < expires - time.monotonic() <= 5
    module._cache[key] = (0, data)
    await module.search(SearchType.track, "Music", limit=1)
    await module.search(SearchType.track, "Music", offset=1, limit=1)
    session.user = SimpleNamespace(id=2)
    await module.search(SearchType.track, "Music", limit=1)
    assert session.request.request.call_count == 4


async def test_collection_uses_paged_library_calls(
    module, session, lists, track_json, album_json, artist_json, playlist_json
):
    session.user = SimpleNamespace(id=5)
    lists.serve("users/5/favorites/tracks", [favorite(track_json)] * 60)
    lists.serve("users/5/favorites/albums", [favorite(album_json)] * 60)
    lists.serve("users/5/favorites/artists", [favorite(artist_json)] * 60)
    entry = {
        "type": "USER_CREATED",
        "created": "2026-01-01T00:00:00.000+0000",
        "playlist": playlist_json,
    }
    lists.serve("users/5/playlistsAndFavoritePlaylists", [entry] * 60)
    shelves = (await module.browse(cat("collection"))).items
    results = [await module.browse(shelf.id, offset=50, limit=100) for shelf in shelves]
    assert [call[0].rsplit("/", 1)[-1] for call in lists.calls] == [
        "tracks", "albums", "artists", "playlistsAndFavoritePlaylists"
    ]
    assert all(call[1] == {"offset": 50, "limit": 50} for call in lists.calls)
    assert [result.total for result in results] == [60] * 4
    assert [result.items[0].name for result in results] == [
        "A Song (Live)", "An Album", "An Artist", "A Playlist"
    ]


async def test_favorites_page_on_past_answers_tidal_cuts_short(
    module, session, lists, track_json
):
    session.user = SimpleNamespace(id=5)
    rows = [favorite(row) for row in numbered(track_json, 120)]
    # TIDAL answers no further than the next multiple of 50 (issue #10).
    lists.serve("users/5/favorites/tracks", rows, cap=lambda offset: 50 - offset % 50)
    seen, totals = [], set()
    for _ in range(5):
        page = await module.browse(cat("collection-track"), offset=len(seen), limit=30)
        seen += [item.id.id for item in page.items]
        totals.add(page.total)
        if len(seen) >= page.total:
            break
    assert seen == [str(number) for number in range(120)]
    assert totals == {120}


async def test_a_list_tidal_overcounts_ends_with_its_exact_total(
    module, lists, album, track_json
):
    lists.serve("albums/10/tracks", numbered(track_json, 10), count=12)
    result = await module.browse(module._item(album).id)
    assert (len(result.items), result.total) == (10, 10)
    assert [params["offset"] for _, params in lists.calls] == [0, 10, 10]


async def test_a_list_without_a_count_advertises_one_more_until_it_ends(
    module, lists, playlist, track_json
):
    lists.serve("playlists/list-1/tracks", numbered(track_json, 60), counted=False)
    item = module._item(playlist)
    assert (await module.browse(item.id)).total == 51
    rest = await module.browse(item.id, offset=50)
    assert (len(rest.items), rest.total) == (10, 60)


async def test_a_page_tidal_keeps_answering_short_is_returned_part_filled(
    module, lists, playlist, track_json
):
    lists.serve("playlists/list-1/tracks", numbered(track_json, 100), cap=lambda offset: 1)
    item = module._item(playlist)
    first = await module.browse(item.id)
    assert (len(first.items), first.total) == (MAX_REQUESTS, 100)
    second = await module.browse(item.id, offset=MAX_REQUESTS)
    assert second.items[0].id.id == str(MAX_REQUESTS)


async def test_ids_from_clients_stay_one_path_segment(module, lists):
    lists.serve("playlists/..%2Fsessions/tracks", [])
    lists.serve("artists/1%2F..%2Fsessions/toptracks", [])
    await module.browse(entity_id(EntityType.PLAYLIST, "../sessions"))
    await module.browse(cat(route("artist", "1/../sessions", "top")))
    assert len(lists.calls) == 2


async def test_mix_tracks_are_paginated_without_reloading(module, session, track):
    session.mix = Mock(
        return_value=SimpleNamespace(items=Mock(return_value=[track, track, track]))
    )
    eid = cat(route("mix", "mix-1"))
    result = await module.browse(eid, offset=1, limit=1)
    assert len(result.items) == 1 and result.total == 3
    await module.browse(eid, offset=2, limit=1)
    session.mix.assert_called_once_with("mix-1")


async def test_unavailable_track_is_visible_but_cannot_be_added(module, track):
    track.available = False
    item = module._item(track)
    assert item.track.unavailable
    assert not item.can_add


async def test_track_queueing_is_lazy_and_stream_urls_are_resolved_each_time(
    module, track
):
    stream = SimpleNamespace(
        is_bts=True,
        is_mpd=False,
        get_stream_manifest=Mock(
            return_value=SimpleNamespace(
                encryption_type="NONE",
                codecs="FLAC",
                mime_type="audio/flac",
                get_urls=lambda: ["https://cdn.example/audio.flac"],
            )
        ),
    )
    track.get_stream = Mock(return_value=stream)
    module._item(track)
    info = (await module.get_track_info(["11"]))[0]
    track.get_stream.assert_not_called()
    assert info.metadata.title == "A Song (Live)"
    assert (info.metadata.track_number, info.metadata.disc_number) == (7, 2)
    assert (await info.source_retriever()).format == "flac"
    assert (
        await module.get_track_source("11")
    ).source.url == "https://cdn.example/audio.flac"
    assert track.get_stream.call_count == 2


@pytest.mark.parametrize("numbers", [(None, None), (7, None), (None, 2)])
def test_missing_track_positions_remain_unknown(module, track, numbers):
    track.track_num, track.volume_num = numbers
    payload = module._item(track).track.model_dump(mode="json")
    assert (payload["track_number"], payload["disc_number"]) == numbers


CONTAINER_PATHS = {"album": "albums/10/tracks", "playlist": "playlists/list-1/tracks"}


@pytest.mark.parametrize("kind", ["album", "playlist"])
@pytest.mark.parametrize("limit", [0, 61, 5000])
async def test_container_add_pages_tracks_and_honors_the_queue_limit(
    module, session, lists, album, playlist, track_json, kind, limit
):
    parent = album if kind == "album" else playlist
    rows = numbered(track_json, 80)
    rows[-1] = rows[0]  # A repeated playlist track must retain its position.
    lists.serve(CONTAINER_PATHS[kind], rows)
    item = module._item(parent)
    ids = await module.tracks_to_add(item.id, limit)
    session.track = Mock(side_effect=AssertionError("Queue metadata was already fetched"))
    assert [eid.id for eid in ids] == [str(row["id"]) for row in rows[:limit]]
    assert [item.id for item in await module.get_all(ids)] == ids
    assert all(params["limit"] <= 50 for _, params in lists.calls)
    if limit > 50:
        assert lists.calls[1][1]["offset"] == 50


async def test_large_container_keeps_metadata_until_the_queue_resolves_it(
    module, session, lists, playlist, track_json
):
    rows = numbered(track_json, 600)
    lists.serve(CONTAINER_PATHS["playlist"], rows)
    item = module._item(playlist)
    ids = await module.tracks_to_add(item.id, 5000)
    session.track = Mock(side_effect=AssertionError("Do not refetch expanded tracks"))
    assert len(ids) == 600
    assert [item.id for item in await module.get_all(ids)] == ids


@pytest.mark.parametrize("kind", ["album", "playlist"])
@pytest.mark.parametrize("listed", [0, 10, 50])
async def test_tracks_tidal_no_longer_lists_do_not_fail_the_add(
    module, lists, album, playlist, track_json, kind, listed
):
    # TIDAL counts tracks that its track listing omits.
    parent = album if kind == "album" else playlist
    rows = numbered(track_json, listed)
    lists.serve(CONTAINER_PATHS[kind], rows, count=listed + 2)
    item = module._item(parent)
    ids = await module.tracks_to_add(item.id, 5000)
    assert [eid.id for eid in ids] == [str(row["id"]) for row in rows]


async def test_one_off_empty_page_does_not_cut_the_add_short(
    module, lists, playlist, track_json
):
    rows = numbered(track_json, 80)
    lists.serve(CONTAINER_PATHS["playlist"], rows, empty_at={50})
    item = module._item(playlist)
    ids = await module.tracks_to_add(item.id, 5000)
    assert [eid.id for eid in ids] == [str(row["id"]) for row in rows]


async def test_unplayable_listed_tracks_are_still_added(
    module, lists, album, track_json
):
    lists.serve(CONTAINER_PATHS["album"], [dict(track_json, allowStreaming=False)])
    ids = await module.tracks_to_add(module._item(album).id, 5000)
    [added] = await module.get_all(ids)
    assert added.id == ids[0]
    assert added.track.unavailable


async def test_short_upstream_pages_do_not_skip_tracks(
    module, lists, playlist, track_json
):
    rows = numbered(track_json, 80)
    lists.serve(CONTAINER_PATHS["playlist"], rows, cap=lambda offset: 17)
    item = module._item(playlist)
    ids = await module.tracks_to_add(item.id, 5000)
    assert [eid.id for eid in ids] == [str(row["id"]) for row in rows]
    assert [params["offset"] for _, params in lists.calls] == [0, 17, 34, 50, 67]


async def test_mix_add_includes_tracks_beyond_the_first_page(module, session, track_json):
    rows = [session.parse_track(row) for row in numbered(track_json, 80)]
    session.mix = Mock(return_value=SimpleNamespace(items=Mock(return_value=rows)))
    ids = await module.tracks_to_add(cat(route("mix", "daily")), 5000)
    assert [eid.id for eid in ids] == [str(row.id) for row in rows]
    session.mix.assert_called_once_with("daily")


async def test_account_change_discards_queue_metadata(
    module, session, lists, playlist, track_json
):
    session.user = SimpleNamespace(id=1)
    lists.serve(CONTAINER_PATHS["playlist"], [track_json])
    item = module._item(playlist)
    ids = await module.tracks_to_add(item.id, 5000)
    session.user = SimpleNamespace(id=2)
    session.track = Mock(side_effect=ObjectNotFound())
    assert await module.get_all(ids) == []
    session.track.assert_called_once_with("11")


@pytest.mark.parametrize(
    "bts,encryption,codec,url",
    [
        (False, "NONE", "FLAC", "https://cdn.example/segment"),
        (True, "OLD_AES", "FLAC", "https://cdn.example/encrypted"),
        (True, "NONE", "EAC3", "https://cdn.example/atmos"),
        (True, "NONE", "FLAC", "file:///etc/passwd"),
    ],
)
async def test_unsupported_streams_fail_clearly(
    module, track, bts, encryption, codec, url
):
    track.get_stream = Mock(
        return_value=SimpleNamespace(
            is_bts=bts,
            is_mpd=False,
            get_stream_manifest=lambda: SimpleNamespace(
                encryption_type=encryption,
                codecs=codec,
                mime_type="audio/mp4",
                get_urls=lambda: [url],
            ),
        )
    )
    module._item(track)
    with pytest.raises(SourceUnavailableError):
        await module.get_track_source("11")


@pytest.mark.parametrize("codec,quality", [("flac", "LOSSLESS"), ("mp3", "HIGH")])
async def test_real_bts_manifest_returns_renderer_format(module, session, track, codec, quality):
    payload = {
        "mimeType": "audio/flac" if codec == "flac" else "audio/mpeg",
        "codecs": codec,
        "encryptionType": "NONE",
        "urls": [f"https://cdn.example/audio.{codec}?token=private"],
    }
    response = {
        "trackId": 11, "audioQuality": quality,
        "manifestMimeType": "application/vnd.tidal.bts",
        "manifest": base64.b64encode(json.dumps(payload).encode()).decode(),
    }
    session.request.request = Mock(return_value=SimpleNamespace(json=lambda: response))
    module._item(track)
    source = await module.get_track_source("11")
    assert source.format == codec
    assert source.source.url == payload["urls"][0]
    assert session.request.request.call_args.args[2]["audioquality"] == session.config.quality


def bts_stream(**manifest):
    payload = {
        "encryptionType": "NONE",
        "urls": ["https://cdn.example/audio?token=private"],
        **manifest,
    }
    return Stream().parse({
        "trackId": 11,
        "audioQuality": "LOSSLESS",
        "manifestMimeType": "application/vnd.tidal.bts",
        "manifest": base64.b64encode(json.dumps(payload).encode()).decode(),
    })


@pytest.mark.parametrize(
    "codec,mime_type,format",
    [
        ("AAC", "audio/mp4", "audio/mp4"),
        ("mp4a.40.2", "audio/mp4", "audio/mp4"),
        ("mp4a.40.5", "audio/m4a", "audio/mp4"),
        ("FLAC", "audio/mp4", "audio/mp4"),
        ("flac", "audio/flac", "flac"),
        ("FLAC", "audio/x-flac", "flac"),
        ("FLAC", None, "flac"),
        ("MP3", "audio/mpeg", "mp3"),
    ],
)
async def test_bts_stream_format_follows_codec_and_container(
    module, track, codec, mime_type, format
):
    track.get_stream = Mock(return_value=bts_stream(codecs=codec, mimeType=mime_type))
    module._item(track)
    source = await module.get_track_source("11")
    assert source.format == format
    assert source.source.url == "https://cdn.example/audio?token=private"


@pytest.mark.parametrize("codec", ["EAC3", "AC4", "MHA1"])
async def test_bts_stream_with_unplayable_codec_is_refused(module, track, codec):
    track.get_stream = Mock(return_value=bts_stream(codecs=codec, mimeType="audio/mp4"))
    module._item(track)
    with pytest.raises(SourceUnavailableError, match=codec) as caught:
        await module.get_track_source("11")
    assert "private" not in str(caught.value)


async def test_bts_manifest_without_mime_type_is_refused(module, track):
    track.get_stream = Mock(return_value=bts_stream(codecs="FLAC"))
    module._item(track)
    with pytest.raises(SourceUnavailableError, match="invalid stream manifest"):
        await module.get_track_source("11")


@pytest.mark.parametrize(
    "path",
    [
        "https://evil.example/",
        "//evil.example/",
        "pages/../sessions",
        "pages/%2e%2e/sessions",
        "pages/x?token=y",
        "sessions",
        "pages/x\\y",
    ],
)
async def test_untrusted_page_routes_never_reach_the_network(module, path):
    with pytest.raises(LookupError):
        await module.browse(cat(route("page", path)))


async def test_invalid_ids_and_filters_are_rejected(module):
    with pytest.raises(LookupError):
        await module.browse(entity_id(EntityType.CATALOG, "unknown"))
    with pytest.raises(LookupError):
        await module.browse(EntityId(source="other", type=EntityType.ALBUM, id="10"))
    with pytest.raises(UnsupportedFilter):
        await module.browse(
            cat("root"), filter=FilterQuery.model_validate({"genre": {"any": ["jazz"]}})
        )
    with pytest.raises(ValueError):
        await module.browse(cat("root"), offset=-1)
    with pytest.raises(ValueError):
        await module.browse(cat("root"), limit=0)
