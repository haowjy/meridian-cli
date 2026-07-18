"""Per-session control action coordinator for managed runtime actions."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.connections.base import ConnectionNotReady
from meridian.lib.state.atomic import append_durable_jsonl_line
from meridian.lib.state.spawn_aggregate import mutate_published_spawn_artifact


class ControlActionType(StrEnum):
    """Control actions routed to an active harness connection."""

    INTERRUPT = "interrupt"
    INJECT = "inject"
    PERMISSION_REPLY = "permission_reply"
    USER_INPUT_REPLY = "user_input_reply"


class ControlActionStatus(StrEnum):
    """Durable status transitions for one control action."""

    REQUESTED = "requested"
    SENT = "sent"
    ACKNOWLEDGED = "acknowledged"
    FAILED = "failed"


@dataclass(frozen=True)
class ControlActionResult:
    """Result envelope returned by the coordinator."""

    success: bool
    action_id: str
    value: object | None = None
    error: str | None = None


@dataclass(frozen=True)
class _CounterState:
    transition_seq: int
    action_seq: int
    submission_seq: int
    interrupt_epoch: int
    interrupt_fence_submission: int


class ControlActionCoordinator:
    """Serialize and persist runtime control actions for one active session."""

    def __init__(
        self,
        *,
        spawn_id: SpawnId,
        spawn_dir: Path,
        default_max_attempts: int = 2,
        retry_delay_secs: float = 0.05,
    ) -> None:
        self._spawn_id = spawn_id
        self._actions_path = spawn_dir / "control_actions.jsonl"
        self._runtime_root = spawn_dir.parent.parent
        self._default_max_attempts = max(1, int(default_max_attempts))
        self._retry_delay_secs = max(0.0, float(retry_delay_secs))
        self._lock = asyncio.Lock()
        state = self._load_counter_state()
        self._transition_seq = state.transition_seq
        self._action_seq = state.action_seq
        self._submission_seq = state.submission_seq
        self._interrupt_epoch = state.interrupt_epoch
        self._interrupt_fence_submission = state.interrupt_fence_submission

    @property
    def actions_path(self) -> Path:
        """Path for durable control-action transition records."""

        return self._actions_path

    async def run_action(
        self,
        *,
        action: ControlActionType,
        payload: dict[str, object],
        source: str,
        send: Callable[[], Awaitable[object]],
        max_attempts: int | None = None,
        fence_on_interrupt: bool = True,
    ) -> ControlActionResult:
        """Run one action via serialized send + durable transition records."""

        submission_seq = self._reserve_submission_seq()
        action_id = self._next_action_id()

        async with self._lock:
            if action is ControlActionType.INTERRUPT:
                self._interrupt_epoch += 1
                self._interrupt_fence_submission = max(
                    self._interrupt_fence_submission, submission_seq
                )
            interrupt_epoch = self._interrupt_epoch
            await self._append_transition(
                action_id=action_id,
                action=action,
                status=ControlActionStatus.REQUESTED,
                submission_seq=submission_seq,
                interrupt_epoch=interrupt_epoch,
                source=source,
                payload=payload,
                attempt=0,
            )

            if (
                action is not ControlActionType.INTERRUPT
                and fence_on_interrupt
                and submission_seq < self._interrupt_fence_submission
            ):
                error_text = "stale_after_interrupt"
                await self._append_transition(
                    action_id=action_id,
                    action=action,
                    status=ControlActionStatus.FAILED,
                    submission_seq=submission_seq,
                    interrupt_epoch=interrupt_epoch,
                    source=source,
                    payload=payload,
                    attempt=0,
                    error=error_text,
                )
                return ControlActionResult(success=False, action_id=action_id, error=error_text)

            attempts = self._resolved_attempts(max_attempts)
            last_error: str | None = None
            for attempt in range(1, attempts + 1):
                await self._append_transition(
                    action_id=action_id,
                    action=action,
                    status=ControlActionStatus.SENT,
                    submission_seq=submission_seq,
                    interrupt_epoch=interrupt_epoch,
                    source=source,
                    payload=payload,
                    attempt=attempt,
                )
                try:
                    send_result = await send()
                except Exception as exc:
                    last_error = str(exc)
                    retryable = attempt < attempts and self._is_retryable(exc)
                    if retryable:
                        await asyncio.sleep(self._retry_delay_secs)
                        continue
                    await self._append_transition(
                        action_id=action_id,
                        action=action,
                        status=ControlActionStatus.FAILED,
                        submission_seq=submission_seq,
                        interrupt_epoch=interrupt_epoch,
                        source=source,
                        payload=payload,
                        attempt=attempt,
                        error=last_error,
                    )
                    return ControlActionResult(
                        success=False,
                        action_id=action_id,
                        error=last_error,
                    )

                await self._append_transition(
                    action_id=action_id,
                    action=action,
                    status=ControlActionStatus.ACKNOWLEDGED,
                    submission_seq=submission_seq,
                    interrupt_epoch=interrupt_epoch,
                    source=source,
                    payload=payload,
                    attempt=attempt,
                )
                return ControlActionResult(
                    success=True,
                    action_id=action_id,
                    value=send_result,
                )

            return ControlActionResult(success=False, action_id=action_id, error=last_error)

    def _reserve_submission_seq(self) -> int:
        submission_seq = self._submission_seq
        self._submission_seq += 1
        return submission_seq

    def _next_action_id(self) -> str:
        action_seq = self._action_seq
        self._action_seq += 1
        return f"ca-{action_seq}"

    def _resolved_attempts(self, max_attempts: int | None) -> int:
        if max_attempts is None:
            return self._default_max_attempts
        return max(1, int(max_attempts))

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        return isinstance(exc, (ConnectionNotReady, TimeoutError, ConnectionError, OSError))

    async def _append_transition(
        self,
        *,
        action_id: str,
        action: ControlActionType,
        status: ControlActionStatus,
        submission_seq: int,
        interrupt_epoch: int,
        source: str,
        payload: dict[str, object],
        attempt: int,
        error: str | None = None,
    ) -> None:
        record: dict[str, object] = {
            "seq": self._transition_seq,
            "spawn_id": str(self._spawn_id),
            "action_id": action_id,
            "action": action.value,
            "status": status.value,
            "submission_seq": submission_seq,
            "attempt": attempt,
            "interrupt_epoch": interrupt_epoch,
            "source": source,
            "ts": time.time(),
            "payload": payload,
        }
        if error is not None:
            record["error"] = error
        self._transition_seq += 1
        line = json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
        await asyncio.to_thread(
            mutate_published_spawn_artifact,
            self._runtime_root,
            self._spawn_id,
            lambda: append_durable_jsonl_line(self._actions_path, line),
        )

    def _load_counter_state(self) -> _CounterState:
        if not self._actions_path.exists():
            return _CounterState(
                transition_seq=0,
                action_seq=0,
                submission_seq=0,
                interrupt_epoch=0,
                interrupt_fence_submission=-1,
            )

        transition_seq = 0
        action_seq = 0
        submission_seq = 0
        interrupt_epoch = 0
        interrupt_fence_submission = -1
        with self._actions_path.open(encoding="utf-8", errors="ignore") as handle:
            for raw_line in handle:
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    payload = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                payload = cast("dict[str, object]", payload)

                seq = payload.get("seq")
                if isinstance(seq, int):
                    transition_seq = max(transition_seq, seq + 1)

                action_id = payload.get("action_id")
                if isinstance(action_id, str) and action_id.startswith("ca-"):
                    try:
                        action_number = int(action_id[3:])
                    except ValueError:
                        action_number = -1
                    if action_number >= 0:
                        action_seq = max(action_seq, action_number + 1)

                submitted = payload.get("submission_seq")
                if isinstance(submitted, int):
                    submission_seq = max(submission_seq, submitted + 1)

                epoch = payload.get("interrupt_epoch")
                if isinstance(epoch, int):
                    interrupt_epoch = max(interrupt_epoch, epoch)

                action = payload.get("action")
                if action == ControlActionType.INTERRUPT.value and isinstance(submitted, int):
                    interrupt_fence_submission = max(interrupt_fence_submission, submitted)

        return _CounterState(
            transition_seq=transition_seq,
            action_seq=action_seq,
            submission_seq=submission_seq,
            interrupt_epoch=interrupt_epoch,
            interrupt_fence_submission=interrupt_fence_submission,
        )


__all__ = [
    "ControlActionCoordinator",
    "ControlActionResult",
    "ControlActionStatus",
    "ControlActionType",
]
