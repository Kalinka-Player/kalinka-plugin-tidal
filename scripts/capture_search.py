"""Record what this plugin's search returns, for the server's ranking evaluation.

Writes one JSON fixture per query: the hits of every kind, in the order the
plugin returns them, as the server receives them. The evaluation in
KalinkaPlayer (packages/kalinka-server/tests/search_eval) replays them.

    python scripts/capture_search.py --out DIR "pink floyd" "abbey road"

Without --session the catalogue is read anonymously, with the public client
token, in --country. With --session, a copy of a player's TIDAL session.json is
used and refreshed tokens are written back to that copy.
"""

import argparse
import asyncio
import json
import os
import re
from datetime import date
from pathlib import Path

import tidalapi

from kalinka_plugin_sdk.inputmodule import SearchType

from kalinka_plugin_tidal.client import TidalClient
from kalinka_plugin_tidal.tidal import TidalInputModule


def slim(value):
    """Ranking reads whether an image exists, not its sizes."""
    if isinstance(value, dict):
        return {
            key: (
                {"large": item.get("large", "")}
                if key == "image" and isinstance(item, dict)
                else slim(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [slim(item) for item in value]
    return value


def dump(fixture):
    """One hit per line, so a recapture diffs hit by hit."""
    head = json.dumps(
        {key: value for key, value in fixture.items() if key != "kinds"},
        ensure_ascii=False,
    )
    kinds = ",\n".join(
        f' "{kind}": [\n'
        + ",\n".join("  " + json.dumps(item, ensure_ascii=False) for item in items)
        + "\n ]"
        for kind, items in fixture["kinds"].items()
    )
    return head[:-1] + ', "kinds": {\n' + kinds + "\n}}\n"


async def capture(module, query, limit):
    kinds = {}
    for kind in SearchType:
        result = await module.search(kind, query, 0, limit)
        kinds[kind.value] = [
            slim(item.model_dump(mode="json", exclude_none=True, exclude={"sections"}))
            for item in result.items
        ]
    return kinds


async def main(args):
    client = TidalClient(args.session or Path(os.devnull))
    try:
        if args.session:
            client.ready = await client.run(
                client._restore_session, timeout=10, authenticated=False
            )
            if not client.ready:
                raise SystemExit(f"{args.session} holds no usable TIDAL session")
            origin = "signed-in session"
        else:
            client.session.country_code = args.country
            client.session.request_session.headers["x-tidal-token"] = (
                tidalapi.Config().client_id
            )
            client.ready = True
            origin = f"anonymous, country {args.country}"
        module = TidalInputModule(client)
        args.out.mkdir(parents=True, exist_ok=True)
        for query in args.queries:
            fixture = {
                "query": query,
                "source": "tidal",
                "ranked": True,
                "captured": date.today().isoformat(),
                "origin": origin,
                "kinds": await capture(module, query, args.limit),
            }
            name = re.sub(r"[^a-z0-9]+", "_", query.casefold()).strip("_")
            path = args.out / f"tidal_{name}.json"
            path.write_text(dump(fixture))
            print(path)
    finally:
        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("queries", nargs="+")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--session", type=Path)
    parser.add_argument("--country", default="US")
    parser.add_argument("--limit", type=int, default=50)
    asyncio.run(main(parser.parse_args()))
