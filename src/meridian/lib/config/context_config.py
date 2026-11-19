from __future__ import annotations

from enum import StrEnum
from typing import cast

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ContextSourceType(StrEnum):
    """Source type for context paths."""

    LOCAL = "local"
    GIT = "git"


class _ContextEntry(BaseModel):
    """The keys every context table takes, including `autosync`.

    `None` means the default: on for a managed clone, off for a `[git."<url>"].path`
    override. Only a git context with a remote has a clone to sync.
    """

    source: ContextSourceType = ContextSourceType.LOCAL
    remote: str | None = None  # Git remote URL when source = "git"
    autosync: bool | None = None

    @model_validator(mode="after")
    def _autosync_needs_a_clone(self) -> _ContextEntry:
        if self.autosync and (
            self.source != ContextSourceType.GIT or not (self.remote or "").strip()
        ):
            raise ValueError('`autosync = true` needs `source = "git"` and a `remote`')
        return self


class WorkContextConfig(_ContextEntry):
    """Work context configuration."""

    model_config = ConfigDict(frozen=True)

    path: str = "{user_home}/context/{project}/work"
    archive: str = "{user_home}/context/{project}/archive/work"


class KbContextConfig(_ContextEntry):
    """Knowledge base context configuration."""

    model_config = ConfigDict(frozen=True)

    path: str = "{user_home}/context/{project}/kb"


class ArbitraryContextConfig(_ContextEntry):
    """Arbitrary user-defined context configuration."""

    model_config = ConfigDict(frozen=True)

    path: str


class ContextConfig(BaseModel):
    """Full context configuration with built-in and arbitrary contexts."""

    model_config = ConfigDict(frozen=True, extra="allow")

    work: WorkContextConfig = Field(default_factory=WorkContextConfig)
    kb: KbContextConfig = Field(default_factory=KbContextConfig)


def extra_contexts(config: ContextConfig) -> dict[str, ArbitraryContextConfig]:
    """The named contexts beyond `work` and `kb`, validated, in config order."""

    raw = getattr(config, "__pydantic_extra__", None)
    extras = cast("dict[str, object]", raw) if isinstance(raw, dict) else {}
    return {
        name: value
        if isinstance(value, ArbitraryContextConfig)
        else ArbitraryContextConfig.model_validate(value)
        for name, value in extras.items()
    }
