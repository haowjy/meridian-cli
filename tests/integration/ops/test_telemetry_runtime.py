from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from meridian.lib.core.lifecycle import SpawnLifecycleService
from meridian.lib.core.spawn_lifecycle import SpawnReservation
from meridian.lib.launch.types import PrimarySessionMetadata
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.launch_lock import LaunchSlot
from meridian.lib.telemetry.retention import run_retention_cleanup


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


def start_spawn(
    service: SpawnLifecycleService,
    *,
    status: str = "running",
    launch_lock: LaunchSlot | None = None,
) -> str:
    if launch_lock is None:
        with LaunchSlot() as owned_lock:
            return start_spawn(service, status=status, launch_lock=owned_lock)
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
        ),
        launch_lock=launch_lock,
    ).spawn_id


def test_retention_preserves_spawn_owned_segments_for_live_spawns(tmp_path) -> None:
    telemetry_dir = tmp_path / "telemetry"
    segment = write_segment(
        telemetry_dir,
        "p1.999-0001.jsonl",
        event="spawn.started",
        domain="spawn",
    )
    old_time = time.time() - 10 * 24 * 60 * 60
    os.utime(segment, (old_time, old_time))

    service = SpawnLifecycleService(tmp_path)
    with LaunchSlot() as launch_lock:
        spawn_id = start_spawn(service, launch_lock=launch_lock)
        assert spawn_id == "p1"

        run_retention_cleanup(telemetry_dir, runtime_root=tmp_path, max_age_days=7)

    assert segment.exists()




def test_retention_preserves_valid_active_segment_beside_quarantined_row(tmp_path) -> None:
    telemetry_dir = tmp_path / "telemetry"
    valid_segment = write_segment(
        telemetry_dir,
        "p1.999-0001.jsonl",
        event="spawn.started",
        domain="spawn",
    )
    quarantined_segment = write_segment(
        telemetry_dir,
        "p2.999-0001.jsonl",
        event="spawn.started",
        domain="spawn",
    )
    old_time = time.time() - 10 * 24 * 60 * 60
    os.utime(valid_segment, (old_time, old_time))
    os.utime(quarantined_segment, (old_time, old_time))

    service = SpawnLifecycleService(tmp_path)
    with LaunchSlot() as launch_lock:
        valid_id = start_spawn(service, launch_lock=launch_lock)
        quarantined_id = start_spawn(service)
        assert (valid_id, quarantined_id) == ("p1", "p2")
        state_path = tmp_path / "spawns" / quarantined_id / "state.json"
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        payload["status"] = "zombie"
        atomic_write_text(state_path, json.dumps(payload))

        run_retention_cleanup(telemetry_dir, runtime_root=tmp_path, max_age_days=7)

    assert valid_segment.exists()
    assert quarantined_segment.exists()


def test_retention_size_pressure_prefers_legacy_orphan_over_stale_recognized_segment(
    tmp_path,
    monkeypatch,
) -> None:
    telemetry_dir = tmp_path / "telemetry"
    active_spawn = write_segment(
        telemetry_dir,
        "p1.999-0001.jsonl",
        event="spawn.started",
        domain="spawn",
    )
    live_cli = write_segment(telemetry_dir, f"cli.{os.getpid()}-0001.jsonl")
    stale_recognized = write_segment(telemetry_dir, "cli.999999-0001.jsonl")
    orphan = write_segment(telemetry_dir, "123-0001.jsonl")

    service = SpawnLifecycleService(tmp_path)
    with LaunchSlot() as launch_lock:
        spawn_id = start_spawn(service, launch_lock=launch_lock)
        assert spawn_id == "p1"

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
