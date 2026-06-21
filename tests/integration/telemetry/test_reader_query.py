"""Integration tests for telemetry reader, query, and status helpers."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from meridian.lib.core.lifecycle import SpawnLifecycleService
from meridian.lib.core.spawn_lifecycle import SpawnReservation
from meridian.lib.launch.types import PrimarySessionMetadata
from meridian.lib.state import spawn_store
from meridian.lib.telemetry.query import query_events
from meridian.lib.telemetry.reader import read_events, tail_events
from meridian.lib.telemetry.status import compute_status


def _event(
    ts: datetime,
    event: str,
    *,
    domain: str = "chat",
    ids: dict[str, str] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "v": 1,
        "ts": ts.isoformat().replace("+00:00", "Z"),
        "domain": domain,
        "event": event,
        "scope": "test",
    }
    if ids is not None:
        payload["ids"] = ids
    return payload


def _write_segment(
    telemetry_dir: Path,
    name: str,
    events: list[dict[str, object]],
    extra: str = "",
) -> Path:
    telemetry_dir.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(event, separators=(",", ":")) for event in events]
    if extra:
        lines.append(extra)
    path = telemetry_dir / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_query_with_no_filters_returns_events_from_segments(tmp_path: Path) -> None:
    telemetry_dir = tmp_path / "telemetry"
    now = datetime.now(UTC)
    events = [_event(now, "chat.ws.connected"), _event(now, "spawn.succeeded", domain="spawn")]
    _write_segment(telemetry_dir, "123-0001.jsonl", events)

    assert list(query_events(telemetry_dir)) == events


def test_query_domain_filter(tmp_path: Path) -> None:
    telemetry_dir = tmp_path / "telemetry"
    now = datetime.now(UTC)
    chat_event = _event(now, "chat.ws.connected", domain="chat")
    spawn_event = _event(now, "spawn.succeeded", domain="spawn")
    _write_segment(telemetry_dir, "123-0001.jsonl", [chat_event, spawn_event])

    assert list(query_events(telemetry_dir, domain="chat")) == [chat_event]


def test_query_since_filter(tmp_path: Path) -> None:
    telemetry_dir = tmp_path / "telemetry"
    recent = _event(datetime.now(UTC), "chat.ws.connected")
    old = _event(datetime.now(UTC) - timedelta(hours=2), "chat.ws.disconnected")
    _write_segment(telemetry_dir, "123-0001.jsonl", [old, recent])

    assert list(query_events(telemetry_dir, since="1h")) == [recent]


def test_query_spawn_correlation_filter(tmp_path: Path) -> None:
    telemetry_dir = tmp_path / "telemetry"
    now = datetime.now(UTC)
    matching = _event(now, "spawn.succeeded", domain="spawn", ids={"spawn_id": "p123"})
    other = _event(now, "spawn.failed", domain="spawn", ids={"spawn_id": "p456"})
    _write_segment(telemetry_dir, "123-0001.jsonl", [matching, other])

    assert list(query_events(telemetry_dir, ids_filter={"spawn_id": "p123"})) == [matching]


def test_status_returns_segment_count_and_size(tmp_path: Path) -> None:
    telemetry_dir = tmp_path / "telemetry"
    event = _event(datetime.now(UTC), "chat.ws.connected")
    path = _write_segment(telemetry_dir, "123-0001.jsonl", [event])

    status = compute_status(tmp_path)

    assert status.telemetry_dir == telemetry_dir
    assert status.segment_count == 1
    assert status.total_bytes == path.stat().st_size
    assert status.total_size_human.endswith("B")


def test_truncated_lines_are_skipped_gracefully(tmp_path: Path) -> None:
    telemetry_dir = tmp_path / "telemetry"
    event = _event(datetime.now(UTC), "chat.ws.connected")
    path = _write_segment(telemetry_dir, "123-0001.jsonl", [event], extra='{"truncated"')

    assert list(read_events(path)) == [event]


def test_query_events_merges_multiple_directories_in_mtime_order(tmp_path: Path) -> None:
    first_dir = tmp_path / "project-a" / "telemetry"
    second_dir = tmp_path / "project-b" / "telemetry"
    older = _write_segment(
        first_dir,
        "cli.100-0001.jsonl",
        [_event(datetime.now(UTC), "chat.ws.connected", ids={"chat_id": "c1"})],
    )
    newer = _write_segment(
        second_dir,
        "cli.200-0001.jsonl",
        [_event(datetime.now(UTC), "spawn.succeeded", domain="spawn", ids={"spawn_id": "p2"})],
    )
    now = time.time()
    os.utime(older, (now - 10, now - 10))
    os.utime(newer, (now, now))

    events = list(query_events([first_dir, second_dir]))

    assert [event["event"] for event in events] == ["chat.ws.connected", "spawn.succeeded"]


def test_tail_events_yields_new_lines_from_multiple_directories(tmp_path: Path) -> None:
    first_dir = tmp_path / "project-a" / "telemetry"
    second_dir = tmp_path / "project-b" / "telemetry"
    first_path = _write_segment(first_dir, "cli.100-0001.jsonl", [])
    second_path = _write_segment(second_dir, "cli.200-0001.jsonl", [])
    iterator = tail_events([first_dir, second_dir], poll_interval=0.01)

    with first_path.open("a", encoding="utf-8") as file:
        file.write(
            json.dumps(
                _event(datetime.now(UTC), "chat.ws.connected"),
                separators=(",", ":"),
            )
            + "\n"
        )
    first_event = next(iterator)

    with second_path.open("a", encoding="utf-8") as file:
        file.write(
            json.dumps(
                _event(datetime.now(UTC), "spawn.succeeded", domain="spawn"),
                separators=(",", ":"),
            )
            + "\n"
        )
    second_event = next(iterator)

    assert [first_event["event"], second_event["event"]] == [
        "chat.ws.connected",
        "spawn.succeeded",
    ]


def test_status_aggregates_active_writers_across_project_directories_and_legacy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_a_root = tmp_path / "project-a"
    project_b_root = tmp_path / "project-b"
    telemetry_a = project_a_root / "telemetry"
    telemetry_b = project_b_root / "telemetry"
    legacy_dir = tmp_path / "legacy"

    _write_segment(
        telemetry_a,
        "cli.111-0001.jsonl",
        [_event(datetime.now(UTC), "chat.ws.connected")],
    )
    _write_segment(
        telemetry_a,
        "p1.222-0001.jsonl",
        [_event(datetime.now(UTC), "spawn.succeeded", domain="spawn")],
    )
    _write_segment(
        telemetry_b,
        "p2.333-0001.jsonl",
        [_event(datetime.now(UTC), "spawn.failed", domain="spawn")],
    )
    _write_segment(
        legacy_dir,
        "444-0001.jsonl",
        [_event(datetime.now(UTC), "chat.ws.disconnected")],
    )

    service_a = SpawnLifecycleService(project_a_root)
    spawn_a = service_a.start(
        SpawnReservation(
            chat_id="chat-a",
            session_metadata=PrimarySessionMetadata(
                harness="test-harness",
                model="test-model",
                agent="coder",
                agent_path="",
                skills=(),
                skill_paths=(),
            ),
            prompt="do the thing",
            status="running",
        )
    )
    spawn_b = str(
        spawn_store.start_spawn(
            project_b_root,
            spawn_id="p2",
            chat_id="chat-b",
            model="test-model",
            agent="coder",
            harness="test-harness",
            prompt="do the thing",
            status="running",
        )
    )
    assert spawn_a == "p1"
    assert spawn_b == "p2"
    heartbeat_a = project_a_root / "spawns" / spawn_a / "heartbeat"
    heartbeat_b = project_b_root / "spawns" / spawn_b / "heartbeat"
    heartbeat_a.parent.mkdir(parents=True, exist_ok=True)
    heartbeat_b.parent.mkdir(parents=True, exist_ok=True)
    heartbeat_a.touch()
    heartbeat_b.touch()
    monkeypatch.setattr("meridian.lib.telemetry.status.is_process_alive", lambda pid: False)

    status = compute_status(
        project_a_root,
        telemetry_dirs=[telemetry_a, telemetry_b],
        legacy_dir=legacy_dir,
    )

    assert status.telemetry_dir == [telemetry_a, telemetry_b]
    assert status.segment_count == 3
    assert status.active_writers == ["p1.222", "p2.333"]
    assert status.legacy_segments == 1
