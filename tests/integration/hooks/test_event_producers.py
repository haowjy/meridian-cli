"""Integration coverage tying accepted hook names to their production seams."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, get_args

from meridian.lib.config.models import MeridianConfig
from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.lifecycle import create_lifecycle_service
from meridian.lib.core.spawn_lifecycle import SpawnReservation
from meridian.lib.hooks.dispatch import HookDispatcher
from meridian.lib.hooks.types import (
    EVENT_CLASS,
    HookContext,
    HookEventName,
    HookResult,
    Notification,
)
from meridian.lib.idle.service import IdleService
from meridian.lib.launch.types import PrimarySessionMetadata
from meridian.lib.notify.emit import emit
from meridian.lib.ops.runtime import resolve_roots
from meridian.lib.ops.work_lifecycle import (
    WorkDoneInput,
    WorkStartInput,
    WorkSwitchInput,
    work_done_sync,
    work_start_sync,
    work_switch_sync,
)
from meridian.lib.state import session_store
from meridian.lib.state.idle_store import IdleStore
from meridian.lib.state.launch_lock import LaunchSlot

if TYPE_CHECKING:
    import pytest


class MillisecondClock:
    def __init__(self) -> None:
        self.now = 0

    def now_ms(self) -> int:
        return self.now


def test_every_accepted_hook_event_reaches_the_dispatcher_from_a_producer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    runtime_root = tmp_path / "runtime"
    project_root.mkdir()
    monkeypatch.setenv("MERIDIAN_HOME", str(tmp_path / "home"))

    dispatched: list[HookEventName] = []

    def record_dispatch(
        _dispatcher: HookDispatcher,
        context: HookContext,
    ) -> list[HookResult]:
        dispatched.append(context.event_name)
        return []

    monkeypatch.setattr(HookDispatcher, "fire", record_dispatch)

    lifecycle = create_lifecycle_service(project_root, runtime_root)
    with LaunchSlot() as launch_lock:
        spawn_id = lifecycle.reserve(
            SpawnReservation(
                chat_id="c1",
                session_metadata=PrimarySessionMetadata(
                    harness="codex",
                    model="gpt-5.6-luna",
                    agent="coder",
                    agent_path="",
                    skills=(),
                    skill_paths=(),
                ),
                prompt="record lifecycle events",
                status=SpawnStatus.QUEUED,
            ),
            launch_lock=launch_lock,
        )
    lifecycle.announce(spawn_id)
    lifecycle.mark_running(spawn_id)
    lifecycle.finalize(spawn_id, "succeeded", 0, origin="runner")

    with LaunchSlot() as launch_lock:
        running_spawn_id = lifecycle.reserve(
            SpawnReservation(
                chat_id="c2",
                session_metadata=PrimarySessionMetadata(
                    harness="codex",
                    model="gpt-5.6-luna",
                    agent="coder",
                    agent_path="",
                    skills=(),
                    skill_paths=(),
                ),
                prompt="record created-running lifecycle events",
                status=SpawnStatus.RUNNING,
            ),
            launch_lock=launch_lock,
        )
    lifecycle.announce(running_spawn_id)

    work_runtime_root = resolve_roots(str(project_root)).runtime_root
    session_store.start_session(
        work_runtime_root,
        harness="codex",
        harness_session_id="work-session",
        model="gpt-5.6-luna",
        chat_id="work-chat",
    )
    created = work_start_sync(
        WorkStartInput(
            label="producer-coverage",
            chat_id="work-chat",
            project_root=str(project_root),
        )
    )
    work_start_sync(
        WorkStartInput(
            label="producer-switch",
            chat_id="work-chat",
            project_root=str(project_root),
        )
    )
    work_switch_sync(
        WorkSwitchInput(
            work_id=created.name,
            chat_id="work-chat",
            project_root=str(project_root),
        )
    )
    existing = work_start_sync(
        WorkStartInput(
            label=created.name,
            chat_id="work-chat",
            project_root=str(project_root),
        )
    )
    assert existing.created is False
    work_done_sync(WorkDoneInput(work_id=created.name, project_root=str(project_root)))
    reopened = work_start_sync(
        WorkStartInput(
            label=created.name,
            chat_id="work-chat",
            project_root=str(project_root),
        )
    )
    assert reopened.created is False
    session_store.stop_session(work_runtime_root, "work-chat")
    work_done_sync(WorkDoneInput(work_id=created.name, project_root=str(project_root)))

    clock = MillisecondClock()
    idle = IdleService(
        store=IdleStore(tmp_path / "idle", now_ms=clock.now_ms),
        config=MeridianConfig(),
        env={
            "MERIDIAN_HOME": str(tmp_path / "home"),
            "MERIDIAN_PROJECT_DIR": str(project_root),
            "MERIDIAN_SESSION_ROLE": "primary",
        },
        now_ms=clock.now_ms,
        project_root=project_root,
    )
    armed = idle.arm(harness="codex", session="idle-session", ttl_seconds=3600)
    assert armed.push_at is not None
    assert armed.warn_at is not None
    clock.now = armed.push_at
    idle.fire("push", harness="codex", session="idle-session", stretch=1, anchor=1)
    clock.now = armed.warn_at
    idle.fire("warn", harness="codex", session="idle-session", stretch=1, anchor=1)

    emit(
        "notify.manual",
        Notification(title="Producer coverage", body="Manual notification"),
        project_root=project_root,
        env={"MERIDIAN_HOME": str(tmp_path / "home")},
    )

    event_counts = Counter(dispatched)
    assert event_counts["spawn.started"] == 2
    assert event_counts["work.created"] == 2
    assert event_counts["work.activated"] == 5
    assert set(dispatched) == set(EVENT_CLASS) == set(get_args(HookEventName))
