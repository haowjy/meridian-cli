"""Parse a user-typed reference by shape alone: pure, no I/O.

Every command that takes a reference classifies it here and nowhere else. Whether
the named thing exists is a question for ``ops/locate.py``.
"""

from __future__ import annotations

import re
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_SPAWN_ID_RE = re.compile(r"p[0-9]+")
_CHAT_ID_RE = re.compile(r"c[0-9]+")

# Selector -> spawn statuses it chooses among (``None``: any status).
SELECTORS: dict[str, tuple[str, ...] | None] = {
    "@latest": None,
    "@last-failed": ("failed",),
    "@last-completed": ("succeeded",),
}

type RefKind = Literal["spawn", "chat", "selector", "native", "machine"]


_KIND_LABEL: dict[RefKind, str] = {
    "spawn": "Spawn",
    "chat": "Chat",
    "native": "Session",
    "machine": "Session",
}

_MACHINE_NAME_RE = re.compile(r"[a-z][a-z0-9-]{0,62}")
_MACHINE_ID_RE = re.compile(r"[0-9a-f]{12}")
REMOTE_INBOX_ERROR = "the inbox of another machine is not built yet: file the message there"


class RefNotFound(ValueError):
    """A spawn, chat or selector reference names nothing.

    A ``ValueError`` because callers already catch that for bad references. The one error
    for a missing spawn or chat: the CLI prints it with kind ``not_found``.
    """

    def __init__(
        self,
        ref: str,
        kind: RefKind,
        *,
        legacy_hint: str | None = None,
        claimed_root: Path | None = None,
    ) -> None:
        self.ref = ref
        self.kind: RefKind = kind
        # Where the claim says the ID lives, when the row is no longer there (reclaimed or
        # pruned): a reader that can recover rows from history looks in this root.
        self.claimed_root = claimed_root
        text = (
            f"No spawn matches '{ref}'"
            if kind == "selector"
            else f"{_KIND_LABEL[kind]} '{ref}' not found"
        )
        if legacy_hint is not None:
            text += (
                f". '{ref}' was issued before machine-wide IDs; name its project with {legacy_hint}"
            )
        super().__init__(text)


_PROJECT_NAMED: ContextVar[bool] = ContextVar("meridian_project_named", default=False)


@contextmanager
def project_named_scope(named: bool) -> Generator[None]:
    """Mark that the user named the current project (``-C``) for this invocation.

    An ID that is claimed for another project and also held, unclaimed, by the named
    project means the named project: that is how a caller picks one of the two.
    """

    token = _PROJECT_NAMED.set(named)
    try:
        yield
    finally:
        _PROJECT_NAMED.reset(token)


def project_is_named() -> bool:
    return _PROJECT_NAMED.get()


class RefAmbiguous(ValueError):
    """One ID names a session in the current project and another in the project its
    claim points at. A ``ValueError`` for the same reason as ``RefNotFound``."""

    def __init__(self, ref: str, here: str, there: str) -> None:
        self.ref = ref
        super().__init__(
            f"'{ref}' names a session in {here} and one in {there}; "
            "add -C to say which project you mean"
        )


@dataclass(frozen=True)
class Ref:
    """``kind`` ``machine`` is ``<machine>/<id>``: ``machine`` is the name typed and ``value``
    the spawn or chat ID after it."""

    kind: RefKind
    value: str
    machine: str | None = None


def is_spawn_id(value: str) -> bool:
    return _SPAWN_ID_RE.fullmatch(value) is not None


def is_chat_id(value: str) -> bool:
    """True for Meridian's chat ID shape: ``c`` and ASCII digits.

    ``reserve_chat_id`` is the only producer, so the shape is the whole test.
    """
    return _CHAT_ID_RE.fullmatch(value) is not None


def is_machine_name(value: str) -> bool:
    """The rule for a machine's name: ``[a-z][a-z0-9-]{0,62}``, not ``inbox`` (reserved for an
    address), not shaped like a spawn or chat ID (it would read as one)."""

    return (
        _MACHINE_NAME_RE.fullmatch(value) is not None
        and value != "inbox"
        and not is_spawn_id(value)
        and not is_chat_id(value)
    )


def is_machine_id(value: str) -> bool:
    """The shape of a machine's ID (``machine.json``): 12 hex characters."""

    return _MACHINE_ID_RE.fullmatch(value) is not None


def parse_ref(raw: str) -> Ref:
    value = raw.strip()
    if not value:
        raise ValueError("A reference is required.")
    if value.startswith("@"):
        if value not in SELECTORS:
            raise ValueError(
                f"Unknown reference '{value}'. Supported references: "
                + ", ".join(sorted(SELECTORS))
            )
        return Ref("selector", value)
    head, slash, tail = value.partition("/")
    if value == "inbox":
        raise ValueError("`inbox` is an address for `meridian message`, not a session")
    if slash and tail == "inbox":
        raise ValueError(REMOTE_INBOX_ERROR)
    if slash and is_machine_name(head) and (is_spawn_id(tail) or is_chat_id(tail)):
        return Ref("machine", tail, head)
    if is_spawn_id(value):
        return Ref("spawn", value)
    if is_chat_id(value):
        return Ref("chat", value)
    return Ref("native", value)


__all__ = [
    "REMOTE_INBOX_ERROR",
    "SELECTORS",
    "Ref",
    "RefAmbiguous",
    "RefKind",
    "RefNotFound",
    "is_chat_id",
    "is_machine_id",
    "is_machine_name",
    "is_spawn_id",
    "parse_ref",
    "project_is_named",
    "project_named_scope",
]
