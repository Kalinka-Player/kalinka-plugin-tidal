from importlib.metadata import entry_points
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from tidalapi.page import Page, PageItem, PageLink

from kalinka_plugin_sdk.datamodel import EntityId, EntityType
from kalinka_plugin_sdk.filters import FilterQuery, UnsupportedFilter
from kalinka_plugin_sdk.inputmodule import SearchType, SourceUnavailableError
from kalinka_plugin_tidal.config_model import TidalConfig
from kalinka_plugin_tidal.tidal import TidalInputModule, entity_id, route


def cat(value):
    return entity_id(EntityType.CATALOG, value)


def test_plugin_discovery():
    plugin = next(
        ep
        for ep in entry_points(group="kalinka.plugins")
        if ep.name == "kalinka_plugin_tidal"
    ).load()
    assert plugin.PLUGIN_ID == "tidal"
    assert plugin.CONFIG_MODEL is TidalConfig
    assert not TidalConfig().enabled


async def test_root_is_paginated_and_available_before_sign_in(module):
    module.client.run = Mock(side_effect=AssertionError("No network for the root"))
    first = await module.browse(cat("root"), limit=2)
    rest = await module.browse(cat("root"), offset=2)
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
    page = Page(session, "").parse(data)
    session.home = Mock(return_value=page)
    shelves = await module.browse(cat("home"))
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
        more = (await module.browse(shelf.id, offset=1)).items[0]
        session.request.request = Mock(return_value=SimpleNamespace(json=lambda: data))
        assert (await restarted.browse(more.id)).items[0].name == "New Releases"
        assert session.request.request.call_args.args[:2] == ("GET", "pages/new")


async def test_duplicate_shelf_titles_have_distinct_ids_and_reordering_is_safe(
    module, session, album, track
):
    first = SimpleNamespace(title="Featured", type="ALBUM_LIST", items=[album])
    second = SimpleNamespace(title="Featured", type="TRACK_LIST", items=[track])
    page = SimpleNamespace(categories=[first, second])
    session.explore = Mock(return_value=page)
    shelves = (await module.browse(cat("explore"))).items
    assert shelves[0].id != shelves[1].id
    page.categories.reverse()
    module._cache.clear()
    assert (await module.browse(shelves[0].id)).items[0].album is not None


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


async def test_nested_genre_links_and_promotions_are_lazy(module, session, album):
    link = PageLink(
        session,
        {
            "title": "Jazz",
            "icon": None,
            "apiPath": "pages/genre/jazz",
            "imageId": "abc-def",
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
    assert result.items[0].catalog.image.large.endswith("640x640.jpg")
    album.tracks = Mock(return_value=[])
    session.album = Mock(return_value=album)
    await module.browse(result.items[1].id)
    session.album.assert_called_once_with("10")
    album.tracks.assert_called_once_with(offset=0, limit=50)


async def test_album_and_playlist_tracks_keep_metadata_and_order(
    module, album, playlist, track
):
    for parent in (album, playlist):
        parent.tracks = Mock(return_value=[track])
        item = module._item(parent)
        result = await module.browse(item.id, offset=2, limit=1)
        assert result.total == 3
        assert result.items[0].track.title == "A Song (Live)"
        assert result.items[0].track.duration == 200
        assert result.items[0].track.performer.name == "An Artist"
        assert result.items[0].track.album.image.large.endswith("640x640.jpg")
        assert result.items[0].track.replaygain_gain == -5.4
        assert (
            await module.browse(item.sections[0].id, offset=2, limit=1)
        ).items == result.items
        parent.tracks.assert_called_with(offset=2, limit=1)


async def test_artist_sections_use_the_corresponding_api(module, artist, album, track):
    artist.get_albums = Mock(return_value=[album])
    artist.get_albums_ep_singles = Mock(return_value=[album])
    artist.get_top_tracks = Mock(return_value=[track])
    artist.get_similar = Mock(return_value=[])
    item = module._item(artist)
    for section in item.sections:
        await module.browse(section.id, offset=2, limit=3)
    artist.get_albums.assert_called_once_with(offset=2, limit=3)
    artist.get_albums_ep_singles.assert_called_once_with(offset=2, limit=3)
    artist.get_top_tracks.assert_called_once_with(offset=2, limit=3)
    artist.get_similar.assert_called_once_with()


@pytest.mark.parametrize("kind", list(SearchType))
async def test_search_selects_one_model_and_paginates(
    module, session, kind, album, track, artist, playlist
):
    item = {"album": album, "track": track, "artist": artist, "playlist": playlist}[
        kind.value
    ]
    session.search = Mock(return_value={kind.value + "s": [item]})
    result = await module.search(kind, "Music", offset=50, limit=1)
    assert result.total == 52
    assert result.items[0].id.type.value == kind.value
    assert session.search.call_args.kwargs == {
        "models": [type(item)],
        "offset": 50,
        "limit": 1,
    }
    assert (await module.search(kind, "", offset=0)).total == 0
    assert (await module.search(kind, "Music", offset=300)).total == 0
    assert session.search.call_count == 1


async def test_collection_uses_paged_library_calls(module, session, track, playlist):
    favorites = SimpleNamespace(
        tracks=Mock(return_value=[track]),
        albums=Mock(return_value=[]),
        artists=Mock(return_value=[]),
    )
    session.user = SimpleNamespace(
        favorites=favorites,
        playlist_and_favorite_playlists=Mock(return_value=[playlist]),
    )
    for shelf in (await module.browse(cat("collection"))).items:
        await module.browse(shelf.id, offset=50, limit=100)
    favorites.tracks.assert_called_once_with(offset=50, limit=50)
    session.user.playlist_and_favorite_playlists.assert_called_once_with(
        offset=50, limit=50
    )


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
        get_stream_manifest=Mock(
            return_value=SimpleNamespace(
                encryption_type="NONE",
                codecs="FLAC",
                get_urls=lambda: ["https://cdn.example/audio.flac"],
            )
        ),
    )
    track.get_stream = Mock(return_value=stream)
    module._item(track)
    info = (await module.get_track_info(["11"]))[0]
    track.get_stream.assert_not_called()
    assert info.metadata.title == "A Song (Live)"
    assert (await info.source_retriever()).format == "flac"
    assert (
        await module.get_track_source("11")
    ).source.url == "https://cdn.example/audio.flac"
    assert track.get_stream.call_count == 2


@pytest.mark.parametrize(
    "bts,encryption,codec,url",
    [
        (False, "NONE", "FLAC", "https://cdn.example/segment"),
        (True, "OLD_AES", "FLAC", "https://cdn.example/encrypted"),
        (True, "NONE", "MP4A", "https://cdn.example/aac"),
        (True, "NONE", "FLAC", "file:///etc/passwd"),
    ],
)
async def test_unsupported_streams_fail_clearly(
    module, track, bts, encryption, codec, url
):
    track.get_stream = Mock(
        return_value=SimpleNamespace(
            is_bts=bts,
            get_stream_manifest=lambda: SimpleNamespace(
                encryption_type=encryption,
                codecs=codec,
                get_urls=lambda: [url],
            ),
        )
    )
    module._item(track)
    with pytest.raises(SourceUnavailableError):
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
