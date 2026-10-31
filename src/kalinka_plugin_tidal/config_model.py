from enum import Enum
from pathlib import Path
from typing import ClassVar

from pydantic import Field

from kalinka_plugin_sdk import ModuleIconFiles
from kalinka_plugin_sdk.module_config import ModuleConfig


class TidalAudioQuality(str, Enum):
    LOSSLESS = "Lossless — 16-bit / 44.1 kHz"
    MAX = "Max — up to 24-bit / 192 kHz"


class TidalConfig(ModuleConfig):
    __module_icon__: ClassVar[str] = "music_note_outlined"
    __module_icon_color__: ClassVar[str] = "#007E8A"
    __module_icon_files__: ClassVar[ModuleIconFiles] = ModuleIconFiles(
        small=Path(__file__).with_name("icons") / "tidal.svg",
        standard=Path(__file__).with_name("icons") / "tidal.svg",
        monochrome=True,
    )

    name: str = Field(
        default="tidal",
        title="TIDAL",
        description="Browse and play music from your TIDAL account.",
        frozen=True,
        exclude=True,
    )
    enabled: bool = Field(
        default=True,
        title="Enable TIDAL",
        description="Sign in using the link under TIDAL account.",
        json_schema_extra={"importance": "simple"},
    )
    audio_quality: TidalAudioQuality = Field(
        default=TidalAudioQuality.MAX,
        title="Maximum audio quality",
        json_schema_extra={
            "widget": "enum_dropdown",
            "importance": "simple",
            "help": (
                "TIDAL may return a lower quality, such as AAC."
            ),
        },
    )
    unpair: bool = Field(
        default=False,
        title="Unlink Tidal",
        json_schema_extra={
            "help": (
                "Forget this account on Apply & Restart, then sign in with another account."
            ),
            "widget": "toggle",
            "one_shot": True,
            "importance": "simple",
        },
    )
