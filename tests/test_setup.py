import asyncio
from types import SimpleNamespace

from kalinka_plugin_sdk import ModuleHealthState
from kalinka_plugin_sdk.inputmodule import InputModule
from kalinka_plugin_tidal.client import TidalClient
from kalinka_plugin_tidal.config_model import TidalConfig
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
    await plugin.setup(SimpleNamespace(config=TidalConfig(enabled=True)))
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
