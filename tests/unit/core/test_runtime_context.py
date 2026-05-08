"""Unit tests for the RuntimeContext compatibility wrapper."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.resolved_context import ResolvedContext
from meridian.lib.core.types import SpawnId


def test_runtime_context_from_environment_delegates_to_resolved_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = ResolvedContext(
        spawn_id=SpawnId("p123"),
        depth=4,
        project_root=Path("/repo"),
        runtime_root=Path("/runtime/state"),
        chat_id="chat-7",
        work_id="work-7",
        work_dir=Path("/repo/.meridian/work/work-7"),
        kb_dir=Path("/repo/.meridian/kb"),
    )

    def fake_from_environment(cls, **kwargs: object) -> ResolvedContext:
        _ = cls, kwargs
        return expected

    monkeypatch.setattr(ResolvedContext, "from_environment", classmethod(fake_from_environment))

    resolved = RuntimeContext.from_environment()

    assert resolved == RuntimeContext(
        spawn_id=SpawnId("p123"),
        depth=4,
        project_root=Path("/repo"),
        runtime_root=Path("/runtime/state"),
        chat_id="chat-7",
        work_id="work-7",
    )


def test_runtime_context_to_env_overrides_uses_repo_scoped_work_dir() -> None:
    ctx = RuntimeContext(
        spawn_id=SpawnId("p456"),
        depth=2,
        project_root=Path("/repo"),
        runtime_root=Path("/runtime/state"),
        chat_id="chat-9",
        work_id="work-9",
    )

    assert ctx.to_env_overrides() == {
        "MERIDIAN_DEPTH": "2",
        "MERIDIAN_SPAWN_ID": "p456",
        "MERIDIAN_PROJECT_DIR": "/repo",
        "MERIDIAN_RUNTIME_DIR": "/runtime/state",
        "MERIDIAN_CHAT_ID": "chat-9",
        "MERIDIAN_ACTIVE_WORK_ID": "work-9",
        "MERIDIAN_ACTIVE_WORK_DIR": "/repo/.meridian/work/work-9",
    }


def test_runtime_context_to_env_overrides_falls_back_to_state_root_for_work_dir() -> None:
    ctx = RuntimeContext(
        depth=1,
        runtime_root=Path("/runtime/state"),
        work_id="work-state",
    )

    assert ctx.to_env_overrides() == {
        "MERIDIAN_DEPTH": "1",
        "MERIDIAN_RUNTIME_DIR": "/runtime/state",
        "MERIDIAN_ACTIVE_WORK_ID": "work-state",
        "MERIDIAN_ACTIVE_WORK_DIR": "/runtime/state/work/work-state",
    }


def test_runtime_context_to_env_overrides_omits_absent_values() -> None:
    ctx = RuntimeContext(depth=0)

    assert ctx.to_env_overrides() == {"MERIDIAN_DEPTH": "0"}


def test_runtime_context_is_nested_uses_zero_based_depth() -> None:
    assert not RuntimeContext(depth=0).is_nested
    assert RuntimeContext(depth=1).is_nested
