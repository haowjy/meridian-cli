"""Contracts that prevent persisted spawn projection drift."""

from __future__ import annotations

import pytest

from meridian.lib.state.spawn.repository import _enforce_spawn_state_field_accounting


def test_field_accounting_guard_rejects_a_dropped_stored_field() -> None:
    shared = {"id", "status", "claude_config_dir"}

    with pytest.raises(ImportError, match=r"Stored missing=\['claude_config_dir'\]"):
        _enforce_spawn_state_field_accounting(
            shared_fields=shared,
            stored_fields={"v", "prompt_length", "id", "status"},
            record_fields={"prompt", *shared},
        )
