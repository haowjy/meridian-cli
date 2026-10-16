"""Integration tests for telemetry reader, query, and status helpers."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

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


@pytest.mark.parametrize("filter_kind", ["domain", "since", "spawn"])
def test_query_filters(tmp_path: Path, filter_kind: str) -> None:
    telemetry_dir = tmp_path / "telemetry"
    if filter_kind == "domain":
        now = datetime.now(UTC)
        expected = _event(now, "chat.ws.connected", domain="chat")
        other = _event(now, "spawn.succeeded", domain="spawn")
        kwargs = {"domain": "chat"}
    elif filter_kind == "since":
        expected = _event(datetime.now(UTC), "chat.ws.connected")
        other = _event(datetime.now(UTC) - timedelta(hours=2), "chat.ws.disconnected")
        kwargs = {"since": "1h"}
    else:
        now = datetime.now(UTC)
        expected = _event(now, "spawn.succeeded", domain="spawn", ids={"spawn_id": "p123"})
        other = _event(now, "spawn.failed", domain="spawn", ids={"spawn_id": "p456"})
        kwargs = {"ids_filter": {"spawn_id": "p123"}}
    _write_segment(telemetry_dir, "123-0001.jsonl", [other, expected])

    assert list(query_events(telemetry_dir, **kwargs)) == [expected]


def test_truncated_lines_are_skipped_gracefully(tmp_path: Path) -> None:
    telemetry_dir = tmp_path / "telemetry"
    event = _event(datetime.now(UTC), "chat.ws.connected")
    path = _write_segment(telemetry_dir, "123-0001.jsonl", [event], extra='{"truncated"')

    assert list(read_events(path)) == [event]


def test_valid_unterminated_telemetry_tail_is_not_committed(tmp_path: Path) -> None:
    path = tmp_path / "segment.jsonl"
    event = _event(datetime.now(UTC), "chat.ws.connected")
    path.write_text(json.dumps(event), encoding="utf-8")

    assert list(read_events(path)) == []


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


def _write_binary_tail_event(path: Path, event: str, extra: dict | None = None) -> None:
    payload: dict = {"ts": "2026-05-01T00:00:00Z", "domain": "test", "event": event}
    if extra:
        payload.update(extra)
    with path.open("ab") as file:
        file.write((json.dumps(payload) + "\n").encode("utf-8"))


def test_tail_events_keeps_byte_stable_offsets_across_handle_lifetimes(
    tmp_path: Path,
) -> None:
    segment = tmp_path / "seg.jsonl"
    events = tail_events(tmp_path, poll_interval=0.001)

    _write_binary_tail_event(segment, "e1", {"msg": "€uro sign"})
    results = [next(events)]
    _write_binary_tail_event(segment, "e2", {"msg": "naïve"})
    results.append(next(events))

    assert results[0]["event"] == "e1"
    assert results[0]["msg"] == "€uro sign"
    assert results[1]["event"] == "e2"
    assert results[1]["msg"] == "naïve"


def test_tail_events_picks_up_segment_created_after_tail_start(tmp_path: Path) -> None:
    events = tail_events(tmp_path, poll_interval=0.001)
    segment = tmp_path / "new_seg.jsonl"
    _write_binary_tail_event(segment, "fresh.event")

    assert next(events)["event"] == "fresh.event"


def test_tail_events_applies_domain_filter_in_binary_mode(tmp_path: Path) -> None:
    segment = tmp_path / "seg.jsonl"
    events = tail_events(tmp_path, domain="wanted", poll_interval=0.001)

    _write_binary_tail_event(segment, "skip.event")
    wanted = {
        "ts": "2026-05-01T00:00:00Z",
        "domain": "wanted",
        "event": "keep.event",
    }
    with segment.open("ab") as file:
        file.write((json.dumps(wanted) + "\n").encode("utf-8"))

    assert next(events)["event"] == "keep.event"
