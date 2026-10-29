"""Contracts that prevent persisted spawn projection drift."""

from __future__ import annotations

from enum import StrEnum

from meridian.lib.core.domain import (
    SpawnLifecycleClass,
    _derive_spawn_status_sets,
)


def test_status_sets_classify_a_synthetic_appended_active_member() -> None:
    class SyntheticStatus(StrEnum):
        EXISTING = "existing"
        APPENDED_ACTIVE = "appended_active"

    all_statuses, active, terminal = _derive_spawn_status_sets(
        {
            SyntheticStatus.EXISTING: SpawnLifecycleClass.TERMINAL,
            SyntheticStatus.APPENDED_ACTIVE: SpawnLifecycleClass.ACTIVE,
        }
    )

    assert all_statuses == frozenset(SyntheticStatus)
    assert active == {SyntheticStatus.APPENDED_ACTIVE}
    assert terminal == {SyntheticStatus.EXISTING}
