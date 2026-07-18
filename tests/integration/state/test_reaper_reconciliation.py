# pyright: reportUnknownArgumentType=false, reportUnknownLambdaType=false, reportUnknownParameterType=false, reportMissingParameterType=false
# qa-validated: test-suite-redesign

"""Reconciliation status-transition tests for reconcile_active_spawn.

Covers: terminal short-circuit, runner-pid guards, background-mode
boundary events, dead-runner + activity artifact heuristics, and the
_MERIDIAN_DEPTH gate.  Managed-primary paths live in
test_reaper_managed_primary.py; cancel flows in test_reaper_cancel.py.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from meridian.lib.launch.constants import (
    FINALIZE_EVIDENCE_FILENAME,
    LAST_OBSERVED_EVENT_FILENAME,
)
from meridian.lib.state import spawn_store
from meridian.lib.state.launch_boundary import (
    EVENT_PARENT_LAUNCH_SPAWNED,
    EVENT_WORKER_TAKEOVER_STARTED,
)
from tests.integration.state.conftest import (
    _OLD_STARTED_AT,
    _create_spawn,
    _get_spawn,
    _recent_started_at,
    _reconcile,
    _record_launch_boundary,
    _write_activity_artifact,
    _write_report,
    fake_reaper_liveness,
)


def test_reconcile_active_spawn_returns_terminal_record_unchanged(
    tmp_path: Path,
) -> None:
    runtime_root, spawn_id = _create_spawn(tmp_path, status="succeeded")
    record = _get_spawn(runtime_root, spawn_id)

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled == record
    assert _get_spawn(runtime_root, spawn_id).status == "succeeded"


def test_reconcile_active_spawn_without_runner_pid_stays_unchanged_during_startup_grace(
    tmp_path: Path,
) -> None:
    runtime_root, spawn_id = _create_spawn(
        tmp_path,
        runner_pid=None,
        started_at=_recent_started_at(),
    )
    record = _get_spawn(runtime_root, spawn_id)

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled == record
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "running"
    assert latest.error is None


def test_reconcile_active_spawn_without_runner_pid_fails_after_startup_grace(
    tmp_path: Path,
) -> None:
    runtime_root, spawn_id = _create_spawn(tmp_path, runner_pid=None, started_at=_OLD_STARTED_AT)
    record = _get_spawn(runtime_root, spawn_id)

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == "failed"
    assert reconciled.exit_code == 1
    assert reconciled.error == "missing_runner_pid"
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "failed"
    assert latest.error == "missing_runner_pid"


def test_reconcile_active_spawn_background_without_takeover_evidence_gets_boundary_error(
    tmp_path: Path,
) -> None:
    runtime_root, spawn_id = _create_spawn(
        tmp_path,
        launch_mode="background",
        runner_pid=None,
        started_at=_OLD_STARTED_AT,
    )
    _record_launch_boundary(
        runtime_root,
        spawn_id,
        event=EVENT_PARENT_LAUNCH_SPAWNED,
        launcher_pid=8111,
    )
    record = _get_spawn(runtime_root, spawn_id)

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == "failed"
    assert reconciled.error == "launch_boundary_no_takeover"
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "failed"
    assert latest.error == "launch_boundary_no_takeover"


def test_reconcile_active_spawn_background_takeover_evidence_keeps_runner_alive_skip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher_pid = 8124
    runtime_root, spawn_id = _create_spawn(
        tmp_path,
        launch_mode="background",
        runner_pid=launcher_pid,
        started_at=_OLD_STARTED_AT,
    )
    _record_launch_boundary(
        runtime_root,
        spawn_id,
        event=EVENT_PARENT_LAUNCH_SPAWNED,
        launcher_pid=launcher_pid,
    )
    _record_launch_boundary(
        runtime_root,
        spawn_id,
        event=EVENT_WORKER_TAKEOVER_STARTED,
        worker_pid=9001,
    )
    record = _get_spawn(runtime_root, spawn_id)
    fake_reaper_liveness(monkeypatch, lambda _pid: True)

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled == record
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "running"
    assert latest.error is None


def test_reconcile_active_spawn_returns_unchanged_when_runner_is_alive(
    tmp_path: Path, monkeypatch
) -> None:
    runtime_root, spawn_id = _create_spawn(tmp_path)
    record = _get_spawn(runtime_root, spawn_id)
    fake_reaper_liveness(monkeypatch, lambda _pid: True)

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled == record
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "running"
    assert latest.error is None


def test_reconcile_active_spawn_finalizing_stale_heartbeat_marks_orphan_finalization(
    tmp_path: Path,
) -> None:
    runtime_root, spawn_id = _create_spawn(
        tmp_path,
        status="finalizing",
        started_at=_OLD_STARTED_AT,
    )
    _write_activity_artifact(
        runtime_root,
        spawn_id,
        "heartbeat",
        age_secs=300,
    )
    record = _get_spawn(runtime_root, spawn_id)
    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == "failed"
    assert reconciled.exit_code == 1
    assert reconciled.error == "orphan_finalization"
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "failed"
    assert latest.error == "orphan_finalization"


def test_reconcile_active_spawn_finalizing_recent_activity_skips(
    tmp_path: Path,
) -> None:
    runtime_root, spawn_id = _create_spawn(
        tmp_path,
        status="finalizing",
        started_at=_OLD_STARTED_AT,
    )
    _write_activity_artifact(
        runtime_root,
        spawn_id,
        "heartbeat",
        age_secs=5,
    )
    record = _get_spawn(runtime_root, spawn_id)
    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled == record
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "finalizing"
    assert latest.error is None


def test_reconcile_active_spawn_with_dead_runner_and_no_exit_or_report_fails(
    tmp_path: Path, monkeypatch
) -> None:
    runtime_root, spawn_id = _create_spawn(tmp_path, started_at=_OLD_STARTED_AT)
    record = _get_spawn(runtime_root, spawn_id)
    fake_reaper_liveness(monkeypatch, set())

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == "failed"
    assert reconciled.exit_code == 1
    assert reconciled.error == "orphan_run"
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "failed"
    assert latest.error == "orphan_run"


def test_reaped_orphan_records_runner_child_and_last_event_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root, spawn_id = _create_spawn(
        tmp_path,
        runner_pid=123,
        worker_pid=456,
        started_at=_OLD_STARTED_AT,
    )
    spawn_dir = runtime_root / "spawns" / spawn_id
    heartbeat_path = _write_activity_artifact(
        runtime_root,
        spawn_id,
        "heartbeat",
        age_secs=300,
    )
    marker = {
        "event_kind": "item/started",
        "timestamp": "2026-04-16T16:46:39Z",
        "seq": 93,
        "turn_started": 1,
        "turn_completed": 0,
        "item_started": 94,
        "item_completed": 93,
    }
    (spawn_dir / LAST_OBSERVED_EVENT_FILENAME).write_text(
        json.dumps(marker),
        encoding="utf-8",
    )
    fixed_now = heartbeat_path.stat().st_mtime + 300
    monkeypatch.setattr("meridian.lib.state.reaper.time.time", lambda: fixed_now)
    fake_reaper_liveness(monkeypatch, {456})

    reconciled = _reconcile(tmp_path, runtime_root, _get_spawn(runtime_root, spawn_id))

    assert reconciled.error == "orphan_run"
    evidence = json.loads(
        (spawn_dir / FINALIZE_EVIDENCE_FILENAME).read_text(encoding="utf-8")
    )
    assert evidence["reason"] == "orphan_run"
    assert evidence["runner"] == {"pid": 123, "alive": False}
    assert evidence["worker"] == {"pid": 456, "alive": True}
    assert evidence["worker_or_backend_alive"] is True
    assert evidence["heartbeat_age_secs"] == pytest.approx(300)
    assert evidence["last_observed_event"] == marker


def test_reconcile_active_spawn_with_cancel_intent_and_dead_runner_cancels(
    tmp_path: Path, monkeypatch
) -> None:
    runtime_root, spawn_id = _create_spawn(tmp_path, started_at=_OLD_STARTED_AT)
    spawn_store.record_cancel_intent(
        runtime_root,
        spawn_id,
        exit_code=130,
        error="cancelled",
        requested_at="2026-06-03T01:00:00Z",
    )
    record = _get_spawn(runtime_root, spawn_id)
    fake_reaper_liveness(monkeypatch, set())

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == "cancelled"
    assert reconciled.exit_code == 130
    assert reconciled.error == "cancelled"


def test_reconcile_active_spawn_with_cancel_intent_keeps_durable_completion(
    tmp_path: Path, monkeypatch
) -> None:
    runtime_root, spawn_id = _create_spawn(tmp_path, started_at=_OLD_STARTED_AT)
    spawn_store.record_cancel_intent(
        runtime_root,
        spawn_id,
        exit_code=130,
        error="cancelled",
        requested_at="2026-06-03T01:00:00Z",
    )
    _write_report(runtime_root, spawn_id)
    record = _get_spawn(runtime_root, spawn_id)
    fake_reaper_liveness(monkeypatch, set())

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == "succeeded"
    assert reconciled.exit_code == 0
    assert reconciled.error is None


@pytest.mark.parametrize(
    ("depth_value", "expected_status", "expected_error"),
    [
        ("1", "running", None),
        ("0", "failed", "missing_runner_pid"),
    ],
)
def test_reconcile_active_spawn_depth_gate_respects_env_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    depth_value: str,
    expected_status: str,
    expected_error: str | None,
) -> None:
    runtime_root, spawn_id = _create_spawn(
        tmp_path,
        runner_pid=None,
        started_at=_OLD_STARTED_AT,
    )
    record = _get_spawn(runtime_root, spawn_id)
    monkeypatch.setenv("_MERIDIAN_DEPTH", depth_value)

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == expected_status
    assert reconciled.error == expected_error
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == expected_status
    assert latest.error == expected_error
    if expected_status == "failed":
        assert reconciled.exit_code == 1
        assert latest.exit_code == 1


def test_reconcile_active_spawn_dead_runner_recent_activity_still_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root, spawn_id = _create_spawn(tmp_path, started_at=_OLD_STARTED_AT)
    record = _get_spawn(runtime_root, spawn_id)
    _write_activity_artifact(
        runtime_root,
        spawn_id,
        "heartbeat",
        age_secs=5,
    )

    fixed_now = time.time()
    monkeypatch.setattr("meridian.lib.state.reaper.time.time", lambda: fixed_now)
    fake_reaper_liveness(monkeypatch, set())

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == "failed"
    assert reconciled.exit_code == 1
    assert reconciled.error == "orphan_run"
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "failed"
    assert latest.error == "orphan_run"


def test_reconcile_active_spawn_last_attempt_exit_drives_orphan_failure_after_activity_stales(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root, spawn_id = _create_spawn(tmp_path, started_at=_OLD_STARTED_AT)

    fixed_now = time.time()
    monkeypatch.setattr("meridian.lib.state.reaper.time.time", lambda: fixed_now)
    monkeypatch.setattr("tests.integration.state.conftest.time.time", lambda: fixed_now)
    spawn_store.record_spawn_exited(
        runtime_root,
        spawn_id,
        exit_code=3,
        exited_at="1970-01-01T00:16:30Z",
    )
    _write_activity_artifact(runtime_root, spawn_id, "history.jsonl", age_secs=300)
    record = _get_spawn(runtime_root, spawn_id)
    fake_reaper_liveness(monkeypatch, set())

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == "failed"
    assert reconciled.exit_code == 3
    assert reconciled.error == "orphan_run"
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "failed"
    assert latest.exit_code == 3
    assert latest.error == "orphan_run"


def test_reconcile_active_spawn_post_exit_with_live_runner_skips(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recorded attempt exit must not finalize a spawn whose runner is alive.

    The runner records last_attempt_exit_code/last_attempt_exited_at after every attempt drains,
    including between retries and before post-attempt guardrails run. While the
    runner process is still alive it owns finalization, so the reaper must skip
    rather than orphan it.
    """
    runtime_root, spawn_id = _create_spawn(tmp_path, started_at=_OLD_STARTED_AT)

    fixed_now = time.time()
    monkeypatch.setattr("meridian.lib.state.reaper.time.time", lambda: fixed_now)
    spawn_store.record_spawn_exited(
        runtime_root,
        spawn_id,
        exit_code=1,
        exited_at="1970-01-01T00:00:30Z",
    )
    record = _get_spawn(runtime_root, spawn_id)
    fake_reaper_liveness(monkeypatch, lambda _pid: True)

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled == record
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "running"
    assert latest.error is None


@pytest.mark.parametrize(
    ("runner_status", "runner_exit_code", "runner_error", "expected_status", "expected_error"),
    [
        ("succeeded", 0, None, "succeeded", None),
        ("failed", 23, "guardrail_failed", "failed", "guardrail_failed"),
        ("timed_out", 1, "resident_deadline_expired", "timed_out", "resident_deadline_expired"),
    ],
)
def test_reconcile_active_spawn_finalizes_from_runner_exit_tuple_after_grace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner_status: str,
    runner_exit_code: int,
    runner_error: str | None,
    expected_status: str,
    expected_error: str | None,
) -> None:
    runtime_root, spawn_id = _create_spawn(tmp_path, started_at=_OLD_STARTED_AT)
    spawn_store.record_runner_exit(
        runtime_root,
        spawn_id,
        status=runner_status,
        exit_code=runner_exit_code,
        error=runner_error,
        exited_at="1970-01-01T00:16:30Z",
    )
    record = _get_spawn(runtime_root, spawn_id)
    monkeypatch.setattr("meridian.lib.state.reaper.time.time", lambda: 1_000.0)
    fake_reaper_liveness(monkeypatch, set())

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == expected_status
    assert reconciled.exit_code == runner_exit_code
    assert reconciled.error == expected_error
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == expected_status
    assert latest.exit_code == runner_exit_code
    assert latest.error == expected_error


def test_reconcile_active_spawn_durable_report_wins_over_cancelled_runner_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root, spawn_id = _create_spawn(tmp_path, started_at=_OLD_STARTED_AT)
    spawn_store.record_cancel_intent(
        runtime_root,
        spawn_id,
        exit_code=130,
        error="cancelled",
        requested_at="2026-06-03T01:00:00Z",
    )
    spawn_store.record_runner_exit(
        runtime_root,
        spawn_id,
        status="cancelled",
        exit_code=130,
        error="cancelled",
        exited_at="1970-01-01T00:16:30Z",
    )
    _write_report(runtime_root, spawn_id)
    record = _get_spawn(runtime_root, spawn_id)
    monkeypatch.setattr("meridian.lib.state.reaper.time.time", lambda: 1_000.0)
    fake_reaper_liveness(monkeypatch, set())

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == "succeeded"
    assert reconciled.exit_code == 0
    assert reconciled.error is None
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "succeeded"
    assert latest.exit_code == 0
    assert latest.error is None


class _MidPrepKillSimulation(Exception):
    """Simulates abrupt launcher death during prep (no cleanup)."""


def test_reserve_then_prepare_mid_prep_kill_leaves_queued_row_reconciled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meridian.lib.ops.spawn.execute as execute_module
    from meridian.lib.config.settings import load_config
    from meridian.lib.core.context import RuntimeContext
    from meridian.lib.core.sink import OutputSink
    from meridian.lib.launch.request import SpawnRequest
    from meridian.lib.ops.runtime import (
        build_runtime_from_root_and_config,
        resolve_runtime_authority_for_write,
    )
    from meridian.lib.ops.spawn.models import SpawnCreateInput
    from meridian.lib.state import work_store
    from meridian.lib.state.paths import resolve_project_paths
    from meridian.lib.state.spawn.model import BACKGROUND_LAUNCH_MODE
    from meridian.lib.state.spawn.repository import scan_spawn_ids, write_state_locked

    class _RecordingSink:
        def __init__(self) -> None:
            self.events: list[dict[str, object]] = []

        def result(self, payload: object) -> None:
            _ = payload

        def status(self, message: str) -> None:
            _ = message

        def warning(self, message: str) -> None:
            _ = message

        def error(self, message: str, exit_code: int = 1) -> None:
            _ = (message, exit_code)

        def heartbeat(self, message: str) -> None:
            _ = message

        def event(self, payload: dict[str, object]) -> None:
            self.events.append(payload)

    project_root = tmp_path / "repo"
    project_root.mkdir()
    monkeypatch.setenv("MERIDIAN_HOME", (tmp_path / "home").as_posix())
    authority = resolve_runtime_authority_for_write(project_root)
    assert authority.runtime_root is not None
    config = load_config(project_root, authority=authority)
    sink: OutputSink = _RecordingSink()
    runtime = build_runtime_from_root_and_config(
        project_root,
        config,
        authority=authority,
        sink=sink,
    )
    runtime_root = authority.runtime_root
    project_state_dir = resolve_project_paths(project_root).root_dir
    assert work_store.get_work_item(project_state_dir, "new-work-item") is None

    def _prep_kill(**kwargs: object) -> object:
        spawn_ids = scan_spawn_ids(runtime_root / "spawns")
        assert len(spawn_ids) == 1
        record = spawn_store.get_spawn(runtime_root, spawn_ids[0])
        assert record is not None
        assert record.status == "queued"
        assert record.runner_pid is None
        raise _MidPrepKillSimulation()

    monkeypatch.setattr(execute_module, "_prepare_spawn_execution", _prep_kill)

    with pytest.raises(_MidPrepKillSimulation):
        execute_module._reserve_then_prepare(
            payload=SpawnCreateInput(prompt="run", work="new-work-item"),
            request=SpawnRequest(prompt="run", model="gpt-5.4", harness="codex"),
            runtime=runtime,
            ctx=RuntimeContext(depth=1, spawn_id="p-parent"),
            launch_mode=BACKGROUND_LAUNCH_MODE,
        )

    spawn_ids = scan_spawn_ids(runtime_root / "spawns")
    assert len(spawn_ids) == 1
    spawn_id = spawn_ids[0]
    assert work_store.get_work_item(project_state_dir, "new-work-item") is None
    assert [event.get("t") for event in sink.events] == []
    write_state_locked(
        runtime_root / "spawns",
        spawn_id,
        lambda current: current.model_copy(update={"started_at": _OLD_STARTED_AT}),
    )
    record = _get_spawn(runtime_root, spawn_id)

    reconciled = _reconcile(project_root, runtime_root, record)

    assert reconciled.status == "failed"
    assert reconciled.exit_code == 1
    assert reconciled.error == "missing_runner_pid"
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "failed"
    assert latest.error == "missing_runner_pid"
