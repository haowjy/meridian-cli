"""Reference grammar and the explicit boundary to exact persisted run keys.

A bare pN is a *spawn reference*, not a request to read run 1. User-facing
callers resolve it through latest_run. ``row_key=True`` is for mechanisms
which already own an exact key (including legacy, nonconforming row names).
It never discovers a latest run and must not be used to resolve user input.
"""

import re
from dataclasses import dataclass
from typing import Literal, overload

from meridian.lib.core.types import ChatId, RunId, SpawnId

_SPAWN = re.compile(r"p([0-9]+)(?:\.([0-9]+))?\Z")
_CHAT = re.compile(r"c[0-9]+\Z")
_LEGACY_SORT = re.compile(r"r([0-9]+)\Z")


@dataclass(frozen=True)
class ParsedRef:
    kind: Literal["spawn", "run", "chat", "native"]
    value: str
    spawn_id: SpawnId | None = None
    run_id: RunId | None = None
    run: int | None = None
    chat_id: ChatId | None = None


@overload
def parse_ref(text: str, *, row_key: Literal[True]) -> RunId: ...


@overload
def parse_ref(text: str, *, row_key: Literal[False] = False) -> ParsedRef: ...


def parse_ref(text: str, *, row_key: bool = False) -> ParsedRef | RunId:
    """Parse case-sensitive ASCII IDs; .1 aliases the bare first-run key.

    User refs trim whitespace. Leading zeros in spawn/chat IDs are preserved;
    run suffixes are numeric. .0, rN and junk are native refs, never discovered
    runs. Exact persisted keys retain their spelling, including legacy names.
    """
    if row_key:
        return RunId(SpawnId(text))
    value = text.strip()
    match = _SPAWN.fullmatch(value)
    if match is not None and (match[2] is None or int(match[2]) >= 1):
        spawn = SpawnId(f"p{match[1]}")
        number = int(match[2]) if match[2] is not None else 1
        key = RunId(spawn if number == 1 else SpawnId(f"{spawn}.{number}"))
        return ParsedRef(
            "spawn" if match[2] is None else "run", value,
            spawn_id=spawn, run_id=key if match[2] is not None else None, run=number,
        )
    if _CHAT.fullmatch(value):
        return ParsedRef("chat", value, chat_id=ChatId(value))
    return ParsedRef("native", value)


def spawn_of(run_key: str) -> SpawnId:
    """Cheap stable prefix, retaining nonconforming historical row identities."""
    value = run_key.strip()
    match = _SPAWN.fullmatch(value)
    if match is not None and (match[2] is None or int(match[2]) >= 1):
        return SpawnId(f"p{match[1]}")
    return SpawnId(run_key)


def reference_sort_key(value: str) -> tuple[int, int, str]:
    """Numeric scan order, preserving old rN/junk ordering for diagnostics."""
    ref = parse_ref(value)
    if ref.spawn_id is not None:
        return int(ref.spawn_id[1:]), ref.run or 1, value
    legacy = _LEGACY_SORT.fullmatch(value)
    return (int(legacy[1]), 1, value) if legacy else (10**9, 1, value)
