"""TIDAL's offset-addressed lists, read with the counts tidalapi discards."""

from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import quote

# Past this, a page that TIDAL keeps answering short is returned part-filled.
MAX_REQUESTS = 4


@dataclass
class Listing:
    """Where one TIDAL list lives and how each of its entries is parsed."""

    path: str
    parse: Callable[[dict], Any]
    params: dict = field(default_factory=dict)


def item_path(template, value):
    """Ids from clients are interpolated into API paths; keep them one segment."""
    return template % quote(str(value), safe="")


def reported_count(data):
    count = data.get("totalNumberOfItems")
    return count if isinstance(count, int) and count >= 0 else None


def page_total(end, count):
    """What to advertise for a page ending at ``end`` while the list goes on.

    Without TIDAL's count, one more entry than was read lets a client ask
    for the next page.
    """
    return end + 1 if count is None else max(count, end)


def read_page(request, listing, offset, limit):
    """Up to ``limit`` entries from ``offset``, and the size of the list.

    TIDAL can answer a page short while the list goes on, so the page is
    filled from where each answer stopped. Only TIDAL's count, or an empty
    answer given twice, ends the list; the count may include entries TIDAL
    no longer lists, and a one-off empty answer happens mid-list.
    """
    rows = []
    count = None
    retried = False
    for _ in range(MAX_REQUESTS):
        data = request.request(
            "GET",
            listing.path,
            params={
                **listing.params,
                "offset": offset + len(rows),
                "limit": limit - len(rows),
            },
        ).json()
        count = reported_count(data)
        page = request.map_json(
            {"items": data.get("items") or []}, parse=listing.parse
        )
        rows.extend(page[: limit - len(rows)])
        end = offset + len(rows)
        if len(rows) == limit or (count is not None and end >= count):
            break
        if not page:
            if retried:
                return rows, end
            retried = True
    return rows, page_total(offset + len(rows), count)
