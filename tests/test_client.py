import asyncio
import json
import logging
import stat
import threading
from datetime import datetime
from functools import partial
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
from tidalapi.exceptions import AuthenticationError, ObjectNotFound, TooManyRequests
from tidalapi.user import LoggedInUser

from kalinka_plugin_sdk import ModuleHealthState
from kalinka_plugin_sdk.datamodel import EntityType
from kalinka_plugin_sdk.inputmodule import SourceUnavailableError
from kalinka_plugin_tidal.client import BoundedSession, TidalClient
from kalinka_plugin_tidal.tidal import TidalInputModule, entity_id


@pytest.fixture
async def client(tmp_path):
    instance = TidalClient(tmp_path / "tidal" / "session.json")
    yield instance
    await instance.close()


def signed_in(client):
    client.session.user = SimpleNamespace(id=1)
    client.session.token_type = "Bearer"
    client.session.access_token = "private-access"
    client.session.refresh_token = "private-refresh"
    client.session.expiry_time = datetime(2030, 1, 1)
    client.ready = True


async def test_credentials_saved_privately_and_refreshed_session_persisted(client):
    signed_in(client)
    await client.run(lambda: None)
    assert stat.S_IMODE(client.session_path.stat().st_mode) == 0o600
    data = json.loads(client.session_path.read_text())
    assert data["access_token"]["data"] == "private-access"
    client.session.access_token = "refreshed-access"
    await client.run(lambda: None)
    assert (
        json.loads(client.session_path.read_text())["access_token"]["data"]
        == "refreshed-access"
    )
    assert [path.name for path in client.session_path.parent.iterdir()] == [
        "session.json"
    ]
    client.session.load_oauth_session = Mock(return_value=True)
    assert await client.run(client._restore_session, authenticated=False)
    client.session.load_oauth_session.assert_called_once_with(
        token_type="Bearer",
        access_token="refreshed-access",
        refresh_token="private-refresh",
        expiry_time=datetime(2030, 1, 1),
        is_pkce=False,
    )


async def test_rotated_credentials_survive_a_subsequent_request_failure(client):
    signed_in(client)

    def fails_after_refresh():
        client.session.refresh_token = "rotated-refresh"
        raise TooManyRequests()

    with pytest.raises(SourceUnavailableError):
        await client.run(fails_after_refresh)
    assert (
        json.loads(client.session_path.read_text())["refresh_token"]["data"]
        == "rotated-refresh"
    )


@pytest.mark.parametrize("data", ["not json", "[]", "{}", '{"expiry_time": []}'])
async def test_corrupt_session_can_be_replaced_by_a_new_login(client, data):
    client.session_path.parent.mkdir()
    client.session_path.write_text(data)
    assert not await client.run(client._restore_session, authenticated=False)


async def test_library_session_files_without_expiry_are_supported(client):
    signed_in(client)
    await client.run(lambda: None)
    data = json.loads(client.session_path.read_text())
    del data["expiry_time"]
    client.session_path.write_text(json.dumps(data))
    client.session.load_oauth_session = Mock(return_value=True)
    assert await client.run(client._restore_session, authenticated=False)
    assert client.session.load_oauth_session.call_args.kwargs["expiry_time"] is None


async def test_not_signed_in_fails_without_making_a_request(client):
    work = Mock()
    with pytest.raises(SourceUnavailableError, match="connecting"):
        await client.run(work)
    client._awaiting_approval = True
    with pytest.raises(SourceUnavailableError, match="Sign in"):
        await client.run(work)
    work.assert_not_called()


async def test_reconnecting_after_a_rejected_request_does_not_ask_to_sign_in(client):
    client.ready = True
    with pytest.raises(SourceUnavailableError):
        await client.run(Mock(side_effect=AuthenticationError()))
    with pytest.raises(SourceUnavailableError, match="connecting"):
        await client.run(Mock())


async def test_device_login_updates_status_and_persists_after_authorization(client):
    link = SimpleNamespace(
        verification_uri_complete="link.tidal.com/EXAMPLE", expires_in=30, interval=0
    )
    client.session.get_link_login = Mock(return_value=link)

    def authorize(*args, **kwargs):
        signed_in(client)
        return True

    client.session.process_link_login = Mock(side_effect=authorize)
    task = asyncio.create_task(client.authenticate())
    try:
        async with asyncio.timeout(2):
            while "Sign in" not in (client.state.message or ""):
                await asyncio.sleep(0.01)
        assert "https://link.tidal.com/EXAMPLE" in client.state.message
        async with asyncio.timeout(3):
            while client.state.state != ModuleHealthState.READY:
                await asyncio.sleep(0.01)
        assert client.ready
        assert client.session_path.exists()
        assert client.session.process_link_login.call_args.kwargs == {
            "until_expiry": False
        }
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.parametrize("pkce", [False, True])
async def test_restored_login_needs_no_device_authorization(client, pkce):
    signed_in(client)
    client.session.is_pkce = pkce
    await client.run(lambda: None)
    client.ready = False
    client.session.load_oauth_session = Mock(return_value=True)
    client.session.get_link_login = Mock(
        side_effect=AssertionError("No new login needed")
    )
    task = asyncio.create_task(client.authenticate())
    try:
        async with asyncio.timeout(2):
            while client.state.state != ModuleHealthState.READY:
                await asyncio.sleep(0.01)
        client.session.get_link_login.assert_not_called()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_revoked_session_starts_reauthentication(client):
    client.ready = True
    with pytest.raises(SourceUnavailableError, match="sign-in expired"):
        await client.run(Mock(side_effect=AuthenticationError("sensitive details")))
    assert not client.ready
    assert client._reauthenticate.is_set()
    assert "sensitive" not in client.state.message


async def wait_for_state(client, state, text=""):
    async with asyncio.timeout(3):
        while client.state.state != state or text not in (client.state.message or ""):
            await asyncio.sleep(0.01)


async def test_expired_sign_in_warns_until_the_player_is_approved_again(client):
    signed_in(client)
    await client.run(lambda: None)
    client.ready = False
    client.session.load_oauth_session = Mock(
        side_effect=AuthenticationError("private-refresh rejected")
    )
    link = SimpleNamespace(
        verification_uri_complete="link.tidal.com/AGAIN", expires_in=30, interval=0
    )
    client.session.get_link_login = Mock(return_value=link)
    approved = threading.Event()

    def authorize(*args, **kwargs):
        if not approved.is_set():
            raise TimeoutError
        signed_in(client)
        return True

    client.session.process_link_login = Mock(side_effect=authorize)
    task = asyncio.create_task(client.authenticate())
    try:
        await wait_for_state(client, ModuleHealthState.WARNING, "sign-in expired")
        assert "https://link.tidal.com/AGAIN" in client.state.message
        assert "private" not in client.state.message
        with pytest.raises(SourceUnavailableError, match="expired. Sign in again"):
            await client.run(lambda: None)
        approved.set()
        await wait_for_state(client, ModuleHealthState.READY)
        assert client.ready and not client.expired
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_idle_session_check_notices_a_revoked_sign_in(client):
    signed_in(client)
    await client.run(lambda: None)
    client.ready = False
    client.session_check_interval = 0.01
    client.session.load_oauth_session = Mock(return_value=True)
    client.session.request.request = Mock(return_value=None)
    link = SimpleNamespace(
        verification_uri_complete="link.tidal.com/AGAIN", expires_in=30, interval=0
    )
    client.session.get_link_login = Mock(return_value=link)
    client.session.process_link_login = Mock(side_effect=TimeoutError)
    task = asyncio.create_task(client.authenticate())
    try:
        await wait_for_state(client, ModuleHealthState.READY)
        async with asyncio.timeout(2):
            while not client.session.request.request.called:
                await asyncio.sleep(0.01)
        client.session.load_oauth_session.side_effect = AuthenticationError()
        client.session.request.request.side_effect = AuthenticationError()
        await wait_for_state(client, ModuleHealthState.WARNING, "[Sign in to TIDAL]")
        assert "sign-in expired" in client.state.message
        assert not client.ready
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_session_in_use_is_not_checked_until_idle(client):
    signed_in(client)
    await client.run(lambda: None)
    client.ready = False
    client.session_check_interval = 0.2
    client.session.load_oauth_session = Mock(return_value=True)
    client.session.request.request = Mock(return_value=None)
    task = asyncio.create_task(client.authenticate())
    try:
        await wait_for_state(client, ModuleHealthState.READY)
        for _ in range(10):
            await client.run(lambda: None)
            await asyncio.sleep(0.05)
        client.session.request.request.assert_not_called()
        async with asyncio.timeout(2):
            while not client.session.request.request.called:
                await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_idle_session_check_outage_keeps_the_sign_in(client):
    signed_in(client)
    await client.run(lambda: None)
    client.ready = False
    client.session_check_interval = 0.01
    client.session.load_oauth_session = Mock(return_value=True)
    client.session.request.request = Mock(side_effect=requests.ConnectionError())
    client.session.get_link_login = Mock(
        side_effect=AssertionError("No new login needed")
    )
    task = asyncio.create_task(client.authenticate())
    try:
        await wait_for_state(client, ModuleHealthState.READY)
        async with asyncio.timeout(2):
            while client.session.request.request.call_count < 2:
                await asyncio.sleep(0.01)
        assert client.ready
        assert client.state.state == ModuleHealthState.READY
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def unauthorized():
    response = requests.Response()
    response.status_code = 401
    response._content = b'{"subStatus": 4005, "userMessage": "secret"}'
    return requests.HTTPError("secret", response=response)


async def test_refused_item_does_not_expire_a_valid_sign_in(client):
    signed_in(client)
    client.session.request.request = Mock(return_value=None)
    with pytest.raises(SourceUnavailableError, match="does not allow") as caught:
        await client.run(Mock(side_effect=unauthorized()))
    assert "secret" not in str(caught.value)
    client.session.request.request.assert_called_once_with("GET", "sessions")
    assert client.ready and not client.expired
    assert not client._reauthenticate.is_set()


async def test_unauthorized_item_and_session_expire_the_sign_in(client):
    signed_in(client)
    client.session.request.request = Mock(side_effect=unauthorized())
    with pytest.raises(SourceUnavailableError, match="sign-in expired"):
        await client.run(Mock(side_effect=unauthorized()))
    assert not client.ready and client.expired
    assert client._reauthenticate.is_set()


def token_reply(monkeypatch, status):
    def reply(session, method, url, **kwargs):
        assert url == "https://auth.tidal.com/v1/oauth2/token"
        response = requests.Response()
        response.status_code = status
        response._content = b'{"error": "secret", "error_description": "secret"}'
        return response

    monkeypatch.setattr(requests.Session, "request", reply)


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_auth_server_outage_keeps_the_sign_in(client, monkeypatch, status):
    signed_in(client)
    token_reply(monkeypatch, status)
    with pytest.raises(SourceUnavailableError, match="temporarily") as caught:
        await client.run(lambda: client.session.token_refresh("private-refresh"))
    assert "secret" not in str(caught.value)
    assert client.ready and not client.expired
    assert not client._reauthenticate.is_set()


@pytest.mark.parametrize("status", [400, 401])
async def test_rejected_refresh_token_expires_the_sign_in(client, monkeypatch, status):
    signed_in(client)
    token_reply(monkeypatch, status)
    with pytest.raises(SourceUnavailableError, match="sign-in expired"):
        await client.run(lambda: client.session.token_refresh("private-refresh"))
    assert not client.ready and client.expired
    assert client._reauthenticate.is_set()


@pytest.mark.parametrize(
    "error",
    [
        requests.Timeout("secret"),
        requests.ConnectionError("secret"),
        TooManyRequests("secret"),
    ],
)
async def test_transient_errors_are_sanitized_without_losing_sign_in(client, error):
    client.ready = True
    with pytest.raises(SourceUnavailableError) as caught:
        await client.run(Mock(side_effect=error))
    assert "secret" not in str(caught.value)
    assert client.ready


async def test_missing_items_are_distinct_from_outages(client):
    client.ready = True
    with pytest.raises(LookupError):
        await client.run(Mock(side_effect=ObjectNotFound()))


async def test_timed_out_worker_does_not_block_event_loop_or_race_next_call(client):
    client.ready = True
    started = threading.Event()
    release = threading.Event()

    def blocking():
        started.set()
        release.wait(2)

    try:
        with pytest.raises(SourceUnavailableError):
            await client.run(blocking, timeout=0.05)
        assert started.is_set()
        second = Mock(return_value=42)
        pending = asyncio.create_task(client.run(second))
        await asyncio.sleep(0.02)
        second.assert_not_called()
        release.set()
        assert await pending == 42
    finally:
        release.set()


async def test_upstream_logs_do_not_leak_credentials(client, caplog):
    client.ready = True
    with caplog.at_level(logging.DEBUG):
        await client.run(
            lambda: logging.getLogger("tidalapi.session").error("private-access")
        )
        await client.run(
            lambda: logging.getLogger("urllib3.connectionpool").debug("private-refresh")
        )
    assert "private-access" not in caplog.text
    assert "private-refresh" not in caplog.text


def test_transport_bounds_all_http_calls(monkeypatch):
    request = Mock(return_value=object())
    monkeypatch.setattr(requests.Session, "request", request)
    session = BoundedSession()
    session.get("https://api.tidal.com/v1/sessions")
    assert request.call_args.kwargs["timeout"] == (0.5, 1.5)
    session.deadline = 0
    with pytest.raises(requests.Timeout):
        session.post("https://auth.tidal.com/v1/oauth2/token")
    assert request.call_count == 1
    session.close()


async def test_owned_playlists_are_listed_in_one_request(client):
    session = client.session
    session.user = LoggedInUser(session, 42)
    playlist = {
        "uuid": "owned-playlist",
        "title": "Mine",
        "numberOfTracks": 3,
        "numberOfVideos": 0,
        "description": "",
        "duration": 600,
        "type": "USER",
        "image": None,
        "squareImage": None,
        "promotedArtists": [],
        "creator": {"id": 42, "name": "Me"},
    }
    response = {"items": [{"playlist": playlist, "created": "2026-01-01T00:00:00Z"}]}
    session.request.request = Mock(return_value=SimpleNamespace(json=lambda: response))
    rows = await client.run(
        lambda: session.user.playlist_and_favorite_playlists(offset=0, limit=50),
        authenticated=False,
    )
    assert [row.name for row in rows] == ["Mine"]
    session.request.request.assert_called_once()


async def test_bulk_timeout_fails_instead_of_returning_a_partial_queue(client, track):
    client.ready = True
    client.run = partial(client.run, timeout=0.05)
    module = TidalInputModule(client)
    cached = module._item(track)
    release = threading.Event()

    def delayed(tid):
        release.wait(2)
        return track

    client.session.track = Mock(side_effect=delayed)
    try:
        with pytest.raises(SourceUnavailableError):
            await module.get_all([cached.id, entity_id(EntityType.TRACK, "uncached")])
    finally:
        release.set()


async def test_bulk_missing_ids_are_omitted_but_transient_failures_abort(client, track):
    client.ready = True
    module = TidalInputModule(client)
    cached = module._item(track)
    missing = entity_id(EntityType.TRACK, "missing")
    client.session.track = Mock(side_effect=ObjectNotFound())
    assert [item.id for item in await module.get_all([cached.id, missing, cached.id])] == [
        cached.id, cached.id
    ]
    client.session.track = Mock(side_effect=TooManyRequests())
    with pytest.raises(SourceUnavailableError):
        await module.get_all([cached.id, missing])
    client.session.track = Mock(side_effect=KeyError("malformed track"))
    with pytest.raises(KeyError):
        await module.get_all([cached.id, missing])


async def test_later_container_page_error_aborts_the_whole_add(client, playlist, track):
    client.ready = True
    module = TidalInputModule(client)
    playlist.num_tracks = 80
    playlist.tracks = Mock(side_effect=[[track] * 50, requests.Timeout()])
    item = module._item(playlist)
    with pytest.raises(SourceUnavailableError):
        await module.tracks_to_add(item.id, 5000)
