"""Bounded, serialized access to the synchronous tidalapi client."""

import asyncio
import copy
import json
import logging
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import requests
import tidalapi
from tidalapi.exceptions import AuthenticationError, ObjectNotFound, TidalAPIError
from tidalapi.page import Page, PageCategory

from kalinka_plugin_sdk import ModuleHealthState, ModuleState
from kalinka_plugin_sdk.inputmodule import SourceUnavailableError


class _PrivateSessionLogs(logging.Filter):
    def filter(self, record):
        # Upstream debug/error messages include authenticated URLs and raw
        # auth responses. This plugin reports sanitized failures itself.
        # Leave unrelated uses of requests/tidalapi in other threads alone.
        return not record.threadName.startswith("kalinka-tidal")


for _name in (
    "tidalapi.session",
    "tidalapi.request",
    "tidalapi.exceptions",
    "urllib3.connectionpool",
):
    logging.getLogger(_name).addFilter(_PrivateSessionLogs())


class BoundedSession(requests.Session):
    """tidalapi also makes auth requests directly through this transport."""

    deadline = float("inf")

    def request(self, method, url, **kwargs):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise requests.Timeout("TIDAL request timed out")
        kwargs["timeout"] = (min(0.5, remaining), min(1.5, remaining))
        return super().request(method, url, **kwargs)


class CatalogPlaylist(tidalapi.Playlist):
    def factory(self):
        # UserPlaylist fetches its metadata again at construction. Listing
        # owned playlists would otherwise make an extra request per item.
        # This plugin only reads playlists, so the editing wrapper is unused.
        return self


class CatalogPageCategory(PageCategory):
    def parse(self, data):
        category = super().parse(data)
        # V1 parsers discard these fields. Keep them for stable shelf routes.
        category.type = data["type"]
        category.module_id = data.get("moduleId")
        return category


class CatalogPage(Page):
    def __init__(self, session, title):
        super().__init__(session, title)
        self.page_category = CatalogPageCategory(session)


class CatalogSession(tidalapi.Session):
    def __init__(self, config):
        super().__init__(config)
        self.page = CatalogPage(self, "")
        self.parse_page = self.page.parse

    def playlist(self, playlist_id=None):
        return CatalogPlaylist(self, playlist_id)


class TidalClient:
    def __init__(self, session_path: Path, *, quality=tidalapi.Quality.hi_res_lossless):
        self.session_path = session_path
        self.session = CatalogSession(
            tidalapi.Config(quality=quality, item_limit=50)
        )
        self.session.request_session.close()
        self.session.request_session = BoundedSession()
        # The library mutates parsers and authentication state. Even after an
        # asyncio cancellation, an in-flight worker must finish before reuse.
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="kalinka-tidal"
        )
        self._saved = None
        self._closed = False
        self._reauthenticate = asyncio.Event()
        self.ready = False
        self.state = ModuleState(
            state=ModuleHealthState.WARNING, message="Connecting to TIDAL…"
        )

    async def run(self, operation, *, timeout=2.7, authenticated=True):
        if self._closed:
            raise SourceUnavailableError("TIDAL is shutting down.")
        if authenticated and not self.ready:
            raise SourceUnavailableError("Sign in to TIDAL in the module's status.")
        deadline = time.monotonic() + timeout

        def invoke():
            self.session.request_session.deadline = deadline
            if time.monotonic() >= deadline:
                raise requests.Timeout("TIDAL request timed out")
            try:
                return operation()
            finally:
                # Refresh can succeed before the catalog request fails. Keep
                # rotated credentials even when that request returns an error.
                self._save_session()

        try:
            future = asyncio.get_running_loop().run_in_executor(self._executor, invoke)
            return await asyncio.wait_for(future, timeout)
        except ObjectNotFound:
            raise LookupError("This item is no longer available on TIDAL.") from None
        except (AuthenticationError, requests.HTTPError) as error:
            response = getattr(error, "response", None)
            if isinstance(error, AuthenticationError) or (
                response is not None and response.status_code == 401
            ):
                self.ready = False
                self._reauthenticate.set()
                self.state = ModuleState(
                    state=ModuleHealthState.WARNING,
                    message="TIDAL sign-in expired. Reconnecting…",
                )
                raise SourceUnavailableError("TIDAL sign-in expired.") from None
            raise SourceUnavailableError(
                "TIDAL could not complete the request."
            ) from None
        except (TimeoutError, requests.RequestException, TidalAPIError):
            # HTTP errors can contain signed URLs or auth response bodies.
            raise SourceUnavailableError(
                "TIDAL is temporarily unavailable. Try again."
            ) from None

    def _save_session(self):
        if not self.session.access_token or self.session.user is None:
            return
        data = {
            key: {"data": getattr(self.session, key)}
            for key in ("token_type", "access_token", "refresh_token", "is_pkce")
        }
        expiry = self.session.expiry_time
        data["expiry_time"] = {"data": expiry.isoformat() if expiry else None}
        if data == self._saved:
            return
        self.session_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            dir=self.session_path.parent, prefix=".session-"
        )
        try:
            with os.fdopen(fd, "w") as output:
                json.dump(data, output)
            # mkstemp creates mode 0600; replace never exposes a partial file.
            os.replace(temporary, self.session_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        self._saved = data

    def _restore_session(self):
        try:
            data = json.loads(self.session_path.read_text())
            args = {
                key: data[key]["data"]
                for key in ("token_type", "access_token", "refresh_token", "is_pkce")
            }
            if not args["token_type"] or not args["access_token"]:
                return False
            expiry = data.get("expiry_time", {}).get("data")
            args["expiry_time"] = datetime.fromisoformat(expiry) if expiry else None
        except (FileNotFoundError, ValueError, KeyError, TypeError, AttributeError):
            return False
        # Also reads files produced by tidalapi.Session.save_session_to_file.
        return self.session.load_oauth_session(**args)

    async def authenticate(self):
        """Restore a saved session or wait for device authorisation."""
        while True:
            try:
                try:
                    self.ready = await self.run(
                        self._restore_session, timeout=7, authenticated=False
                    )
                except SourceUnavailableError:
                    if not self._reauthenticate.is_set():
                        raise
                    self.ready = False
                self._reauthenticate.clear()
                if not self.ready:
                    login = await self.run(
                        self.session.get_link_login, authenticated=False
                    )
                    url = login.verification_uri_complete
                    if not url.startswith("https://"):
                        url = "https://" + url
                    host = urlsplit(url).hostname or ""
                    if host != "tidal.com" and not host.endswith(".tidal.com"):
                        raise ValueError("Unexpected TIDAL sign-in address")
                    self.state = ModuleState(
                        state=ModuleHealthState.WARNING,
                        message=(
                            f"[Sign in to TIDAL]({url}) and approve this player. "
                            "Kalinka will connect automatically."
                        ),
                    )
                    expires = time.monotonic() + login.expires_in
                    # The library's one-shot poll sleeps before raising
                    # TimeoutError while authorization is pending. Keep that
                    # sleep short; asyncio owns the actual polling interval.
                    poll = copy.copy(login)
                    poll.interval = 1

                    def check_login():
                        try:
                            return self.session.process_link_login(
                                poll, until_expiry=False
                            )
                        except TimeoutError:
                            return False

                    while time.monotonic() < expires:
                        await asyncio.sleep(max(1, login.interval))
                        if await self.run(check_login, timeout=7, authenticated=False):
                            self.ready = True
                            break
                    if not self.ready:
                        continue  # Replace an expired link with a fresh one.
                self.state = ModuleState(state=ModuleHealthState.READY)
                await self._reauthenticate.wait()
            except asyncio.CancelledError:
                raise
            except Exception:
                # No exception repr: upstream errors may contain credentials.
                self.ready = False
                self.state = ModuleState(
                    state=ModuleHealthState.ERROR,
                    message="Could not connect to TIDAL or save its session. Retrying shortly.",
                )
                await asyncio.sleep(30)

    async def close(self):
        self._closed = True
        # Enqueue close behind any cancelled-but-still-running HTTP call.
        await asyncio.get_running_loop().run_in_executor(
            self._executor, self.session.request_session.close
        )
        self._executor.shutdown(wait=False, cancel_futures=True)
