from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import meridian.lib.core.telemetry as core_telemetry
import meridian.lib.telemetry.observer as spawn_observer
import meridian.lib.telemetry.observers as lifecycle_observers
from meridian.lib.core.lifecycle import SpawnLifecycleService
from meridian.lib.core.spawn_lifecycle import SpawnReservation
from meridian.lib.core.telemetry import SpawnEventCounter
from meridian.lib.launch.types import PrimarySessionMetadata
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.telemetry import emit_telemetry
from meridian.lib.telemetry.init import setup_telemetry
from meridian.lib.telemetry.retention import run_retention_cleanup
from meridian.lib.telemetry.router import get_global_router


def read_telemetry_events(runtime_root: Path) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for segment in sorted((runtime_root / "telemetry").glob("*.jsonl")):
        for line in segment.read_text(encoding="utf-8").splitlines():
            events.append(json.loads(line))
    return events


def write_segment(
    telemetry_dir: Path,
    name: str,
    *,
    event: str = "chat.ws.connected",
    domain: str = "chat",
) -> Path:
    telemetry_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "v": 1,
        "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "domain": domain,
        "event": event,
        "scope": "test",
    }
    path = telemetry_dir / name
    path.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
    return path


def start_spawn(service: SpawnLifecycleService, *, status: str = "running") -> str:
    return service.start(
        SpawnReservation(
            chat_id="chat-1",
            session_metadata=PrimarySessionMetadata(
                harness="test-harness",
                model="test-model",
                agent="coder",
                agent_path="",
                skills=(),
                skill_paths=(),
            ),
            prompt="do the thing",
            status=status,  # type: ignore[arg-type]
        )
    )


def setup_spawn_projection(runtime_root: Path, monkeypatch) -> None:
    monkeypatch.setattr(lifecycle_observers, "_GLOBAL_OBSERVERS", [])
    monkeypatch.setattr(lifecycle_observers, "_debug_trace_registered", False)
    monkeypatch.setattr(spawn_observer, "_registered", False)
    monkeypatch.setattr(core_telemetry, "_GLOBAL_EVENT_COUNTER", SpawnEventCounter())
    setup_telemetry(runtime_root=runtime_root)
    spawn_observer.register_spawn_telemetry_observer()


def test_retention_preserves_spawn_owned_segments_for_reconciled_active_spawns(tmp_path) -> None:
    telemetry_dir = tmp_path / "telemetry"
    segment = write_segment(
        telemetry_dir,
        "p1.999-0001.jsonl",
        event="spawn.running",
        domain="spawn",
    )
    old_time = time.time() - 10 * 24 * 60 * 60
    os.utime(segment, (old_time, old_time))

    service = SpawnLifecycleService(tmp_path)
    spawn_id = start_spawn(service)
    assert spawn_id == "p1"
    heartbeat = tmp_path / "spawns" / spawn_id / "heartbeat"
    heartbeat.parent.mkdir(parents=True, exist_ok=True)
    heartbeat.touch()

    run_retention_cleanup(telemetry_dir, runtime_root=tmp_path, max_age_days=7)

    assert segment.exists()


def test_retention_preserves_valid_active_segment_beside_quarantined_row(tmp_path) -> None:
    telemetry_dir = tmp_path / "telemetry"
    valid_segment = write_segment(
        telemetry_dir,
        "p1.999-0001.jsonl",
        event="spawn.running",
        domain="spawn",
    )
    quarantined_segment = write_segment(
        telemetry_dir,
        "p2.999-0001.jsonl",
        event="spawn.running",
        domain="spawn",
    )
    old_time = time.time() - 10 * 24 * 60 * 60
    os.utime(valid_segment, (old_time, old_time))
    os.utime(quarantined_segment, (old_time, old_time))

    service = SpawnLifecycleService(tmp_path)
    valid_id = start_spawn(service)
    quarantined_id = start_spawn(service)
    assert (valid_id, quarantined_id) == ("p1", "p2")
    heartbeat = tmp_path / "spawns" / valid_id / "heartbeat"
    heartbeat.touch()
    state_path = tmp_path / "spawns" / quarantined_id / "state.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["status"] = "zombie"
    atomic_write_text(state_path, json.dumps(payload))

    run_retention_cleanup(telemetry_dir, runtime_root=tmp_path, max_age_days=7)

    assert valid_segment.exists()
    assert quarantined_segment.exists()


def test_retention_deletes_spawn_owned_segments_for_stale_spawns(tmp_path) -> None:
    telemetry_dir = tmp_path / "telemetry"
    segment = write_segment(
        telemetry_dir,
        "p1.999-0001.jsonl",
        event="spawn.running",
        domain="spawn",
    )
    old_time = time.time() - 10 * 24 * 60 * 60
    os.utime(segment, (old_time, old_time))

    service = SpawnLifecycleService(tmp_path)
    spawn_id = start_spawn(service)
    assert spawn_id == "p1"

    run_retention_cleanup(telemetry_dir, runtime_root=tmp_path, max_age_days=7)

    assert not segment.exists()


def test_retention_size_pressure_prefers_legacy_orphan_over_stale_recognized_segment(
    tmp_path,
    monkeypatch,
) -> None:
    telemetry_dir = tmp_path / "telemetry"
    active_spawn = write_segment(
        telemetry_dir,
        "p1.999-0001.jsonl",
        event="spawn.running",
        domain="spawn",
    )
    live_cli = write_segment(telemetry_dir, f"cli.{os.getpid()}-0001.jsonl")
    stale_recognized = write_segment(telemetry_dir, "cli.999999-0001.jsonl")
    orphan = write_segment(telemetry_dir, "123-0001.jsonl")

    service = SpawnLifecycleService(tmp_path)
    spawn_id = start_spawn(service)
    assert spawn_id == "p1"
    heartbeat = tmp_path / "spawns" / spawn_id / "heartbeat"
    heartbeat.parent.mkdir(parents=True, exist_ok=True)
    heartbeat.touch()

    now = time.time()
    os.utime(stale_recognized, (now - 20, now - 20))
    os.utime(orphan, (now - 10, now - 10))
    monkeypatch.setattr(
        "meridian.lib.state.liveness.is_process_alive",
        lambda pid, created_after_epoch=None: pid == os.getpid(),
    )

    live_and_stale_budget = (
        active_spawn.stat().st_size + live_cli.stat().st_size + stale_recognized.stat().st_size
    )
    run_retention_cleanup(
        telemetry_dir,
        runtime_root=tmp_path,
        max_age_days=365,
        max_total_bytes=live_and_stale_budget,
    )

    assert active_spawn.exists()
    assert live_cli.exists()
    assert stale_recognized.exists()
    assert not orphan.exists()


def test_full_pipeline_emit_queue_writer_segment(tmp_path) -> None:
    setup_telemetry(runtime_root=tmp_path)
    emit_telemetry("server", "mcp.command.invoked", scope="mcp.server", ids={"request_id": "r1"})
    get_global_router().close()

    segment = tmp_path / "telemetry" / f"cli.{os.getpid()}-0001.jsonl"
    assert segment.exists()
    event = json.loads(segment.read_text(encoding="utf-8").splitlines()[0])
    assert event["event"] == "mcp.command.invoked"
    assert event["ids"] == {"request_id": "r1"}


def test_spawn_terminal_success_and_failure_project_to_telemetry_segment(
    tmp_path, monkeypatch
) -> None:
    setup_spawn_projection(tmp_path, monkeypatch)
    service = SpawnLifecycleService(tmp_path)
    succeeded_id = start_spawn(service)
    failed_id = start_spawn(service)

    service.finalize(
        succeeded_id,
        "succeeded",
        0,
        origin="runner",
        duration_secs=1.5,
    )
    service.finalize(
        failed_id,
        "failed",
        2,
        origin="runner",
        error="boom",
    )

    get_global_router().close()
    events = read_telemetry_events(tmp_path)
    assert {event["event"] for event in events}.issuperset({"spawn.succeeded", "spawn.failed"})
    succeeded = next(event for event in events if event["event"] == "spawn.succeeded")
    failed = next(event for event in events if event["event"] == "spawn.failed")

    assert succeeded["severity"] == "info"
    assert succeeded["ids"] == {"spawn_id": succeeded_id}
    assert succeeded["data"] == {
        "status": "succeeded",
        "exit_code": 0,
        "duration_secs": 1.5,
    }
    assert "category" not in succeeded["data"]
    assert failed["severity"] == "error"
    assert failed["ids"] == {"spawn_id": failed_id}
    assert failed["data"] == {
        "status": "failed",
        "exit_code": 2,
        "reason": "boom",
        "error": {"type": "SpawnFailed", "message": "boom"},
    }
    assert "category" not in failed["data"]
