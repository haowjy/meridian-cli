"""Shared projection drift guards."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pydantic import BaseModel


def check_projection_drift(
    spec_cls: type[BaseModel],
    projected: frozenset[str],
    delegated: frozenset[str],
) -> None:
    """Fail import when projection accounting diverges from launch spec fields."""

    expected = set(spec_cls.model_fields)
    accounted = set(projected | delegated)
    if expected != accounted:
        missing = expected - accounted
        stale = accounted - expected
        raise ImportError(
            f"{spec_cls.__name__} projection drift. missing={sorted(missing)} stale={sorted(stale)}"
        )


_check_projection_drift = check_projection_drift

__all__ = ["_check_projection_drift", "check_projection_drift"]
