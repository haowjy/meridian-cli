"""Parse a user-typed reference by shape alone: pure, no I/O.

Every command that takes a reference classifies it here and nowhere else. Whether
the named thing exists is a question for ``ops/locate.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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


class RefNotFound(ValueError):
    """A spawn, chat or selector reference names nothing.

    A ``ValueError`` because callers already catch that for bad references.
    """

    def __init__(self, ref: str, kind: RefKind) -> None:
        self.ref = ref
        self.kind: RefKind = kind
        super().__init__(f"{_KIND_LABEL[kind]} '{ref}' not found")


_KIND_LABEL: dict[RefKind, str] = {
    "spawn": "Spawn",
    "chat": "Chat",
    "selector": "Reference",
    "native": "Session",
}


@dataclass(frozen=True)
class Ref:
    kind: RefKind
    value: str


def is_spawn_id(value: str) -> bool:
    """True for Meridian's spawn ID shape: ``p`` and ASCII digits."""
    return _SPAWN_ID_RE.fullmatch(value) is not None


def is_chat_id(value: str) -> bool:
    """True for Meridian's chat ID shape: ``c`` and ASCII digits.

    ``reserve_chat_id`` is the only producer, so the shape is the whole test.
    """
    return _CHAT_ID_RE.fullmatch(value) is not None


def parse_ref(raw: str) -> Ref:
    """Classify a reference: spawn, chat, selector, or (anything else) a native session ID."""
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
    "RefKind",
    "RefNotFound",
    "is_chat_id",
    "is_spawn_id",
    "parse_ref",
]
