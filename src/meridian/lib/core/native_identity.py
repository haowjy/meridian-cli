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
