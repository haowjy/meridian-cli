# qa-validated: test-suite-redesign
# qa-validated: pi-rpc-quiescence
"""Streaming runner watchdog and finalization behavior."""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path

import pytest

from meridian.lib.core.domain import Spawn, TokenUsage
from meridian.lib.core.types import HarnessId, ModelId, SpawnId, TransportId
from meridian.lib.harness.registry import HarnessRegistry
from meridian.lib.launch import constants as launch_constants
from meridian.lib.launch.extract import enrich_finalize, reset_finalize_attempt_artifacts
from meridian.lib.state import spawn_store
from meridian.lib.state.artifact_store import LocalStore, make_artifact_key
from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from meridian.lib.streaming import spawn_manager as spawn_manager_module
from tests.integration.launch.streaming_runner_support import (
    _build_request,
    _EndMonotonicFailsClock,
    _execute_with_context,
    _FakeControlSocketServer,
    _pi_extension_projection_fixture,
    _ReportThenHangConnection,
    streaming_runner_module,
)
from tests.support.fakes import FakeClock, FakeHeartbeat

_pi_extension_projection_fixture = _pi_extension_projection_fixture

_STORE_ATTEMPT_FILES = (
    launch_constants.HISTORY_FILENAME,
    launch_constants.OUTPUT_FILENAME,
    launch_constants.STDERR_FILENAME,
    launch_constants.TOKENS_FILENAME,
    launch_constants.REPORT_FILENAME,
)
_DISK_ATTEMPT_FILES = (
    launch_constants.HISTORY_FILENAME,
    launch_constants.LAST_OBSERVED_EVENT_FILENAME,
    launch_constants.RUNNER_LIFECYCLE_FILENAME,
    launch_constants.STDERR_FILENAME,
    launch_constants.TOKENS_FILENAME,
    launch_constants.REPORT_FILENAME,
)


class _NoReportExtractor:
    def extract_usage(self, artifacts: object, spawn_id: SpawnId) -> TokenUsage:
        _ = artifacts, spawn_id
        return TokenUsage()

    def extract_session_id(self, artifacts: object, spawn_id: SpawnId) -> str | None:
        _ = artifacts, spawn_id
        return None

    def extract_report(self, artifacts: object, spawn_id: SpawnId) -> str | None:
        _ = artifacts, spawn_id
        return None


def test_retry_preserves_completed_attempt_artifacts(tmp_path: Path) -> None:
    log_dir = tmp_path / "spawns" / "r-truncate"
    log_dir.mkdir(parents=True, exist_ok=True)
    artifacts = LocalStore(root_dir=tmp_path / ".artifacts")
    spawn_id = SpawnId("r-truncate")
    for name in _DISK_ATTEMPT_FILES:
        (log_dir / name).write_text("attempt data\n", encoding="utf-8")
    for name in _STORE_ATTEMPT_FILES:
        artifacts.put(
            make_artifact_key(spawn_id, name),
            b"persisted attempt data\n",
        )
    durable_path = log_dir / "durable.json"
    durable_path.write_text("durable data\n", encoding="utf-8")

    streaming_runner_module._preserve_attempt_artifacts(
        artifacts=artifacts,
        spawn_id=spawn_id,
        log_dir=log_dir,
        completed_attempt=1,
    )

    for name in _DISK_ATTEMPT_FILES:
        assert not (log_dir / name).exists()
        assert (log_dir / "attempt-1" / name).read_text(encoding="utf-8") == "attempt data\n"
    for name in _STORE_ATTEMPT_FILES:
        assert not artifacts.exists(make_artifact_key(spawn_id, name))
        assert artifacts.get(make_artifact_key(spawn_id, f"attempt-1/{name}")) == (
            b"persisted attempt data\n"
        )
    assert durable_path.exists()
    assert not (log_dir / "attempt-1.tmp").exists()


def test_preserve_clears_active_history_before_retry_extraction(tmp_path: Path) -> None:
    log_dir = tmp_path / "spawns" / "r-stale-history"
    log_dir.mkdir(parents=True, exist_ok=True)
    artifacts = LocalStore(root_dir=tmp_path / ".artifacts")
    spawn_id = SpawnId("r-stale-history")
    attempt_one_history = (
        b'{"role":"assistant","content":"attempt 1 durable completion report text"}\n'
    )
    attempt_one_report = b"# Report\n\nattempt 1 durable completion\n"
    history_key = make_artifact_key(spawn_id, launch_constants.HISTORY_FILENAME)
    report_key = make_artifact_key(spawn_id, launch_constants.REPORT_FILENAME)
    artifacts.put(history_key, attempt_one_history)
    artifacts.put(report_key, attempt_one_report)
    (log_dir / launch_constants.HISTORY_FILENAME).write_bytes(attempt_one_history)
    (log_dir / launch_constants.REPORT_FILENAME).write_bytes(attempt_one_report)

    streaming_runner_module._preserve_attempt_artifacts(
        artifacts=artifacts,
        spawn_id=spawn_id,
        log_dir=log_dir,
        completed_attempt=1,
    )

    assert not artifacts.exists(history_key)
    attempt_one_history_key = make_artifact_key(spawn_id, "attempt-1/history.jsonl")
    assert artifacts.get(attempt_one_history_key) == attempt_one_history

    reset_finalize_attempt_artifacts(
        artifacts=artifacts,
        spawn_id=spawn_id,
        log_dir=log_dir,
    )

    extraction = enrich_finalize(
        artifacts=artifacts,
        extractor=_NoReportExtractor(),
        spawn_id=spawn_id,
        log_dir=log_dir,
        failure_reason="adapter startup failed",
    )

    assert extraction.durable_report_completion is False
    assert extraction.report.content == "adapter startup failed"
    assert extraction.report.source == "failure_reason"
    assert not artifacts.exists(history_key)


def test_preserve_recovers_interrupted_rotation(tmp_path: Path) -> None:
    log_dir = tmp_path / "spawns" / "r-interrupted"
    log_dir.mkdir(parents=True, exist_ok=True)
    artifacts = LocalStore(root_dir=tmp_path / ".artifacts")
    spawn_id = SpawnId("r-interrupted")
    staging_dir = log_dir / "attempt-1.tmp"
    staging_dir.mkdir(parents=True)
    (staging_dir / launch_constants.HISTORY_FILENAME).write_text(
        "staged history\n",
        encoding="utf-8",
    )
    (log_dir / launch_constants.STDERR_FILENAME).write_text(
        "late stderr\n",
        encoding="utf-8",
    )
    artifacts.put(
        make_artifact_key(spawn_id, launch_constants.HISTORY_FILENAME),
        b"active history\n",
    )

    streaming_runner_module._preserve_attempt_artifacts(
        artifacts=artifacts,
        spawn_id=spawn_id,
        log_dir=log_dir,
        completed_attempt=1,
    )

    assert not staging_dir.exists()
    assert (log_dir / "attempt-1" / launch_constants.HISTORY_FILENAME).read_text(
        encoding="utf-8",
    ) == "staged history\n"
    assert (log_dir / "attempt-1" / launch_constants.STDERR_FILENAME).read_text(
        encoding="utf-8",
    ) == "late stderr\n"
    history_key = make_artifact_key(spawn_id, launch_constants.HISTORY_FILENAME)
    attempt_one_history_key = make_artifact_key(spawn_id, "attempt-1/history.jsonl")
    assert not artifacts.exists(history_key)
    assert artifacts.get(attempt_one_history_key) == b"active history\n"


def test_preserve_discards_stale_staging_when_attempt_dir_exists(tmp_path: Path) -> None:
    log_dir = tmp_path / "spawns" / "r-stale-staging"
    log_dir.mkdir(parents=True, exist_ok=True)
    artifacts = LocalStore(root_dir=tmp_path / ".artifacts")
    spawn_id = SpawnId("r-stale-staging")
    attempt_dir = log_dir / "attempt-1"
    attempt_dir.mkdir(parents=True)
    (attempt_dir / launch_constants.HISTORY_FILENAME).write_text(
        "committed history\n",
        encoding="utf-8",
    )
    staging_dir = log_dir / "attempt-1.tmp"
    staging_dir.mkdir(parents=True)
    (staging_dir / launch_constants.STDERR_FILENAME).write_text(
        "stale staging\n",
        encoding="utf-8",
    )
    (log_dir / launch_constants.STDERR_FILENAME).write_text(
        "live stderr\n",
        encoding="utf-8",
    )

    streaming_runner_module._preserve_attempt_artifacts(
        artifacts=artifacts,
        spawn_id=spawn_id,
        log_dir=log_dir,
        completed_attempt=1,
    )

    assert not staging_dir.exists()
    assert (attempt_dir / launch_constants.HISTORY_FILENAME).read_text(
        encoding="utf-8",
    ) == "committed history\n"
    assert (attempt_dir / launch_constants.STDERR_FILENAME).read_text(
        encoding="utf-8",
    ) == "live stderr\n"


def test_retry_blocked_after_pi_child_started_detects_disk_child_state(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    state_path = runtime_root / "spawns" / "p2" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"id": "p2", "parent_id": "p1", "status": "running"}),
        encoding="utf-8",
    )

    assert streaming_runner_module._retry_blocked_after_pi_child_started(
        harness_id=HarnessId.PI,
        runtime_root=runtime_root,
        current_spawn_id=SpawnId("p1"),
    )


@pytest.mark.parametrize("entry_name", [".staging", ".p2", "spawn-stage", "p²"])
def test_retry_scan_ignores_non_spawn_row_entries(
    tmp_path: Path,
    entry_name: str,
) -> None:
    runtime_root = tmp_path / "runtime"
    state_path = runtime_root / "spawns" / entry_name / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"id": entry_name, "parent_id": "p1", "status": "running"}),
        encoding="utf-8",
    )

    assert not streaming_runner_module._retry_blocked_after_pi_child_started(
        harness_id=HarnessId.PI,
        runtime_root=runtime_root,
        current_spawn_id=SpawnId("p1"),
    )

@pytest.mark.asyncio
async def test_execute_with_streaming_succeeds_after_report_watchdog_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = resolve_project_runtime_root_for_write(tmp_path)
    artifacts = LocalStore(root_dir=tmp_path / ".artifacts")
    registry = HarnessRegistry.with_defaults()
    fake_clock = FakeClock(start=1_000.0)
    fake_heartbeat = FakeHeartbeat()
    fake_heartbeat.set_clock(fake_clock)

    monkeypatch.setattr(spawn_manager_module, "ControlSocketServer", _FakeControlSocketServer)
    monkeypatch.setattr(
        "meridian.lib.harness.connections.get_connection_class",
        lambda _harness_id, _transport_id=TransportId.STREAMING: _ReportThenHangConnection,
    )
    monkeypatch.setattr(launch_constants, "REPORT_WATCHDOG_POLL_SECONDS", 0.001)
    monkeypatch.setattr(launch_constants, "REPORT_WATCHDOG_GRACE_SECONDS", 0.001)
    importlib.reload(streaming_runner_module)

    run = Spawn(
        spawn_id=SpawnId("r-watchdog"),
        prompt="hello",
        model=ModelId("gpt-5.3-codex"),
        status="queued",
    )
    spawn_store.start_spawn(
        runtime_root,
        chat_id="test-chat-watchdog",
        model=str(run.model),
        agent="",
        harness=HarnessId.CODEX.value,
        kind="streaming",
        prompt=run.prompt,
        spawn_id=run.spawn_id,
        launch_mode="foreground",
        status="queued",
    )

    exit_code = await asyncio.wait_for(
        _execute_with_context(
            run,
            request=_build_request(),
            project_root=tmp_path,
            runtime_root=runtime_root,
            artifacts=artifacts,
            registry=registry,
            clock=fake_clock,
            heartbeat_touch=fake_heartbeat.touch,
            heartbeat_interval_secs=0.001,
        ),
        timeout=15.0,
    )

    assert exit_code == 0
    row = spawn_store.get_spawn(runtime_root, run.spawn_id)
    assert row is not None
    assert row.status == "succeeded"
    assert row.exit_code == 0
    assert row.error is None
    assert fake_heartbeat.touches
    report = (runtime_root / "spawns" / str(run.spawn_id) / "report.md").read_text(encoding="utf-8")
    assert "Watchdog fallback completed." in report



@pytest.mark.asyncio
async def test_execute_with_streaming_finalizes_when_duration_clock_read_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = resolve_project_runtime_root_for_write(tmp_path)
    artifacts = LocalStore(root_dir=tmp_path / ".artifacts")
    registry = HarnessRegistry.with_defaults()
    failing_clock = _EndMonotonicFailsClock(start=2_000.0)
    fake_heartbeat = FakeHeartbeat()
    fake_heartbeat.set_clock(failing_clock)

    monkeypatch.setattr(spawn_manager_module, "ControlSocketServer", _FakeControlSocketServer)
    monkeypatch.setattr(
        "meridian.lib.harness.connections.get_connection_class",
        lambda _harness_id, _transport_id=TransportId.STREAMING: _ReportThenHangConnection,
    )
    monkeypatch.setattr(launch_constants, "REPORT_WATCHDOG_POLL_SECONDS", 0.001)
    monkeypatch.setattr(launch_constants, "REPORT_WATCHDOG_GRACE_SECONDS", 0.001)
    importlib.reload(streaming_runner_module)

    run = Spawn(
        spawn_id=SpawnId("r-duration-guard"),
        prompt="hello",
        model=ModelId("gpt-5.3-codex"),
        status="queued",
    )
    spawn_store.start_spawn(
        runtime_root,
        chat_id="test-chat-duration-guard",
        model=str(run.model),
        agent="",
        harness=HarnessId.CODEX.value,
        kind="streaming",
        prompt=run.prompt,
        spawn_id=run.spawn_id,
        launch_mode="foreground",
        status="queued",
    )

    exit_code = await asyncio.wait_for(
        _execute_with_context(
            run,
            request=_build_request(),
            project_root=tmp_path,
            runtime_root=runtime_root,
            artifacts=artifacts,
            registry=registry,
            clock=failing_clock,
            heartbeat_touch=fake_heartbeat.touch,
            heartbeat_interval_secs=0.001,
        ),
        timeout=15.0,
    )

    assert exit_code == 0
    row = spawn_store.get_spawn(runtime_root, run.spawn_id)
    assert row is not None
    assert row.status == "succeeded"
    assert row.exit_code == 0
    assert row.error is None
    assert row.duration_secs == 0.0
    assert fake_heartbeat.touches
