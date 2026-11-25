"""The account's favourites on TIDAL: one set of v1 endpoints for every kind."""

import uuid

from kalinka_plugin_sdk.datamodel import EntityType
from kalinka_plugin_sdk.inputmodule import SourceUnavailableError

from .paging import Listing, item_path

# What TIDAL calls each kind: its key among the ids, and the form field its
# id is marked through.
NAMES = {
    EntityType.TRACK: ("TRACK", "trackId"),
    EntityType.ALBUM: ("ALBUM", "albumId"),
    EntityType.ARTIST: ("ARTIST", "artistId"),
    EntityType.PLAYLIST: ("PLAYLIST", "uuids"),
}
NEWEST_FIRST = {"order": "DATE", "orderDirection": "DESC"}


def favourite_target(eid):
    """The kind and TIDAL id ``eid`` names, where it can be a favourite.

    TIDAL reads a comma in an id as several items, so an id is checked to
    name exactly one before it is sent.
    """
    if eid.source != "tidal":
        raise LookupError("This item does not belong to TIDAL.")
    if eid.type not in NAMES:
        raise ValueError(
            "TIDAL keeps favourite tracks, albums, artists and playlists only."
        )
    if eid.type == EntityType.PLAYLIST:
        try:
            uuid.UUID(eid.id)
        except ValueError:
            raise LookupError("This playlist is not on TIDAL.") from None
    elif not (eid.id.isascii() and eid.id.isdigit()):
        raise LookupError("This item is not on TIDAL.")
    return eid.type, eid.id


class Favourites:
    """Reads and changes the signed-in account's favourites.

    Playlists go through v1 like the rest: the v2 removal tidalapi uses
    deletes a playlist the account owns instead of unmarking it.
    """

    def __init__(self, session):
        self.session = session

    def _path(self, kind=None):
        path = item_path("users/%s/favorites", self.session.user.id)
        return path if kind is None else f"{path}/{kind.value}s"

    def listing(self, kind, parse):
        return Listing(self._path(kind), parse, NEWEST_FIRST)

    def ids(self):
        """Every favourite's TIDAL id by kind, whole, in one request."""
        body = self.session.request.request("GET", self._path() + "/ids").json()
        try:
            return {
                kind: [str(value) for value in body[key]]
                for kind, (key, _) in NAMES.items()
            }
        except (KeyError, TypeError):
            raise SourceUnavailableError(
                "TIDAL returned favourites Kalinka cannot read."
            ) from None

    def add(self, kind, value):
        self.session.request.request(
            "POST", self._path(kind), data={NAMES[kind][1]: value}
        )

    def remove(self, kind, value):
        self.session.request.request(
            "DELETE", self._path(kind) + item_path("/%s", value)
        )
