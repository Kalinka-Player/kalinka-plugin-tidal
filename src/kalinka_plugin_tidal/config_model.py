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
    unpair: bool = Field(
        default=False,
        title="Forget TIDAL account on next restart",
        json_schema_extra={
            "help": (
                "Clear the saved authorization on this player. Apply and restart, "
                "then use Sign in to TIDAL to choose an account. If the browser "
                "remembers the old account, sign out there first. This switch "
                "resets automatically."
            ),
            "one_shot": True,
            "importance": "simple",
        },
    )
