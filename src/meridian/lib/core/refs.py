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

type RefKind = Literal["spawn", "chat", "selector", "native"]


_KIND_LABEL: dict[RefKind, str] = {"spawn": "Spawn", "chat": "Chat", "native": "Session"}


class RefNotFound(ValueError):
    """A spawn, chat or selector reference names nothing.

    A ``ValueError`` because callers already catch that for bad references.
    """

    def __init__(
        self,
        ref: str,
        kind: RefKind,
        *,
        legacy: bool = False,
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
        if legacy:
            text += f". '{ref}' was issued before machine-wide IDs; name its project with -C"
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
    kind: RefKind
    value: str


def is_spawn_id(value: str) -> bool:
    return _SPAWN_ID_RE.fullmatch(value) is not None


def is_chat_id(value: str) -> bool:
    """True for Meridian's chat ID shape: ``c`` and ASCII digits.

    ``reserve_chat_id`` is the only producer, so the shape is the whole test.
    """
    return _CHAT_ID_RE.fullmatch(value) is not None


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
    if is_spawn_id(value):
        return Ref("spawn", value)
    if is_chat_id(value):
        return Ref("chat", value)
    return Ref("native", value)


__all__ = [
    "SELECTORS",
    "Ref",
    "RefAmbiguous",
    "RefKind",
    "RefNotFound",
    "is_chat_id",
    "is_spawn_id",
    "parse_ref",
    "project_is_named",
    "project_named_scope",
]
