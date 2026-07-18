"""Stable domain identifier types."""

from enum import StrEnum
from typing import Annotated, NewType

from pydantic import BeforeValidator, StringConstraints


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


def normalize_optional_identity(value: object) -> str | None:
    """Canonicalize an identity read from persisted state."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("persisted identity must be a string or null")
    normalized = value.strip()
    return normalized or None


# Persisted optional identity fields normalize at the type boundary.  Keeping the
# NewType inside Annotated preserves static separation while Pydantic owns the
# whitespace/non-empty contract everywhere these types are used.
PersistedChatId = Annotated[
    ChatId,
    BeforeValidator(normalize_optional_identity),
    StringConstraints(min_length=1),
]
PersistedHarnessSessionId = Annotated[
    HarnessSessionId,
    BeforeValidator(normalize_optional_identity),
    StringConstraints(min_length=1),
]
OptionalPersistedChatId = Annotated[
    ChatId | None,
    BeforeValidator(normalize_optional_identity),
]
OptionalPersistedHarnessSessionId = Annotated[
    HarnessSessionId | None,
    BeforeValidator(normalize_optional_identity),
]

__all__ = [
    "ArtifactKey",
    "ChatId",
    "HarnessId",
    "HarnessSessionId",
    "ModelId",
    "OptionalPersistedChatId",
    "OptionalPersistedHarnessSessionId",
    "PersistedChatId",
    "PersistedHarnessSessionId",
    "SchemaVersion",
    "SpawnId",
    "TransportId",
    "normalize_mars_target_name",
    "normalize_optional_identity",
]
