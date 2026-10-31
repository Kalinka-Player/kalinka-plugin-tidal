import asyncio
from contextlib import suppress
from pathlib import Path
from typing import ClassVar

import tidalapi

from kalinka_plugin_sdk import DynamicFieldDecl, ModuleHealthState, ModuleState, paths
from kalinka_plugin_sdk.plugin import InputModulePlugin, InputPluginContext

from .client import TidalClient
from .config_model import TidalAudioQuality, TidalConfig
from .tidal import TidalInputModule


class KalinkaPluginTidal(InputModulePlugin):
    PLUGIN_ID = "tidal"
    REQUIRES_SDK = ">=3.18,<4"
    CONFIG_MODEL = TidalConfig
    DYNAMIC_FIELDS: ClassVar[dict[str, DynamicFieldDecl]] = {
        "account_status": DynamicFieldDecl(
            section_id="",
            label="TIDAL account",
            widget="rich_text",
        ),
    }

    def __init__(self):
        self.interface = None
        self._client = None
        self._login_task = None

    async def setup(self, context: InputPluginContext):
        config = TidalConfig(**context.config.model_dump())
        session_path = Path(paths.state_dir()) / "tidal" / "session.json"
        if config.unpair:
            session_path.unlink(missing_ok=True)
            session_path.with_name("login.json").unlink(missing_ok=True)
        self._client = TidalClient(
            session_path,
            quality=(
                tidalapi.Quality.hi_res_lossless
                if config.audio_quality == TidalAudioQuality.MAX
                else tidalapi.Quality.high_lossless
            ),
        )
        self.interface = TidalInputModule(self._client)
        self._login_task = asyncio.create_task(self._client.authenticate())

    def get_interface(self):
        return self.interface

    async def get_state(self):
        if self._client is None:
            return ModuleState(state=ModuleHealthState.DISABLED)
        return self._client.state

    async def resolve_dynamic_field(self, path: str):
        if path == "account_status":
            if self._client is None:
                return "Enable TIDAL and restart to sign in."
            return self._client.state.message or "Signed in to TIDAL."
        raise KeyError(path)

    async def shutdown(self):
        if self._login_task is not None:
            self._login_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._login_task
        if self._client is not None:
            await self._client.close()
        self.interface = None
        self._client = None
