"""TIDAL's pages and music entities exposed through the Kalinka catalog API."""

import base64
import hashlib
import io
import json
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from functools import partial
from urllib.parse import unquote, urlsplit

import tidalapi
from tidalapi.mix import Mix, MixV2
from tidalapi.exceptions import ObjectNotFound
from tidalapi.page import PageItem, PageLink

from kalinka_plugin_sdk.datamodel import (
    Album,
    Artist,
    BrowseItem,
    BrowseItemList,
    Catalog,
    CatalogRole,
    CoverImage,
    EmptyList,
    EntityId,
    EntityType,
    FavoriteIds,
    Owner,
    Playlist,
    Preview,
    PreviewContentType,
    PreviewType,
    Track,
)
from kalinka_plugin_sdk.filters import UnsupportedFilter, or_unfiltered
from kalinka_plugin_sdk.inputmodule import (
    ContentInfo,
    DirectUrl,
    InputModule,
    ModuleAsset,
    SearchType,
    SourceUnavailableError,
    TrackInfo,
    TrackSource,
)

from .client import CatalogPage

PAGES = {
    "home": ("Home", "home"),
    "explore": ("Explore", "explore"),
    "genres": ("Genres", "genres"),
    "moods": ("Moods", "moods"),
    "mixes": ("My Mixes", "mixes"),
}
KINDS = {
    EntityType.TRACK: tidalapi.Track,
    EntityType.ALBUM: tidalapi.Album,
    EntityType.ARTIST: tidalapi.Artist,
    EntityType.PLAYLIST: tidalapi.Playlist,
}
MAX_LIMIT = 50
MP4_MIME_TYPES = frozenset({"audio/mp4", "audio/m4a"})


def entity_id(kind, value):
    return EntityId(source="tidal", type=kind, id=str(value))


def route(*parts):
    """Self-contained routes survive server restarts; ':' is reserved by EntityId."""
    data = json.dumps(parts, separators=(",", ":")).encode()
    return "r-" + base64.urlsafe_b64encode(data).decode().rstrip("=")


def parse_route(value):
    try:
        if not value.startswith("r-") or len(value) > 8192:
            raise ValueError
        data = value[2:]
        parts = json.loads(
            base64.b64decode(
                data + "=" * (-len(data) % 4), altchars=b"-_", validate=True
            )
        )
        if (
            not isinstance(parts, list)
            or not parts
            or not all(isinstance(p, str) for p in parts)
        ):
            raise ValueError
        return parts
    except (ValueError, UnicodeError):
        raise LookupError("Unknown TIDAL catalog.") from None


def page_path(value):
    """Only relative catalog paths may reach a client carrying account tokens."""
    parsed = urlsplit(value)
    path = unquote(parsed.path)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or not path.startswith("pages/")
        or "\\" in path
        or any(part in (".", "..", "") for part in path.split("/"))
    ):
        raise LookupError("Unsupported TIDAL page link.")
    return path


def catalog(value, title, *, content=None, role=None, image=None, can_add=False):
    eid = entity_id(EntityType.CATALOG, value)
    return BrowseItem(
        id=eid,
        name=title,
        can_browse=True,
        can_add=can_add,
        catalog=Catalog(
            id=eid,
            title=title,
            image=image,
            role=role,
            preview_config=Preview(
                type=(
                    PreviewType.TILE
                    if content == PreviewContentType.TRACK
                    else PreviewType.IMAGE_TEXT
                ),
                content_type=content,
                items_count=10,
                rows_count=2,
            ),
        ),
    )


def cover(item):
    if item is None:
        return None
    try:
        # 320 and 640 work for artists, albums, playlists and both mix types.
        return CoverImage(
            small=item.image(320), thumbnail=item.image(320), large=item.image(640)
        )
    except (AttributeError, ValueError, TypeError):
        return None


def image_id_cover(value):
    if not value:
        return None
    path = str(value).replace("-", "/")
    return CoverImage(
        **{
            key: f"https://resources.tidal.com/images/{path}/{size}x{size}.jpg"
            for key, size in (("small", 320), ("thumbnail", 320), ("large", 640))
        }
    )


def artist_metadata(item):
    if item is None:
        return None
    return Artist(
        id=entity_id(EntityType.ARTIST, item.id),
        name=item.name or "",
        image=cover(item),
    )


def album_metadata(item, performer=None):
    if item is None:
        return Album(id=entity_id(EntityType.ALBUM, ""), title="", artist=performer)
    return Album(
        id=entity_id(EntityType.ALBUM, item.id),
        title=item.name or "",
        artist=artist_metadata(item.artist) or performer,
        image=cover(item),
        duration=max(0, item.duration or 0),
        track_count=max(0, item.num_tracks or 0),
    )


def window(items, offset, limit):
    return BrowseItemList(
        offset=offset,
        limit=limit,
        total=len(items),
        items=items[offset : offset + limit],
    )


class TidalInputModule(InputModule):
    def __init__(self, client):
        self.client = client
        self.session = client.session
        self._cache = OrderedDict()
        # Container expansion is followed by get_all(). Keep that metadata
        # briefly, including containers larger than the general entity cache.
        self._queue_metadata = OrderedDict()
        self._account_id = None

    def module_name(self):
        return "tidal"

    def display_name(self):
        return "TIDAL"

    def _remember(self, key, value, *, ttl=300):
        self._check_account()
        self._cache[key] = (time.monotonic() + ttl, value)
        self._cache.move_to_end(key)
        while len(self._cache) > 512:
            self._cache.popitem(last=False)
        return value

    def _cached(self, key, load, *, ttl=300):
        self._check_account()
        stored = self._cache.get(key)
        if stored and stored[0] > time.monotonic():
            self._cache.move_to_end(key)
            return stored[1]
        return self._remember(key, load(), ttl=ttl)

    def _check_account(self):
        account_id = getattr(self.session.user, "id", None)
        if account_id != self._account_id:
            self._cache.clear()
            self._queue_metadata.clear()
            self._account_id = account_id

    @staticmethod
    def _pagination(offset, limit):
        if offset < 0 or limit < 1:
            raise ValueError("Offset must be nonnegative and limit must be positive.")
        return offset, min(limit, MAX_LIMIT)

    @staticmethod
    def _validate_id(eid):
        if eid.source != "tidal":
            raise LookupError("This item does not belong to TIDAL.")

    def _entity(self, kind, value):
        if kind not in KINDS:
            raise LookupError("Unsupported TIDAL entity.")
        return self._cached(
            (kind, value), lambda: getattr(self.session, kind.value)(value)
        )

    def _item(self, item):
        if isinstance(item, tidalapi.Track):
            performer = artist_metadata(item.artist)
            metadata = Track(
                id=entity_id(EntityType.TRACK, item.id),
                title=item.full_name or item.name,
                duration=max(0, item.duration or 0),
                performer=performer,
                album=album_metadata(item.album, performer),
                unavailable=not item.available,
                replaygain_gain=item.replay_gain,
                replaygain_peak=item.peak or None,
            )
            result = BrowseItem(
                id=metadata.id,
                name=metadata.title,
                track=metadata,
                subname=performer.name if performer else None,
                can_browse=False,
                can_add=item.available,
            )
        elif isinstance(item, tidalapi.Album):
            metadata = album_metadata(item)
            result = BrowseItem(
                id=metadata.id,
                name=metadata.title,
                album=metadata,
                subname=metadata.artist.name if metadata.artist else None,
                can_browse=True,
                can_add=bool(item.available),
                sections=[
                    catalog(
                        route("tracks", "album", str(item.id)),
                        "Tracks",
                        content=PreviewContentType.TRACK,
                    )
                ],
            )
        elif isinstance(item, tidalapi.Artist):
            metadata = artist_metadata(item)
            result = BrowseItem(
                id=metadata.id,
                name=metadata.name,
                artist=metadata,
                can_browse=True,
                can_add=False,
                sections=[
                    catalog(route("artist", str(item.id), kind), title, content=content)
                    for kind, title, content in (
                        ("top", "Top Tracks", PreviewContentType.TRACK),
                        ("albums", "Albums", PreviewContentType.ALBUM),
                        ("singles", "EPs and Singles", PreviewContentType.ALBUM),
                        ("similar", "Similar Artists", PreviewContentType.ARTIST),
                    )
                ],
            )
        elif isinstance(item, tidalapi.Playlist):
            creator = item.creator
            metadata = Playlist(
                id=entity_id(EntityType.PLAYLIST, item.id),
                name=item.name or "",
                description=item.description,
                image=cover(item),
                track_count=max(0, item.num_tracks or 0),
                duration=max(0, item.duration or 0),
                owner=Owner(
                    id=entity_id(EntityType.USER, getattr(creator, "id", "")),
                    name=getattr(creator, "name", None) or "TIDAL",
                ),
            )
            result = BrowseItem(
                id=metadata.id,
                name=metadata.name,
                playlist=metadata,
                subname=metadata.owner.name,
                can_browse=True,
                can_add=True,
                sections=[
                    catalog(
                        route("tracks", "playlist", str(item.id)),
                        "Tracks",
                        content=PreviewContentType.TRACK,
                    )
                ],
            )
        elif isinstance(item, (Mix, MixV2)):
            return catalog(
                route("mix", str(item.id)),
                item.title,
                image=cover(item),
                content=PreviewContentType.TRACK,
                can_add=True,
            )
        elif isinstance(item, PageLink):
            try:
                path = page_path(item.api_path)
            except LookupError:
                return None
            return catalog(
                route("page", path),
                item.title,
                image=image_id_cover(item.image_id),
                role=CatalogRole.INDEX,
            )
        elif isinstance(item, PageItem):
            if item.type.lower() not in {kind.value for kind in KINDS}:
                return None
            # Promotions are sparse references. Resolve only when opened;
            # fetching each promotion here would make one page N requests.
            return catalog(
                route("promotion", item.type.lower(), str(item.artifact_id)),
                item.short_header or item.header,
                image=image_id_cover(item.image_id),
                can_add=item.type in ("ALBUM", "PLAYLIST", "TRACK"),
            )
        else:
            return None  # Video, article and text-only modules aren't audio.
        self._remember((result.id.type, result.id.id), item)
        return result

    def _items(self, rows):
        return [mapped for row in rows if (mapped := self._item(row)) is not None]

    def _remote_list(self, rows, offset, limit, total=None):
        items = self._items(rows)
        # tidalapi's list methods discard upstream totals. A full page
        # advertises one more item so clients can request the next page.
        if total is None:
            total = offset + len(rows) + int(len(rows) == limit)
        return BrowseItemList(offset=offset, limit=limit, total=total, items=items)

    def _page(self, value):
        def load():
            if value in PAGES:
                return getattr(self.session, PAGES[value][1])()
            parts = parse_route(value)
            if len(parts) == 2 and parts[0] == "page":
                return CatalogPage(self.session, "").get(page_path(parts[1]))
            raise LookupError("Unknown TIDAL page.")

        return self._cached(("page", value), load)

    def _categories(self, value):
        counts = {}
        categories = []
        for category in self._page(value).categories or []:
            rows = getattr(category, "items", None)
            if not isinstance(rows, list):
                continue
            title = getattr(category, "title", None) or "Featured"
            more = getattr(category, "_more", None)
            identity = (
                getattr(category, "module_id", None)
                or f"{getattr(category, 'type', '')}/{title}/{getattr(more, 'api_path', '')}"
            )
            counts[identity] = counts.get(identity, 0) + 1
            categories.append((identity, title, category))
        result = []
        for identity, title, category in categories:
            discriminator = "0"
            if counts[identity] > 1:
                # No upstream id distinguishes these shelves. Content
                # fingerprints survive reordering; changed content makes
                # an old route expire instead of pointing somewhere else.
                discriminator = json.dumps(sorted(
                    (
                        type(item).__name__,
                        str(getattr(item, "type", "")),
                        str(getattr(item, "id", "")),
                        str(getattr(item, "artifact_id", "")),
                        str(getattr(item, "api_path", "")),
                    )
                    for item in category.items
                ))
            key = hashlib.sha256(
                f"{identity}/{discriminator}".encode()
            ).hexdigest()[:20]
            result.append((key, title, category))
        return result

    def _page_sections(self, value):
        return [
            catalog(route("category", value, key), title)
            for key, title, _ in self._categories(value)
        ]

    @staticmethod
    def _root():
        return [
            catalog("home", "Home", role=CatalogRole.INDEX),
            catalog("collection", "My Collection", role=CatalogRole.INDEX),
            catalog("mixes", "My Mixes", role=CatalogRole.INDEX),
        ]

    @staticmethod
    def _collection():
        return [
            catalog(
                "collection-" + kind.value,
                title,
                content=PreviewContentType(kind.value),
                role=CatalogRole.LIBRARY,
            )
            for kind, title in (
                (EntityType.TRACK, "Favorite Tracks"),
                (EntityType.ALBUM, "Favorite Albums"),
                (EntityType.ARTIST, "Favorite Artists"),
                (EntityType.PLAYLIST, "My Playlists"),
            )
        ]

    async def browse(self, entity_id, offset=0, limit=50, filter=None):
        self._validate_id(entity_id)
        offset, limit = self._pagination(offset, limit)
        or_unfiltered(filter).reject_undeclared(())
        if entity_id.type == EntityType.CATALOG and entity_id.id in (
            "root",
            "collection",
        ):
            return window(
                self._root() if entity_id.id == "root" else self._collection(),
                offset,
                limit,
            )
        return await self.client.run(lambda: self._browse(entity_id, offset, limit))

    async def tracks_to_add(self, entity_id, limit):
        self._validate_id(entity_id)
        if limit <= 0 or (
            entity_id.type == EntityType.CATALOG
            and entity_id.id in ("root", "collection")
        ):
            return []

        def expand():
            self._check_account()
            tracks = []
            offset = 0
            while len(tracks) < limit:
                page = self._browse(entity_id, offset, min(MAX_LIMIT, limit - len(tracks)))
                if not page.items and offset < page.total:
                    raise SourceUnavailableError(
                        "TIDAL returned an incomplete track list. Try again."
                    )
                for item in page.items:
                    if item.track is not None:
                        tracks.append(item.id)
                        self._queue_metadata[item.id.to_string] = (
                            time.monotonic() + 30, item
                        )
                        self._queue_metadata.move_to_end(item.id.to_string)
                        while len(self._queue_metadata) > 5000:
                            self._queue_metadata.popitem(last=False)
                        if len(tracks) == limit:
                            break
                offset += len(page.items)
                if offset >= page.total:
                    break
            return tracks

        # All pages share one deadline; a failure aborts the add, so the
        # server never receives a silently truncated container.
        return await self.client.run(expand)

    def _browse(self, eid, offset, limit):
        if eid.type in (EntityType.ALBUM, EntityType.PLAYLIST):
            item = self._entity(eid.type, eid.id)
            total = (
                item.num_tracks
                if item.num_tracks is not None and item.num_tracks >= 0
                else None
            )
            return self._remote_list(
                item.tracks(offset=offset, limit=limit), offset, limit, total
            )
        if eid.type == EntityType.ARTIST:
            artist = self._entity(eid.type, eid.id)
            return self._remote_list(
                artist.get_albums(offset=offset, limit=limit), offset, limit
            )
        if eid.type == EntityType.TRACK:
            return window([self._item(self._entity(eid.type, eid.id))], offset, limit)
        if eid.type != EntityType.CATALOG:
            raise LookupError("Unsupported TIDAL entity.")
        value = eid.id
        if value in PAGES:
            return window(self._page_sections(value), offset, limit)
        if value.startswith("collection-"):
            kind = value.removeprefix("collection-")
            if kind == "playlist":
                rows = self.session.user.playlist_and_favorite_playlists(
                    offset=offset, limit=limit
                )
            elif kind in ("track", "album", "artist"):
                rows = getattr(self.session.user.favorites, kind + "s")(
                    offset=offset, limit=limit
                )
            else:
                raise LookupError("Unknown TIDAL collection.")
            return self._remote_list(rows, offset, limit)
        parts = parse_route(value)
        if parts[0] == "page" and len(parts) == 2:
            return window(self._page_sections(value), offset, limit)
        if parts[0] == "category" and len(parts) == 3:
            for key, _, category in self._categories(parts[1]):
                if key != parts[2]:
                    continue
                items = self._items(category.items)
                more = getattr(category, "_more", None)
                if more and more.api_path:
                    try:
                        path = page_path(more.api_path)
                    except LookupError:
                        pass
                    else:
                        items.append(catalog(route("page", path), more.title or "More"))
                return window(items, offset, limit)
            raise LookupError(
                "This TIDAL section has changed. Open its parent catalog again."
            )
        if parts[0] in ("tracks", "promotion") and len(parts) == 3:
            try:
                kind = EntityType(parts[1])
            except ValueError:
                raise LookupError("Unknown TIDAL catalog.") from None
            return self._browse(entity_id(kind, parts[2]), offset, limit)
        if parts[0] == "mix" and len(parts) == 2:
            rows = self._cached(
                ("mix-tracks", parts[1]), lambda: self.session.mix(parts[1]).items()
            )
            return window(self._items(rows), offset, limit)
        if parts[0] == "artist" and len(parts) == 3:
            artist = self._entity(EntityType.ARTIST, parts[1])
            if parts[2] == "similar":
                rows = self._cached(("similar", parts[1]), artist.get_similar)
                return window(self._items(rows), offset, limit)
            method = {
                "top": "get_top_tracks",
                "albums": "get_albums",
                "singles": "get_albums_ep_singles",
            }.get(parts[2])
            if method:
                return self._remote_list(
                    getattr(artist, method)(offset=offset, limit=limit), offset, limit
                )
        raise LookupError("Unknown TIDAL catalog.")

    async def search(self, type: SearchType, query: str, offset=0, limit=50):
        offset, limit = self._pagination(offset, limit)
        if not query.strip() or offset >= 300:
            return EmptyList(offset, limit)
        kind = EntityType(type.value)

        def search():
            # Kalinka asks for all four kinds concurrently on one serialized client.
            results = self._cached(
                ("search", query, offset, limit),
                lambda: self.session.search(
                    query,
                    models=list(KINDS.values()),
                    offset=offset,
                    limit=min(limit, 300 - offset),
                ),
                ttl=5,
            )
            rows = results[kind.value + "s"]
            result = self._remote_list(rows, offset, limit)
            result.total = min(result.total, 300)
            return result

        return await self.client.run(search)

    async def get(self, entity_id):
        self._validate_id(entity_id)
        if entity_id.type == EntityType.CATALOG:
            item = self._static_catalog(entity_id)
            if item is not None:
                return item
        return await self.client.run(lambda: self._get(entity_id))

    def _static_catalog(self, entity_id):
        for item in [
            catalog("root", "TIDAL", role=CatalogRole.INDEX),
            *self._root(),
            *self._collection(),
            # Keep saved discovery links working without listing them at the root.
            *(catalog(key, title, role=CatalogRole.INDEX)
              for key, (title, _) in PAGES.items()),
        ]:
            if item.id == entity_id:
                return item
        return None

    def _get(self, entity_id):
        self._check_account()
        cached = self._queue_metadata.get(entity_id.to_string)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        if entity_id.type != EntityType.CATALOG:
            return self._item(self._entity(entity_id.type, entity_id.id))
        static = self._static_catalog(entity_id)
        if static is not None:
            return static

        parts = parse_route(entity_id.id)
        if parts[0] == "category" and len(parts) == 3:
            for key, title, _ in self._categories(parts[1]):
                if key == parts[2]:
                    return catalog(entity_id.id, title)
        elif parts[0] == "page" and len(parts) == 2:
            page = self._page(entity_id.id)
            return catalog(
                entity_id.id, page.title or "TIDAL", role=CatalogRole.INDEX
            )
        elif parts[0] == "mix" and len(parts) == 2:
            return self._item(self.session.mix(parts[1]))
        elif parts[0] in ("tracks", "promotion") and len(parts) == 3:
            kind = EntityType(parts[1])
            item = self._item(self._entity(kind, parts[2]))
            return catalog(
                entity_id.id,
                item.name,
                can_add=item.can_add,
                content=(
                    PreviewContentType.TRACK if kind != EntityType.ARTIST else None
                ),
            )
        elif parts[0] == "artist" and len(parts) == 3:
            item = self._item(self._entity(EntityType.ARTIST, parts[1]))
            for section in item.sections:
                if section.id == entity_id:
                    return section
        raise LookupError("Unknown TIDAL catalog.")

    async def get_all(self, entity_ids):
        if not entity_ids:
            return []
        for eid in entity_ids:
            self._validate_id(eid)

        def resolve():
            items = []
            for eid in entity_ids:
                try:
                    items.append(self._get(eid))
                except ObjectNotFound:
                    # The SDK permits missing entities to be omitted. Network
                    # failures and timeouts must fail the whole lookup.
                    continue
                except LookupError as error:
                    if type(error) is not LookupError:
                        # KeyError/IndexError indicate malformed API data.
                        raise
            return items

        # The SDK default gathers per-id calls and discards their exceptions.
        # Submit one operation so executor waiting cannot lose individual ids.
        return await self.client.run(resolve)

    async def get_track_info(self, track_ids):
        if not track_ids:
            return []

        def resolve():
            return [
                TrackInfo(
                    id=entity_id(EntityType.TRACK, tid),
                    metadata=self._get(entity_id(EntityType.TRACK, tid)).track,
                    source_retriever=partial(self.get_track_source, tid),
                )
                for tid in track_ids
            ]

        return await self.client.run(resolve)

    async def get_track_source(self, track_id):
        def resolve():
            # Never cache signed stream URLs: resolve at the moment of play.
            stream = self._entity(EntityType.TRACK, track_id).get_stream()
            if stream.is_mpd:
                self._dash_manifest(stream)
                return TrackSource(
                    source=ModuleAsset(module="tidal", asset_id=f"dash-{track_id}"),
                    format="application/dash+xml",
                )
            if not stream.is_bts:
                raise SourceUnavailableError(
                    "TIDAL returned an unsupported stream manifest."
                )
            try:
                manifest = stream.get_stream_manifest()
            except (KeyError, ValueError):
                raise SourceUnavailableError(
                    "TIDAL returned an invalid stream manifest."
                ) from None
            if manifest.encryption_type not in (None, "NONE"):
                raise SourceUnavailableError(
                    "TIDAL returned an encrypted stream that this player cannot play."
                )
            urls = manifest.get_urls()
            if not urls or urlsplit(urls[0]).scheme not in ("https", "http"):
                raise SourceUnavailableError("TIDAL did not provide a playable stream.")
            return TrackSource(
                source=DirectUrl(url=urls[0]), format=self._direct_format(manifest)
            )

        return await self.client.run(resolve, timeout=7.5)

    @staticmethod
    def _direct_format(manifest):
        codec = str(manifest.codecs).upper().split(".")[0]
        container = str(manifest.mime_type or "").split(";")[0].strip().lower()
        if codec in ("AAC", "MP4A"):
            return "audio/mp4"
        if codec == "FLAC":
            return "audio/mp4" if container in MP4_MIME_TYPES else "flac"
        if codec == "MP3":
            return "mp3"
        raise SourceUnavailableError(
            f"TIDAL returned {codec} audio, which this player cannot play."
        )

    @staticmethod
    def _dash_manifest(stream):
        try:
            data = stream.get_manifest_data().encode("utf-8")
            if (len(data) > 1024 * 1024
                    or b"<!DOCTYPE" in data or b"<!ENTITY" in data):
                raise ValueError
            root = ET.fromstring(data)
            if root.tag != "{urn:mpeg:dash:schema:mpd:2011}MPD":
                raise ValueError
        except (ValueError, ET.ParseError, UnicodeError):
            raise SourceUnavailableError("TIDAL returned an invalid DASH manifest.") from None
        if any(e.tag.rsplit("}", 1)[-1] == "ContentProtection" for e in root.iter()):
            raise SourceUnavailableError("Encrypted TIDAL streams are not supported.")
        return data

    async def get_content_info(self, asset_id):
        if (not asset_id.startswith("dash-")
                or not asset_id[5:].isascii() or not asset_id[5:].isdigit()):
            return None

        def resolve():
            stream = self._entity(EntityType.TRACK, asset_id[5:]).get_stream()
            if not stream.is_mpd:
                raise SourceUnavailableError(
                    "TIDAL changed the stream format. Play the track again."
                )
            data = self._dash_manifest(stream)
            return ContentInfo(
                mime_type="application/dash+xml",
                reader=lambda: io.BytesIO(data), size=len(data), cacheable=False,
            )

        return await self.client.run(resolve)

    # TIDAL account collections are exposed above as read-only catalogs. The
    # legacy favorite/playlist mutation API isn't advertised or emulated.
    async def list_favorite(self, type, filter, offset=0, limit=50):
        return EmptyList(offset, limit)

    async def get_favorite_ids(self):
        return FavoriteIds()

    async def playlist_user_list(self, offset=0, limit=25):
        return EmptyList(offset, limit)

    async def add_to_favorite(self, id):
        raise NotImplementedError("Edit TIDAL favorites in the TIDAL app.")

    async def remove_from_favorite(self, id):
        raise NotImplementedError("Edit TIDAL favorites in the TIDAL app.")

    async def playlist_create(self, name, description):
        raise NotImplementedError("Edit TIDAL playlists in the TIDAL app.")

    async def playlist_update(self, id, name, description):
        raise NotImplementedError("Edit TIDAL playlists in the TIDAL app.")

    async def playlist_delete(self, id):
        raise NotImplementedError("Edit TIDAL playlists in the TIDAL app.")

    async def playlist_add_tracks(self, id, track_ids, allow_duplicates):
        raise NotImplementedError("Edit TIDAL playlists in the TIDAL app.")

    async def playlist_remove_tracks(self, id, playlist_track_ids):
        raise NotImplementedError("Edit TIDAL playlists in the TIDAL app.")

    async def list_filter_values(self, catalog_id, field, offset=0, limit=50, q=""):
        raise UnsupportedFilter(field, "TIDAL catalogs do not declare filters")

    async def get_resource_path(self, id):
        return None
