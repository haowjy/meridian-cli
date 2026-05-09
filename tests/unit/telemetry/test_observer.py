from __future__ import annotations

from datetime import UTC, datetime
from typing import Mapping

from meridian.lib.core.telemetry import LifecycleEvent
from meridian.lib.telemetry.observer import _extract_terminal_data


def _lifecycle_event(event: str, payload: Mapping[str, object]) -> LifecycleEvent:
    return LifecycleEvent(
        event=event,
        spawn_id="spawn-1",
        harness_id="codex",
        model="gpt-5.4",
        agent="coder",
        ts=datetime.now(tz=UTC),
        seq=1,
        payload=dict(payload),
    )


def test_extract_terminal_data_forwards_usage_fields() -> None:
    payload = {
        "status": "succeeded",
        "exit_code": 0,
        "duration_secs": 1.2,
        "total_cost_usd": 2.5,
        "input_tokens": 10,
        "output_tokens": 20,
        "cache_read_input_tokens": 30,
        "cache_creation_input_tokens": 40,
        "reasoning_tokens": 50,
        "cost_is_estimate": True,
    }

    data = _extract_terminal_data(_lifecycle_event("spawn.succeeded", payload))

    assert data == payload


def test_extract_terminal_data_preserves_failed_error_shape() -> None:
    data = _extract_terminal_data(
        _lifecycle_event(
            "spawn.failed",
            {
                "status": "failed",
                "reason": "runner exploded",
                "total_cost_usd": 0.5,
                "input_tokens": 5,
            },
        )
    )

    assert data["status"] == "failed"
    assert data["reason"] == "runner exploded"
    assert data["total_cost_usd"] == 0.5
    assert data["input_tokens"] == 5
    assert data["error"] == {
        "type": "SpawnFailed",
        "message": "runner exploded",
    }
