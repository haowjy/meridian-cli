"""Persistence-first normalized chat event pipeline."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import TYPE_CHECKING, Protocol

from meridian.lib.chat.event_log import ChatEventLog
from meridian.lib.chat.protocol import (
    CHAT_EXITED,
    RUNTIME_WARNING,
    TURN_COMPLETED,
    ChatEvent,
    utc_now_iso,
)

if TYPE_CHECKING:
    from meridian.lib.chat.session_service import ChatSessionService

logger = logging.getLogger(__name__)

LIFECYCLE_EVENT_TYPES = {TURN_COMPLETED, CHAT_EXITED}
PostPersistHook = Callable[[ChatEvent], list[ChatEvent] | None]


class ChatEventIndex(Protocol):
    def upsert(self, event: ChatEvent) -> None: ...


class ChatEventFanOut(Protocol):
    async def broadcast(self, event: ChatEvent) -> None: ...


class NoopChatEventFanOut:
    async def broadcast(self, event: ChatEvent) -> None:
        _ = event


class ChatEventPipeline:
    """Ingest ChatEvents, persist first, then update projections and callbacks."""

    def __init__(
        self,
        chat_id: str,
        event_log: ChatEventLog,
        session_service: ChatSessionService,
        *,
        event_index: ChatEventIndex | None = None,
        fanout: ChatEventFanOut | None = None,
        turn_completed_callback: Callable[[ChatEvent], Awaitable[None]] | None = None,
        max_queue: int = 10000,
    ) -> None:
        self._chat_id = chat_id
        self._log = event_log
        self._session = session_service
        self._index = event_index
        self._fanout = fanout if fanout is not None else NoopChatEventFanOut()
        self._turn_completed_callback = turn_completed_callback
        self._post_persist_hooks: list[PostPersistHook] = []
        self._queue: asyncio.Queue[ChatEvent | None] = asyncio.Queue(maxsize=max_queue)
        self._task: asyncio.Task[None] | None = None

    @property
    def task(self) -> asyncio.Task[None] | None:
        return self._task

    @property
    def chat_id(self) -> str:
        return self._chat_id

    def set_turn_completed_callback(
        self,
        callback: Callable[[ChatEvent], Awaitable[None]] | None,
    ) -> None:
        self._turn_completed_callback = callback

    def add_post_persist_hook(self, hook: PostPersistHook) -> None:
        """Register a synchronous hook for inline synthetic events.

        Hooks run in the consumer loop after the causative event is persisted,
        indexed, and broadcast. Hooks must treat the causative event as
        read-only. Returned events are appended inline without re-entering
        ``ingest()`` or the queue.
        """

        self._post_persist_hooks.append(hook)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        await self._queue.put(None)
        with suppress(asyncio.CancelledError):
            await self._task

    async def drain(self) -> None:
        """Wait until all currently queued events are persisted and broadcast."""
        await self._queue.join()

    async def ingest(self, event: ChatEvent) -> None:
        """Queue a normalized event; apply lifecycle transitions before drops."""

        if event.chat_id != self._chat_id:
            logger.warning(
                "Dropping chat event for wrong chat: pipeline=%s event=%s",
                self._chat_id,
                event.chat_id,
            )
            return
        lifecycle_already_notified = event.type in LIFECYCLE_EVENT_TYPES
        if lifecycle_already_notified:
            self._notify_session(event)
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning(
                "Chat pipeline queue full for %s, dropping %s",
                self._chat_id,
                event.type,
            )
            warning = ChatEvent(
                type=RUNTIME_WARNING,
                seq=0,
                chat_id=self._chat_id,
                execution_id=event.execution_id,
                timestamp=utc_now_iso(),
                payload={"reason": "pipeline_queue_full", "dropped_type": event.type},
                harness_id=event.harness_id,
            )
            with suppress(asyncio.QueueFull):
                self._queue.put_nowait(warning)

    async def _run(self) -> None:
        while True:
            event = await self._queue.get()
            if event is None:
                self._queue.task_done()
                break
            persisted = await self._persist_index_and_broadcast(event)
            await self._emit_post_persist_events(persisted)
            if persisted.type == TURN_COMPLETED and self._turn_completed_callback is not None:
                try:
                    await self._turn_completed_callback(persisted)
                except Exception:
                    logger.warning("Chat checkpoint creation failed", exc_info=True)
            if persisted.type not in LIFECYCLE_EVENT_TYPES:
                self._notify_session(persisted)
            self._queue.task_done()
            if persisted.type == CHAT_EXITED:
                break

    async def _persist_index_and_broadcast(self, event: ChatEvent) -> ChatEvent:
        persisted = self._log.append(event)
        if self._index is not None:
            try:
                self._index.upsert(persisted)
            except Exception:
                logger.warning("Chat event index upsert failed", exc_info=True)
        await self._fanout.broadcast(persisted)
        return persisted

    async def _emit_post_persist_events(self, persisted: ChatEvent) -> None:
        for hook in self._post_persist_hooks:
            try:
                events = hook(persisted)
            except Exception:
                logger.warning("Post-persist hook failed", exc_info=True)
                continue
            if not events:
                continue
            for event in events:
                if event.chat_id != self._chat_id:
                    logger.warning(
                        "Dropping synthetic event with wrong chat_id: %s",
                        event.chat_id,
                    )
                    continue
                if event.type in LIFECYCLE_EVENT_TYPES:
                    logger.warning("Dropping lifecycle synthetic event: %s", event.type)
                    continue
                await self._persist_index_and_broadcast(event)

    async def on_execution_complete(self, execution_generation: int | None = None) -> None:
        self._session.on_execution_died(execution_generation)

    def _notify_session(self, event: ChatEvent) -> None:
        generation = _execution_generation(event)
        if event.type == TURN_COMPLETED:
            self._session.on_turn_completed(generation)


def _execution_generation(event: ChatEvent) -> int | None:
    value = event.payload.get("execution_generation")
    if isinstance(value, int):
        return value
    return None


__all__ = [
    "ChatEventFanOut",
    "ChatEventIndex",
    "ChatEventPipeline",
    "NoopChatEventFanOut",
]
