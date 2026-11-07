from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from meridian.lib.core.delivery import (
    SEND_TIMEOUT_SECONDS,
    Deliver,
    DeliveryError,
    DeliveryKind,
    DeliveryRefused,
)
from meridian.lib.state.jsonl import append_durable_jsonl_line
from meridian.lib.state.paths import LaunchPaths
from meridian.lib.state.spawn_aggregate import mutate_published_spawn_artifact

# One retry of a refused send, after a short pause.
_MAX_ATTEMPTS = 2
_RETRY_DELAY_SECONDS = 0.05


class ControlActionType(StrEnum):
    """Control actions routed to an active harness connection."""

    INTERRUPT = "interrupt"
    INJECT = "inject"


class ControlActionStatus(StrEnum):
    """Durable status transitions for one control action."""

    REQUESTED = "requested"
    SENT = "sent"
    POSTED = "posted"
    ACKNOWLEDGED = "acknowledged"
    FAILED = "failed"


@dataclass(frozen=True)
class _CounterState:
    transition_seq: int
    action_seq: int
    submission_seq: int
    interrupt_epoch: int
    interrupt_fence_submission: int


def _failure(exc: Exception, action: ControlActionType) -> DeliveryError:
    """The error for the sender: a ``DeliveryError`` passes through; others may follow the write."""

    if isinstance(exc, DeliveryError):
        return exc
    text = str(exc) or type(exc).__name__
    if action is ControlActionType.INTERRUPT:
        return DeliveryError(text)
    return DeliveryError(f"{text}; the message may have arrived and was not resent")


class ControlActionCoordinator:
    """Serialize and persist runtime control actions for one active session."""

    def __init__(self, *, launch: LaunchPaths) -> None:
        self._spawn_id = launch.spawn_id
        self._actions_path = launch.control_actions
        self._runtime_root = launch.runtime_root
        self._lock = asyncio.Lock()
        state = self._load_counter_state()
        self._transition_seq = state.transition_seq
        self._action_seq = state.action_seq
        self._submission_seq = state.submission_seq
        self._interrupt_epoch = state.interrupt_epoch
        self._interrupt_fence_submission = state.interrupt_fence_submission

    async def deliver(self, send: Deliver, text: str, *, source: str) -> DeliveryKind:
        """Journal and serialize one user message; a failure raises ``DeliveryError``."""

        return await self.run_action(
            action=ControlActionType.INJECT,
            payload={"text": text},
            source=source,
            send=lambda: send(text),
        )

    async def run_action[T: DeliveryKind | None](
        self,
        *,
        action: ControlActionType,
        payload: dict[str, object],
        source: str,
        send: Callable[[], Awaitable[T]],
    ) -> T:

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
                raise DeliveryRefused(error_text)

            attempt = 0
            while True:
                attempt += 1
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
                bound = asyncio.timeout(SEND_TIMEOUT_SECONDS)
                try:
                    async with bound:
                        delivery = await send()
                except Exception as exc:
                    # Other failures may have delivered; retry only an asserted non-delivery.
                    if attempt < _MAX_ATTEMPTS and isinstance(exc, DeliveryRefused):
                        await asyncio.sleep(_RETRY_DELAY_SECONDS)
                        continue
                    failure = _failure(
                        TimeoutError(f"no answer within {SEND_TIMEOUT_SECONDS:g}s")
                        if bound.expired()
                        else exc,
                        action,
                    )
                    await self._append_transition(
                        action_id=action_id,
                        action=action,
                        status=ControlActionStatus.FAILED,
                        submission_seq=submission_seq,
                        interrupt_epoch=interrupt_epoch,
                        source=source,
                        payload=payload,
                        attempt=attempt,
                        error=str(failure),
                    )
                    raise failure from exc

                # ``posted``: the write finished but no harness confirmed it. A launcher
                # that died mid-write leaves ``sent`` as the last status instead.
                posted = delivery is DeliveryKind.POSTED
                await self._append_transition(
                    action_id=action_id,
                    action=action,
                    status=(
                        ControlActionStatus.POSTED if posted else ControlActionStatus.ACKNOWLEDGED
                    ),
                    submission_seq=submission_seq,
                    interrupt_epoch=interrupt_epoch,
                    source=source,
                    payload=payload,
                    attempt=attempt,
                )
                return delivery

    def _reserve_submission_seq(self) -> int:
        submission_seq = self._submission_seq
        self._submission_seq += 1
        return submission_seq

    def _next_action_id(self) -> str:
        action_seq = self._action_seq
        self._action_seq += 1
        return f"ca-{action_seq}"

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
        line = json.dumps(record, separators=(",", ":"), sort_keys=True)
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
    "ControlActionStatus",
    "ControlActionType",
]
