"""Immutable harness-native launch identity."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class NativeIdentityPlan:
    """Exact native identity selected before exec; forks may await an owned signal."""

    harness_session_id: str | None
    native_store: str | None
    locator: str | None
    operation: Literal["create", "resume", "fork"]


@dataclass(frozen=True)
class NativeSessionKey:
    native_store: str
    session_id: str


@dataclass(frozen=True)
class RunBoundary:
    """Owned entry/exit observations, independent of execution success."""
    entry_observed: NativeSessionKey | None = None
    exit: NativeSessionKey | None = None
