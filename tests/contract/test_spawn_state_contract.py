"""Contracts that prevent persisted spawn projection drift."""

from __future__ import annotations

from enum import StrEnum
from inspect import signature

from meridian.lib.core.domain import (
    SpawnLifecycleClass,
    _derive_spawn_status_sets,
)
from meridian.lib.core.spawn_service import SpawnApplicationService
from meridian.lib.state.spawn_store import update_spawn


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


def test_generic_metadata_apis_do_not_accept_terminal_error_facts() -> None:
    assert "error" not in signature(update_spawn).parameters
    assert "error" not in signature(SpawnApplicationService.update_metadata).parameters
