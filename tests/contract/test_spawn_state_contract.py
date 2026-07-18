"""Contracts that prevent persisted spawn projection drift."""

from __future__ import annotations

from enum import StrEnum
from inspect import signature

import pytest

from meridian.lib.core.domain import (
    SpawnLifecycleClass,
    _derive_spawn_status_sets,
)
from meridian.lib.core.spawn_service import SpawnApplicationService
from meridian.lib.state.spawn.repository import _enforce_spawn_state_field_accounting
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


def test_field_accounting_guard_rejects_a_dropped_stored_field() -> None:
    shared = {"id", "status", "claude_config_dir"}

    with pytest.raises(ImportError, match=r"Stored missing=\['claude_config_dir'\]"):
        _enforce_spawn_state_field_accounting(
            shared_fields=shared,
            stored_fields={"v", "prompt_length", "id", "status"},
            record_fields={"prompt", *shared},
        )


def test_generic_metadata_apis_do_not_accept_terminal_error_facts() -> None:
    assert "error" not in signature(update_spawn).parameters
    assert "error" not in signature(SpawnApplicationService.update_metadata).parameters
