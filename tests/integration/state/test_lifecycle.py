"""Small contract tests for crash-only lifecycle behavior."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest

from meridian.lib.core.lifecycle import LifecycleEvent, SpawnLifecycleService
from meridian.lib.core.spawn_lifecycle import SpawnReservation
from meridian.lib.launch.types import PrimarySessionMetadata
from meridian.lib.state import spawn_store
from meridian.lib.state.launch_lock import LaunchSlot


class EventTypeHook:
    """Captures lifecycle event types for assertions."""

    def __init__(self) -> None:
        self.event_types: list[str] = []

    def on_event(self, event: LifecycleEvent) -> None:
        self.event_types.append(event.event_type)


def _make_service(
    runtime_root: Path,
    hooks: list[Any] | None = None,
) -> SpawnLifecycleService:
    return SpawnLifecycleService(runtime_root, hooks=hooks)


def _start_spawn(service: SpawnLifecycleService, **overrides: Any) -> str:
    defaults: dict[str, Any] = {
        "chat_id": "c1",
        "session_metadata": PrimarySessionMetadata(
            harness="codex",
            model="gpt-5.4",
            agent="coder",
            agent_path="",
            skills=(),
            skill_paths=(),
        ),
        "prompt": "run it",
        "status": "running",
    }
    defaults.update(overrides)
    with LaunchSlot() as launch_lock:
        return service.start(SpawnReservation(**defaults), launch_lock=launch_lock).spawn_id


def test_owner_record_launch_facts_clears_stale_runner_created_epoch_when_pid_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hook = EventTypeHook()
    service = _make_service(tmp_path, hooks=[hook])
    spawn_id = _start_spawn(service)
    service.record_launch_facts(
        spawn_id,
        runner_pid=os.getpid(),
        runner_created_at_epoch=222.0,
    )
    initial = spawn_store.get_spawn(tmp_path, spawn_id)
    assert initial is not None
    assert initial.runner_created_at_epoch == 222.0

    monkeypatch.setattr("meridian.lib.core.lifecycle._pid_created_at_epoch", lambda _pid: None)
    hook.event_types.clear()
    service.record_launch_facts(
        spawn_id,
        runner_pid=999997,
    )

    updated = spawn_store.get_spawn(tmp_path, spawn_id)
    assert updated is not None
    assert updated.runner_pid == 999997
    assert updated.runner_created_at_epoch is None
    assert hook.event_types == []


def test_owner_transition_preserves_metadata_written_after_cache_load(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    spawn_id = _start_spawn(service)

    spawn_store.update_spawn(tmp_path, spawn_id, claude_config_dir="/tmp/claude-config")
    service.record_launch_facts(spawn_id, launch_mode="background")

    updated = spawn_store.get_spawn(tmp_path, spawn_id)
    assert updated is not None
    assert updated.status == "running"
    assert updated.launch_mode == "background"
    assert updated.claude_config_dir == "/tmp/claude-config"
