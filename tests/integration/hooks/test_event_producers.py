"""Integration coverage tying accepted hook names to their production seams."""

from __future__ import annotations

import json
import shlex
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, get_args

from structlog.testing import capture_logs

from meridian.lib.config.models import MeridianConfig
from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.lifecycle import create_lifecycle_service
from meridian.lib.core.spawn_lifecycle import SpawnReservation
from meridian.lib.hooks.dispatch import HookDispatcher
from meridian.lib.hooks.types import (
    EVENTS,
    HookContext,
    HookEventName,
    HookResult,
    Notification,
)
from meridian.lib.idle.service import IdleService
from meridian.lib.launch.process.session import resolve_attached_work_id
from meridian.lib.launch.request import SessionRequest
from meridian.lib.launch.types import PrimarySessionMetadata
from meridian.lib.notify.emit import emit
from meridian.lib.ops.runtime import resolve_roots
from meridian.lib.ops.spawn.execute_session import _session_execution_context
from meridian.lib.ops.work_attachment import activate_work
from meridian.lib.ops.work_lifecycle import (
    WorkClearInput,
    WorkDoneInput,
    WorkRenameInput,
    WorkStartInput,
    WorkSwitchInput,
    work_clear_sync,
    work_done_sync,
    work_rename_sync,
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

    dispatched: list[HookContext] = []

    def record_dispatch(
        _dispatcher: HookDispatcher,
        context: HookContext,
    ) -> list[HookResult]:
        dispatched.append(context)
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
            ),
            launch_lock=launch_lock,
        ).spawn_id
    lifecycle.announce(spawn_id)
    lifecycle.finalize(
        spawn_id,
        "failed",
        1,
        origin="runner",
        error="producer failure",
    )

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
        ).spawn_id
    lifecycle.announce(running_spawn_id)

    work_runtime_root = resolve_roots(str(project_root)).runtime_root
    session_store.start_session(
        work_runtime_root,
        harness="codex",
        harness_session_id="work-session",
        model="gpt-5.6-luna",
        chat_id="c9004",
        # A live session always belongs to an execution; a rename follows those.
        spawn_id=str(running_spawn_id),
    )
    created = work_start_sync(
        WorkStartInput(
            label="producer-coverage",
            chat_id="c9004",
            project_root=str(project_root),
        )
    )
    work_start_sync(
        WorkStartInput(
            label="producer-switch",
            chat_id="c9004",
            project_root=str(project_root),
        )
    )
    work_switch_sync(
        WorkSwitchInput(
            work_id=created.name,
            chat_id="c9004",
            project_root=str(project_root),
        )
    )
    existing = work_start_sync(
        WorkStartInput(
            label=created.name,
            chat_id="c9004",
            project_root=str(project_root),
        )
    )
    assert existing.created is False

    def activation_count() -> int:
        return sum(context.event_name == "work.activated" for context in dispatched)

    activations_before_rename = activation_count()
    renamed = work_rename_sync(
        WorkRenameInput(
            work_id=created.name,
            new_name="producer-renamed",
            chat_id="c9004",
            project_root=str(project_root),
        )
    )
    assert session_store.get_session_active_work_id(work_runtime_root, "c9004") == renamed.new_name
    assert activation_count() == activations_before_rename

    work_done_sync(WorkDoneInput(work_id=renamed.new_name, project_root=str(project_root)))
    archived_dir = Path(dispatched[-1].work_dir or "")
    assert dispatched[-1].event_name == "work.done"
    assert archived_dir.is_dir()
    # Switching to a done item reopens it, so the activation names the active
    # directory the item moved back to rather than the archived one.
    work_switch_sync(
        WorkSwitchInput(
            work_id=renamed.new_name,
            chat_id="c9004",
            project_root=str(project_root),
        )
    )
    assert dispatched[-1].event_name == "work.activated"
    activated_dir = Path(dispatched[-1].work_dir or "")
    assert activated_dir.parent.name == "work"
    assert activated_dir.is_dir()
    assert not archived_dir.exists()
    reopened = work_start_sync(
        WorkStartInput(
            label=renamed.new_name,
            chat_id="c9004",
            project_root=str(project_root),
        )
    )
    assert reopened.created is False

    activations_before_clear = activation_count()
    work_clear_sync(WorkClearInput(chat_id="c9004", project_root=str(project_root)))
    assert session_store.get_session_active_work_id(work_runtime_root, "c9004") is None
    assert activation_count() == activations_before_clear

    session_store.stop_session(work_runtime_root, "c9004")
    work_done_sync(WorkDoneInput(work_id=renamed.new_name, project_root=str(project_root)))

    session_store.start_session(
        work_runtime_root,
        harness="codex",
        harness_session_id="",
        model="gpt-5.6-luna",
        chat_id="c9013",
    )
    primary_work_id = resolve_attached_work_id(
        project_root=project_root,
        runtime_root=work_runtime_root,
        chat_id="c9013",
        explicit_work_id=renamed.new_name,
        resume_chat_id=None,
        get_session_active_work_id_fn=session_store.get_session_active_work_id,
        activate_work_fn=activate_work,
    )
    assert primary_work_id == renamed.new_name
    assert session_store.get_session_active_work_id(work_runtime_root, "c9013") == renamed.new_name

    activations_before_child = activation_count()
    with _session_execution_context(
        launch_slot=LaunchSlot(),
        runtime_root=work_runtime_root,
        metadata=PrimarySessionMetadata(
            harness="codex",
            model="gpt-5.6-luna",
            agent="coder",
            agent_path="",
            skills=(),
            skill_paths=(),
        ),
        request=SessionRequest(),
        run_agent_name="coder",
        inherited_work_id=renamed.new_name,
    ) as child_session:
        assert child_session.work_id == renamed.new_name
        assert (
            session_store.get_session_active_work_id(work_runtime_root, child_session.chat_id)
            == renamed.new_name
        )
    assert activation_count() == activations_before_child

    session_store.stop_session(work_runtime_root, "c9013")
    sessionless = work_start_sync(
        WorkStartInput(
            label=renamed.new_name,
            chat_id="no-such-session",
            project_root=str(project_root),
        )
    )
    assert sessionless.created is False

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
    idle.needs_permission(harness="claude", message="Claude needs your permission")

    emit(
        "notify.manual",
        Notification(
            title="Producer coverage",
            body="Manual notification",
            label={"agent": "coder"},
        ),
        project_root=project_root,
        env={"MERIDIAN_HOME": str(tmp_path / "home")},
    )

    event_counts = Counter(context.event_name for context in dispatched)
    assert event_counts["spawn.started"] == 2
    assert event_counts["work.created"] == 2
    assert event_counts["work.activated"] == 8
    assert (
        {context.event_name for context in dispatched}
        == set(EVENTS)
        == set(get_args(HookEventName))
    )

    work_event_ids = [
        context.event_id for context in dispatched if context.event_name.startswith("work.")
    ]
    assert len(work_event_ids) == len(set(work_event_ids))

    finished = next(context for context in dispatched if context.event_name == "spawn.finished")
    assert finished.spawn_error == "producer failure"

    manual = next(context for context in dispatched if context.event_name == "notify.manual")
    manual_payload = json.loads(manual.to_json())
    assert manual_payload["spawn"]["id"] is None
    assert manual_payload["spawn"]["agent"] == "coder"


def test_spawn_lifecycle_drops_invalid_hook_rows_and_runs_valid_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    runtime_root = tmp_path / "runtime"
    marker = tmp_path / "valid-hook-fired"
    project_root.mkdir()
    monkeypatch.setenv("MERIDIAN_HOME", str(tmp_path / "home"))
    (project_root / "meridian.toml").write_text(
        "[[hooks]]\n"
        'name = "stale"\n'
        'event = "not.an.event"\n'
        'command = "echo stale"\n'
        "\n"
        "[[hooks]]\n"
        'name = "valid"\n'
        'event = "spawn.created"\n'
        f'command = "printf fired > {shlex.quote(str(marker))}"\n',
        encoding="utf-8",
    )

    with capture_logs() as logs:
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
                    prompt="exercise tolerant hook loading",
                    status=SpawnStatus.QUEUED,
                ),
                launch_lock=launch_lock,
            ).spawn_id
        lifecycle.announce(spawn_id)

    warnings = [log for log in logs if log.get("log_level") == "warning"]
    assert marker.read_text(encoding="utf-8") == "fired"
    assert warnings == [
        {
            "error": (
                "hook 'stale': Invalid value for 'project.hooks[1].event': expected one of "
                "[notify.cache_expiring, notify.manual, notify.needs_permission, "
                "notify.your_turn, spawn.created, spawn.finished, spawn.started, "
                "work.activated, work.created, work.done], "
                "got 'not.an.event'."
            ),
            "event": "Dropped invalid hook rows; continuing with valid lifecycle hooks.",
            "log_level": "warning",
        }
    ]
