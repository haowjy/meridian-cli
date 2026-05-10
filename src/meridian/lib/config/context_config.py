"""Context configuration models."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ContextSourceType(StrEnum):
    """Source type for context paths."""

    LOCAL = "local"
    GIT = "git"


class WorkContextConfig(BaseModel):
    """Work context configuration."""

    model_config = ConfigDict(frozen=True)

    source: ContextSourceType = ContextSourceType.LOCAL
    remote: str | None = None  # Git remote URL when source = "git"
    path: str = "{user_home}/context/{project}/work"
    archive: str = "{user_home}/context/{project}/archive/work"


class KbContextConfig(BaseModel):
    """Knowledge base context configuration."""

    model_config = ConfigDict(frozen=True)

    source: ContextSourceType = ContextSourceType.LOCAL
    remote: str | None = None  # Git remote URL when source = "git"
    path: str = "{user_home}/context/{project}/kb"


class ArbitraryContextConfig(BaseModel):
    """Arbitrary user-defined context configuration."""

    model_config = ConfigDict(frozen=True)

    source: ContextSourceType = ContextSourceType.LOCAL
    remote: str | None = None  # Git remote URL when source = "git"
    path: str


class ContextConfig(BaseModel):
    """Full context configuration with built-in and arbitrary contexts."""

    model_config = ConfigDict(frozen=True, extra="allow")

    work: WorkContextConfig = Field(default_factory=WorkContextConfig)
    kb: KbContextConfig = Field(default_factory=KbContextConfig)
