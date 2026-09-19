"""V2 pre-subscribe terminal reconcile on the liveness-timeout path.

``start()`` posts the prompt before ``events()`` attaches to the live-only
``/api/event`` stream. A turn that terminates in that gap is never replayed, so
the timeout path must re-poll the durable session outcome through the same
guarded helper before declaring a stall. These tests drive the stall branch
deterministically with an already-expired liveness stub: no SSE, no clock.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.harness.connections.liveness import LivenessDecision
from meridian.lib.harness.connections.opencode_v2_http import OpenCodeV2Connection

_PROMPT_POSTED_AT = 1_000.0
_SESSION_PATH = "/api/session/ses_v2"


class _ExpiredLiveness:
    """Liveness double whose silence budget has already elapsed."""

    def evaluate(self) -> LivenessDecision:
        return LivenessDecision.STREAM_STALLED

    def mark_activity(self) -> None:
        return None

    def mark_activity_if_idle(self) -> None:
        return None


class _ReconcileProbeOpenCodeV2Connection(OpenCodeV2Connection):
    def __init__(self, get_responses: list[tuple[int, object, str]]) -> None:
        super().__init__()
        self._state = "connected"
        self._session_id = "ses_v2"
        self._initial_prompt_posted_at = _PROMPT_POSTED_AT
        self._liveness = cast("Any", _ExpiredLiveness())
        self._get_responses = iter(get_responses)
        self.get_paths: list[str] = []

    async def _get_json(self, path: str) -> tuple[int, object | None, str]:
        self.get_paths.append(path)
        try:
            return next(self._get_responses)
        except StopIteration as exc:
            raise AssertionError("Unexpected _get_json call in test") from exc


def _non_terminal_body() -> dict[str, object]:
    return {"data": {"id": "ses_v2"}}


def _terminal_body(outcome: str, *, idle_ms: float) -> dict[str, object]:
    return {
        "data": {
            "id": "ses_v2",
            "outcome": outcome,
            "time": {"idle": idle_ms},
        }
    }


async def _collect(connection: OpenCodeV2Connection) -> list[RawHarnessEvent]:
    return [event async for event in connection.events()]


@pytest.mark.asyncio
async def test_stall_repoll_surfaces_terminal_missed_by_initial_get() -> None:
    connection = _ReconcileProbeOpenCodeV2Connection(
        [
            (200, _non_terminal_body(), ""),
            (
                200,
                _terminal_body("succeeded", idle_ms=_PROMPT_POSTED_AT * 1000.0 + 5_000.0),
                "",
            ),
        ]
    )

    events = await _collect(connection)

    assert [event.event_type for event in events] == ["session.execution.succeeded"]
    assert events[0].payload["reconciled"] is True
    assert connection.get_paths == [_SESSION_PATH, _SESSION_PATH]
    assert connection.state == "connected"


@pytest.mark.asyncio
async def test_stall_repoll_without_terminal_fails_after_one_bounded_poll() -> None:
    connection = _ReconcileProbeOpenCodeV2Connection(
        [
            (200, _non_terminal_body(), ""),
            (200, _non_terminal_body(), ""),
        ]
    )

    events = await _collect(connection)

    assert events == []
    assert connection.get_paths == [_SESSION_PATH, _SESSION_PATH]
    assert connection.state == "failed"


@pytest.mark.asyncio
async def test_stall_repoll_rejects_stale_prior_turn_outcome() -> None:
    connection = _ReconcileProbeOpenCodeV2Connection(
        [
            (200, _non_terminal_body(), ""),
            (
                200,
                _terminal_body("succeeded", idle_ms=_PROMPT_POSTED_AT * 1000.0 - 1.0),
                "",
            ),
        ]
    )

    events = await _collect(connection)

    assert events == []
    assert connection.get_paths == [_SESSION_PATH, _SESSION_PATH]
    assert connection.state == "failed"
