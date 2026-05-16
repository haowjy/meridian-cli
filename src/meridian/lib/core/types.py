"""Stable domain identifier types."""

from enum import StrEnum
from typing import NewType


class HarnessId(StrEnum):
    """Known harness identifiers."""

    CLAUDE = "claude"
    CODEX = "codex"
    OPENCODE = "opencode"
    PI = "pi"


class TransportId(StrEnum):
    """Known transport identifiers."""

    SUBPROCESS = "subprocess"
    STREAMING = "streaming"


SpawnId = NewType("SpawnId", str)
ModelId = NewType("ModelId", str)
ArtifactKey = NewType("ArtifactKey", str)
SchemaVersion = NewType("SchemaVersion", int)

__all__ = [
    "ArtifactKey",
    "HarnessId",
    "ModelId",
    "SchemaVersion",
    "SpawnId",
    "TransportId",
]
