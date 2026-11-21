from __future__ import annotations

from enum import StrEnum
from typing import Annotated, NewType

from pydantic import BeforeValidator, StringConstraints

from meridian.lib.harness.unlaunched import NAMES as UNLAUNCHED_NAMES


class HarnessId(StrEnum):
    """Known harness identifiers."""

    AGY = "agy"
    CLAUDE = "claude"
    CODEX = "codex"
    OPENCODE = "opencode"
    PI = "pi"

    @property
    def launched_by_meridian(self) -> bool:
        """False for a harness the person starts themselves (``agy``): it has sessions
        and rows Meridian reads and routes to, but no adapter, shortcut or launch."""

        return self.value not in UNLAUNCHED_NAMES

    @classmethod
    def _missing_(cls, value: object) -> HarnessId | None:
        # Old chats and spawn rows still name cursor; a bare "not a valid HarnessId"
        # would hide why they no longer open.
        if value == "cursor":
            raise ValueError(
                "Cursor support was removed in 0.26.0; its sessions can no longer be "
                "launched, resumed or read."
            )
        return None


def is_unlaunched_harness(name: str | None) -> bool:
    """Whether a stored harness name is one the person starts themselves.

    A name no build knows (a removed harness) is not: its rows keep their old rules.
    """

    try:
        return not HarnessId(name or "").launched_by_meridian
    except ValueError:
        return False


def launched_harnesses() -> tuple[HarnessId, ...]:
    return tuple(harness for harness in HarnessId if harness.launched_by_meridian)


def harness_names_text() -> str:
    """Every launchable harness id as ``a, b, or c``, for help text."""

    names = [harness.value for harness in launched_harnesses()]
    return f"{', '.join(names[:-1])}, or {names[-1]}"


class TransportId(StrEnum):
    """Known transport identifiers."""

    SUBPROCESS = "subprocess"
    STREAMING = "streaming"


def normalize_mars_target_name(name: str) -> str:
    """Normalize a mars target or target name to its bare harness id.

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
SchemaVersion = NewType("SchemaVersion", int)


def normalize_optional_identity(value: object) -> str | None:

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
