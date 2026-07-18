"""Stable domain identifier types."""

from enum import StrEnum
from typing import NewType


class HarnessId(StrEnum):
    """Known harness identifiers."""

    CLAUDE = "claude"
    CODEX = "codex"
    CURSOR = "cursor"
    OPENCODE = "opencode"
    PI = "pi"


class TransportId(StrEnum):
    """Known transport identifiers."""

    SUBPROCESS = "subprocess"
    STREAMING = "streaming"


def normalize_mars_target_name(name: str) -> str:
    """Normalize a mars target or ``managed_root`` name to its bare harness id.

    Mars target names may be written dotted (``".claude"``) or bare (``"claude"``),
    with surrounding whitespace or mixed case. Producers (init scaffolding) and
    consumers (the Claude agent-copy gate) must agree on one canonical form; this is
    it. Returns the lowercased, dot-stripped harness id.
    """
    return name.strip().lower().lstrip(".")


SpawnId = NewType("SpawnId", str)
ModelId = NewType("ModelId", str)
ChatId = NewType("ChatId", str)
HarnessSessionId = NewType("HarnessSessionId", str)
ArtifactKey = NewType("ArtifactKey", str)
SchemaVersion = NewType("SchemaVersion", int)


def normalize_optional_identity(value: str | None) -> str | None:
    """Canonicalize an identity read from persisted state."""

    if value is None:
        return None
    normalized = value.strip()
    return normalized or None

__all__ = [
    "ArtifactKey",
    "ChatId",
    "HarnessId",
    "HarnessSessionId",
    "ModelId",
    "SchemaVersion",
    "SpawnId",
    "TransportId",
    "normalize_mars_target_name",
    "normalize_optional_identity",
]
