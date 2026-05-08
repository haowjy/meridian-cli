from __future__ import annotations

import json
from pathlib import Path

import pytest

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.connections.base import ConnectionNotReady
from meridian.lib.harness.control_action import ControlActionCoordinator, ControlActionType


def _read_records(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    records: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        records.append(json.loads(line))
    return records


@pytest.mark.asyncio
async def test_control_action_coordinator_records_requested_sent_acknowledged(
    tmp_path: Path,
) -> None:
    coordinator = ControlActionCoordinator(
        spawn_id=SpawnId("p1"),
        spawn_dir=tmp_path,
    )

    async def _send() -> int:
        return 42

    result = await coordinator.run_action(
        action=ControlActionType.INJECT,
        payload={"text": "hello"},
        source="test",
        send=_send,
    )

    assert result.success is True
    assert result.value == 42
    records = _read_records(coordinator.actions_path)
    assert [record["status"] for record in records] == ["requested", "sent", "acknowledged"]
    assert all(record["action"] == "inject" for record in records)


@pytest.mark.asyncio
async def test_control_action_coordinator_retries_retryable_send_failure(
    tmp_path: Path,
) -> None:
    coordinator = ControlActionCoordinator(
        spawn_id=SpawnId("p1"),
        spawn_dir=tmp_path,
    )
    attempts = 0

    async def _send() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionNotReady("not ready")
        return "ok"

    result = await coordinator.run_action(
        action=ControlActionType.PERMISSION_REPLY,
        payload={"request_id": "r1", "decision": "accept"},
        source="test",
        send=_send,
    )

    assert result.success is True
    assert attempts == 2
    records = _read_records(coordinator.actions_path)
    assert [record["status"] for record in records] == [
        "requested",
        "sent",
        "sent",
        "acknowledged",
    ]


@pytest.mark.asyncio
async def test_control_action_coordinator_records_failed_after_retry_exhausted(
    tmp_path: Path,
) -> None:
    coordinator = ControlActionCoordinator(
        spawn_id=SpawnId("p1"),
        spawn_dir=tmp_path,
    )

    async def _send() -> None:
        raise ConnectionNotReady("still not ready")

    result = await coordinator.run_action(
        action=ControlActionType.USER_INPUT_REPLY,
        payload={"request_id": "u1", "answers": {"text": "Ada"}},
        source="test",
        send=_send,
    )

    assert result.success is False
    records = _read_records(coordinator.actions_path)
    assert [record["status"] for record in records] == [
        "requested",
        "sent",
        "sent",
        "failed",
    ]
    assert records[-1]["error"] == "still not ready"


@pytest.mark.asyncio
async def test_control_action_coordinator_fences_stale_actions_after_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = ControlActionCoordinator(
        spawn_id=SpawnId("p1"),
        spawn_dir=tmp_path,
    )

    async def _send_interrupt() -> None:
        return None

    interrupt_result = await coordinator.run_action(
        action=ControlActionType.INTERRUPT,
        payload={},
        source="test",
        send=_send_interrupt,
    )
    assert interrupt_result.success is True

    monkeypatch.setattr(coordinator, "_reserve_submission_seq", lambda: -1)

    async def _send_inject() -> None:
        return None

    stale_result = await coordinator.run_action(
        action=ControlActionType.INJECT,
        payload={"text": "stale"},
        source="test",
        send=_send_inject,
    )

    assert stale_result.success is False
    assert stale_result.error == "stale_after_interrupt"
    records = _read_records(coordinator.actions_path)
    statuses_for_stale = [
        record["status"]
        for record in records
        if record["action_id"] == stale_result.action_id
    ]
    assert statuses_for_stale == ["requested", "failed"]
