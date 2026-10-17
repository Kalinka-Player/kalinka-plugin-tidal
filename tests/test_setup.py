import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import tidalapi

from kalinka_plugin_sdk import ModuleHealthState, ModuleState, paths
from kalinka_plugin_sdk.inputmodule import InputModule
from kalinka_plugin_tidal.client import TidalClient
from kalinka_plugin_tidal.config_model import TidalAudioQuality, TidalConfig
from kalinka_plugin_tidal.module_setup import KalinkaPluginTidal


async def test_setup_returns_while_login_is_pending_and_shutdown_cancels_it(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("KALINKA_PREFIX", str(tmp_path))
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def pending_login(self):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(TidalClient, "authenticate", pending_login)
    plugin = KalinkaPluginTidal()
    await plugin.setup(SimpleNamespace(config=TidalConfig()))
    await asyncio.wait_for(started.wait(), 1)
    assert isinstance(plugin.get_interface(), InputModule)
    assert (await plugin.get_state()).state == ModuleHealthState.WARNING
    client = plugin._client
    assert str(client.session_path).startswith(str(tmp_path))
    await asyncio.wait_for(plugin.shutdown(), 1)
    assert cancelled.is_set()
    assert client._closed
    assert plugin.get_interface() is None
    assert (await plugin.get_state()).state == ModuleHealthState.DISABLED
    await plugin.shutdown()


@pytest.mark.parametrize("unpair,stored", [(False, True), (True, True), (True, False)])
async def test_forget_account_happens_before_authentication(
    monkeypatch, tmp_path, unpair, stored
):
    monkeypatch.setenv("KALINKA_PREFIX", str(tmp_path))
    session_path = Path(paths.state_dir()) / "tidal" / "session.json"
    if stored:
        session_path.parent.mkdir(parents=True)
        session_path.write_text("old-session")
        session_path.with_name("login.json").write_text("old-challenge")
    started = asyncio.Event()
    observed = []

    async def authenticate(self):
        observed.append(self.session_path.exists())
        started.set()

    monkeypatch.setattr(TidalClient, "authenticate", authenticate)
    plugin = KalinkaPluginTidal()
    try:
        await plugin.setup(SimpleNamespace(config=TidalConfig(enabled=True, unpair=unpair)))
        await asyncio.wait_for(started.wait(), 1)
        assert observed == [stored and not unpair]
        assert session_path.with_name("login.json").exists() is (stored and not unpair)
        assert plugin._client.session.config.quality == tidalapi.Quality.hi_res_lossless
    finally:
        await plugin.shutdown()


async def test_forget_failure_does_not_restore_the_old_account(monkeypatch, tmp_path):
    monkeypatch.setenv("KALINKA_PREFIX", str(tmp_path))
    monkeypatch.setattr(Path, "unlink", Mock(side_effect=PermissionError("Cannot remove session")))
    authenticate = Mock()
    monkeypatch.setattr(TidalClient, "authenticate", authenticate)
    plugin = KalinkaPluginTidal()
    with pytest.raises(PermissionError):
        await plugin.setup(SimpleNamespace(config=TidalConfig(enabled=True, unpair=True)))
    authenticate.assert_not_called()
    assert plugin._client is None
    await plugin.shutdown()


async def test_forgotten_account_can_be_replaced_and_restored(monkeypatch, tmp_path):
    monkeypatch.setenv("KALINKA_PREFIX", str(tmp_path))
    session_path = Path(paths.state_dir()) / "tidal" / "session.json"
    session_path.parent.mkdir(parents=True)
    session_path.write_text("old-session")
    link = SimpleNamespace(
        verification_uri_complete="link.tidal.com/EXAMPLE", expires_in=30, interval=0
    )
    login = Mock(return_value=link)
    monkeypatch.setattr(tidalapi.Session, "get_link_login", login)

    def authorize(session, *args, **kwargs):
        session.user = SimpleNamespace(id=2)
        session.token_type = "Bearer"
        session.access_token = "new-test-access"
        session.refresh_token = "new-test-refresh"
        return True

    monkeypatch.setattr(tidalapi.Session, "process_link_login", authorize)

    async def wait_ready(plugin):
        async with asyncio.timeout(3):
            while (await plugin.get_state()).state != ModuleHealthState.READY:
                await asyncio.sleep(0.01)

    plugin = KalinkaPluginTidal()
    try:
        await plugin.setup(SimpleNamespace(config=TidalConfig(enabled=True, unpair=True)))
        await wait_ready(plugin)
        assert json.loads(session_path.read_text())["access_token"]["data"] == "new-test-access"
    finally:
        await plugin.shutdown()

    restored = Mock(return_value=True)
    monkeypatch.setattr(tidalapi.Session, "load_oauth_session", restored)
    restarted = KalinkaPluginTidal()
    try:
        await restarted.setup(SimpleNamespace(config=TidalConfig(enabled=True)))
        await wait_ready(restarted)
        restored.assert_called_once()
        assert restored.call_args.kwargs["access_token"] == "new-test-access"
        login.assert_called_once()
    finally:
        await restarted.shutdown()


async def test_settings_show_device_sign_in_status():
    plugin = KalinkaPluginTidal()
    fields = plugin.DYNAMIC_FIELDS
    assert fields["account_status"].widget == "rich_text"
    assert "audio_quality" not in fields
    assert "restart to sign in" in await plugin.resolve_dynamic_field("account_status")
    link = "[Sign in to TIDAL](https://link.tidal.com/EXAMPLE) to open your catalogs."
    plugin._client = SimpleNamespace(
        state=ModuleState(state=ModuleHealthState.WARNING, message=link)
    )
    assert await plugin.resolve_dynamic_field("account_status") == link
    plugin._client.state = ModuleState(state=ModuleHealthState.READY)
    assert await plugin.resolve_dynamic_field("account_status") == "Signed in to TIDAL."
    with pytest.raises(KeyError):
        await plugin.resolve_dynamic_field("unknown")


def test_forget_account_is_an_opt_in_one_shot_setting():
    assert TidalConfig().unpair is False
    field = TidalConfig.model_fields["unpair"]
    assert field.json_schema_extra["widget"] == "toggle"
    assert field.json_schema_extra["one_shot"] is True
    assert field.json_schema_extra["importance"] == "simple"


@pytest.mark.parametrize("quality,requested", [
    (TidalAudioQuality.LOSSLESS, tidalapi.Quality.high_lossless),
    (TidalAudioQuality.MAX, tidalapi.Quality.hi_res_lossless),
])
async def test_quality_setting_reaches_tidal_stream_request(
    monkeypatch, tmp_path, quality, requested
):
    monkeypatch.setenv("KALINKA_PREFIX", str(tmp_path))

    async def authenticate(self):
        self.ready = True

    monkeypatch.setattr(TidalClient, "authenticate", authenticate)
    plugin = KalinkaPluginTidal()
    try:
        await plugin.setup(SimpleNamespace(config=TidalConfig(audio_quality=quality)))
        await plugin._login_task
        session = plugin._client.session
        track = tidalapi.Track(session)
        track.id = 11
        plugin.interface._entity = Mock(return_value=track)
        session.request.request = Mock(return_value=SimpleNamespace(json=lambda: {
            "trackId": 11, "audioQuality": requested,
            "manifestMimeType": "application/dash+xml", "manifest": "",
        }))
        plugin.interface._dash_manifest = Mock(return_value=b"manifest")
        await plugin.interface.get_track_source("11")
        await plugin.interface.get_content_info("dash-11")
        assert session.request.request.call_count == 2
        for call in session.request.request.call_args_list:
            assert call.args[2]["audioquality"] == requested
    finally:
        await plugin.shutdown()


def test_obsolete_browser_settings_are_not_exposed_or_used():
    config = TidalConfig(login_method="browser", login_response="private-response")
    assert "login_method" not in config.model_dump()
    assert "login_response" not in config.model_dump()
    assert "private-response" not in repr(config)
    assert config.audio_quality == TidalAudioQuality.MAX
