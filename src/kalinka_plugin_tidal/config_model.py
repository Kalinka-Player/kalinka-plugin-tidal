from typing import ClassVar

from pydantic import Field

from kalinka_plugin_sdk.module_config import ModuleConfig


class TidalConfig(ModuleConfig):
    __module_icon__: ClassVar[str] = "music_note_outlined"
    __module_icon_color__: ClassVar[str] = "#007E8A"

    name: str = Field(
        default="tidal",
        title="TIDAL",
        description="Browse and play music from your TIDAL account.",
        frozen=True,
        exclude=True,
    )
    enabled: bool = Field(
        default=False,
        title="Module enabled",
        description=(
            "Enable and restart, then open the sign-in link in this module's "
            "status. A TIDAL account with a streaming subscription is required "
            "for playback."
        ),
        json_schema_extra={"importance": "simple"},
    )
