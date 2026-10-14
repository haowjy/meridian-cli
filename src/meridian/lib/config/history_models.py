"""Typed history retention and archive configuration."""

from typing import Annotated

from pydantic import ConfigDict, Field

from meridian.lib.config.catalog import file_alias
from meridian.lib.config.model_base import ConfigModel
from meridian.lib.config.schema import config_field


class HistoryArchiveConfig(ConfigModel):
    # This section predated the strict TOML normalizers and historically accepted
    # Pydantic's scalar coercions. Keep that file behavior while still forbidding extras.
    model_config = ConfigDict(frozen=True, extra="forbid")
    automatic: Annotated[
        bool,
        config_field(
            "history.archive.automatic",
            value_kind="bool",
            file_aliases=(file_alias(("history", "archive"), "automatic"),),
            env_vars=("MERIDIAN_HISTORY_ARCHIVE_AUTOMATIC",),
        ),
    ] = False
    after_days: Annotated[
        int,
        config_field(
            "history.archive.after_days",
            value_kind="int",
            file_aliases=(file_alias(("history", "archive"), "after_days"),),
            env_vars=("MERIDIAN_HISTORY_ARCHIVE_AFTER_DAYS",),
        ),
    ] = Field(default=30, ge=0)
    interval_hours: Annotated[
        int,
        config_field(
            "history.archive.interval_hours",
            value_kind="int",
            file_aliases=(file_alias(("history", "archive"), "interval_hours"),),
        ),
    ] = Field(default=24, ge=0)
    max_records: Annotated[
        int,
        config_field(
            "history.archive.max_records",
            value_kind="int",
            file_aliases=(file_alias(("history", "archive"), "max_records"),),
        ),
    ] = Field(default=256, ge=1, le=100_000)
    max_uncompressed_bytes: Annotated[
        int,
        config_field(
            "history.archive.max_uncompressed_bytes",
            value_kind="int",
            file_aliases=(file_alias(("history", "archive"), "max_uncompressed_bytes"),),
        ),
    ] = Field(default=1024**3, ge=1, le=1024**4)
    destination: Annotated[
        str | None,
        config_field(
            "history.archive.destination",
            value_kind="str",
            file_aliases=(file_alias(("history", "archive"), "destination"),),
            env_vars=("MERIDIAN_HISTORY_ARCHIVE_DESTINATION",),
        ),
    ] = None


class HistoryConfig(ConfigModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    archive: HistoryArchiveConfig = Field(default_factory=HistoryArchiveConfig)
