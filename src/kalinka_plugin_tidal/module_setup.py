import asyncio
from contextlib import suppress
from pathlib import Path

from kalinka_plugin_sdk import ModuleHealthState, ModuleState, paths
from kalinka_plugin_sdk.plugin import InputModulePlugin, InputPluginContext

from .client import TidalClient
from .config_model import TidalConfig
from .tidal import TidalInputModule


class KalinkaPluginTidal(InputModulePlugin):
    PLUGIN_ID = "tidal"
    REQUIRES_SDK = ">=3.11,<4"
    CONFIG_MODEL = TidalConfig

    def __init__(self):
        self.interface = None
        self._client = None
        self._login_task = None

    async def setup(self, context: InputPluginContext):
        self._client = TidalClient(Path(paths.state_dir()) / "tidal" / "session.json")
        self.interface = TidalInputModule(self._client)
        self._login_task = asyncio.create_task(self._client.authenticate())

    def get_interface(self):
        return self.interface

    async def get_state(self):
        if self._client is None:
            return ModuleState(state=ModuleHealthState.DISABLED)
        return self._client.state

    async def shutdown(self):
        if self._login_task is not None:
            self._login_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._login_task
        if self._client is not None:
            await self._client.close()
        self.interface = None
        self._client = None
