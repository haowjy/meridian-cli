"""Every terminal spawn status reaches a plugin ``when.status`` value."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import get_args
from uuid import uuid4

import pytest

from meridian.lib.core.domain import TerminalSpawnStatus
from meridian.lib.core.lifecycle import LifecycleEvent
from meridian.lib.hooks.dispatch import HookDispatcher


@pytest.mark.parametrize(
    ("status", "when_status"),
    [
        ("succeeded", "success"),
        ("failed", "failure"),
        ("cancelled", "cancelled"),
        ("timed_out", "timeout"),
    ],
)
def test_every_terminal_spawn_status_reaches_when_status(
    tmp_path: Path, status: TerminalSpawnStatus, when_status: str
) -> None:
    assert status in get_args(TerminalSpawnStatus.__value__)
    dispatcher = HookDispatcher(
        project_root=tmp_path,
        runtime_root=None,
        registry=None,
        interval_tracker=None,
        external_runner=None,
        builtin_hooks={},
        env={},
    )
    event = LifecycleEvent(
        event_id=uuid4(),
        event_type="spawn.finished",
        timestamp=datetime.now(UTC),
        spawn_id="p1",
        parent_id=None,
        chat_id=None,
        work_id=None,
        agent=None,
        kind="primary",
        model=None,
        harness=None,
        status=status,
    )

    context = dispatcher._build_context(event)  # pyright: ignore[reportPrivateUsage]

    assert context.spawn_status == when_status
