"""A native primary's activity: the runtime's turn edges and the runner's writes share one file."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.state import spawn_store
from meridian.lib.state.paths import LaunchPaths
from meridian.lib.state.primary_meta import (
    ActivityState,
    PrimaryMetadata,
    read_primary_metadata,
    record_turn_activity,
    write_native_primary_metadata,
)
from tests.integration.state.conftest import _create_spawn


def _launch(tmp_path: Path) -> LaunchPaths:
    runtime_root, spawn_id = _create_spawn(tmp_path, kind="primary", harness="pi")
    row = spawn_store.get_spawn(runtime_root, spawn_id)
    assert row is not None
    return LaunchPaths.for_row(runtime_root, row)


def _runner_write(launch: LaunchPaths, activity: ActivityState | None) -> None:
    write_native_primary_metadata(
        launch, PrimaryMetadata(managed_backend=False, tui_pid=42, activity=activity)
    )


def _activity(launch: LaunchPaths) -> str | None:
    metadata = read_primary_metadata(launch)
    assert metadata is not None
    return metadata.activity


def test_the_runners_tui_start_write_keeps_a_reported_turn(tmp_path: Path) -> None:
    launch = _launch(tmp_path)
    _runner_write(launch, "starting")
    assert record_turn_activity(launch, "turn_active")
    _runner_write(launch, None)
    assert _activity(launch) == "turn_active"


def test_the_tui_start_ends_starting_when_nothing_was_reported(tmp_path: Path) -> None:
    launch = _launch(tmp_path)
    _runner_write(launch, "starting")
    _runner_write(launch, None)
    assert _activity(launch) is None


def test_finalizing_wins_over_a_late_report(tmp_path: Path) -> None:
    launch = _launch(tmp_path)
    assert record_turn_activity(launch, "turn_active")
    _runner_write(launch, "finalizing")
    assert not record_turn_activity(launch, "idle")
    assert _activity(launch) == "finalizing"
