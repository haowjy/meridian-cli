"""Chat lifecycle state machine and backend execution tracking."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal

from meridian.lib.chat.protocol import CHAT_EXITED, ChatEvent, utc_now_iso

if TYPE_CHECKING:
    from meridian.lib.chat.backend_acquisition import BackendAcquisition
    from meridian.lib.chat.backend_handle import BackendHandle
    from meridian.lib.chat.event_pipeline import ChatEventPipeline

ChatState = Literal["idle", "active", "draining", "closed"]
StateTransition = tuple[ChatState, ChatState]


class ChatSessionError(RuntimeError):
    """Base error for chat session failures."""


class ConcurrentPromptError(ChatSessionError):
    def __init__(self, chat_id: str) -> None:
        super().__init__(f"chat {chat_id} already has an active or draining turn")
        self.chat_id = chat_id


class ChatClosedError(ChatSessionError):
    def __init__(self, chat_id: str) -> None:
        super().__init__(f"chat {chat_id} is closed")
        self.chat_id = chat_id


class ChatSessionService:
    """Own live chat lifecycle, turn mutex, and execution generation."""

    def __init__(self, chat_id: str, backend_acquisition: BackendAcquisition) -> None:
        self._chat_id = chat_id
        self._acquisition = backend_acquisition
        self._state: ChatState = "idle"
        self._state_lock = asyncio.Lock()
        self._current_execution: BackendHandle | None = None
        self._execution_generation = 0
        self._pending_state_transitions: list[StateTransition] = []

    @property
    def chat_id(self) -> str:
        return self._chat_id

    @property
    def state(self) -> ChatState:
        return self._state

    @property
    def current_execution(self) -> BackendHandle | None:
        return self._current_execution

    @property
    def execution_generation(self) -> int:
        return self._execution_generation

    def _transition_to(self, new_state: ChatState) -> StateTransition | None:
        """Move to ``new_state`` and return the observed transition, if any."""

        previous_state = self._state
        if previous_state == new_state:
            return None
        self._state = new_state
        transition = (previous_state, new_state)
        self._pending_state_transitions.append(transition)
        return transition

    def consume_state_transitions(self) -> list[StateTransition]:
        """Return and clear state transitions awaiting publication."""

        transitions = list(self._pending_state_transitions)
        self._pending_state_transitions.clear()
        return transitions

    def clear_state_transitions(self) -> None:
        """Drop unpublished transitions for an operation that did not accept."""

        self._pending_state_transitions.clear()

    async def prompt(self, text: str) -> None:
        """Send a prompt, acquiring or re-acquiring the backend if needed."""

        async with self._state_lock:
            if self._state == "closed":
                raise ChatClosedError(self._chat_id)
            if self._state in ("active", "draining"):
                raise ConcurrentPromptError(self._chat_id)

            if self._current_execution is not None and not self._current_execution.health():
                self._current_execution = None

            if self._current_execution is None:
                self._execution_generation += 1
                self._transition_to("active")
                try:
                    self._current_execution = await self._acquisition.acquire(
                        self._chat_id,
                        text,
                        execution_generation=self._execution_generation,
                        chat_state=self._state,
                    )
                except Exception:
                    self._transition_to("idle")
                    self._execution_generation -= 1
                    self.clear_state_transitions()
                    raise
                return

            self._transition_to("active")
            try:
                await self._current_execution.send_message(text)
            except Exception:
                self._transition_to("idle")
                self.clear_state_transitions()
                raise

    async def cancel(self) -> None:
        """Cancel the active turn and enter draining until completion callback."""

        async with self._state_lock:
            if self._state in ("idle", "draining", "closed"):
                return
            self._transition_to("draining")
            if self._current_execution is not None:
                await self._current_execution.send_cancel()

    async def close(self, pipeline: ChatEventPipeline | None = None) -> None:
        """Close the chat, stop the backend, and optionally persist chat.exited."""

        async with self._state_lock:
            if self._state == "closed":
                return
            self._transition_to("closed")
            handle = self._current_execution
            self._current_execution = None
            execution_id = ""
            if handle is not None:
                execution_id = str(handle.spawn_id)
                await handle.stop()
            if pipeline is not None:
                await pipeline.ingest(
                    ChatEvent(
                        type=CHAT_EXITED,
                        seq=0,
                        chat_id=self._chat_id,
                        execution_id=execution_id,
                        timestamp=utc_now_iso(),
                    )
                )

    def on_turn_completed(self, execution_generation: int | None = None) -> None:
        """Mark active/draining turn complete, ignoring stale generations."""

        if execution_generation is not None and execution_generation != self._execution_generation:
            return
        if self._state in ("active", "draining"):
            self._transition_to("idle")

    def on_execution_died(self, execution_generation: int | None = None) -> None:
        """Clear a dead backend and restore idle state unless fenced as stale."""

        if execution_generation is not None and execution_generation != self._execution_generation:
            return
        self._current_execution = None
        if self._state in ("active", "draining"):
            self._transition_to("idle")


__all__ = [
    "ChatClosedError",
    "ChatSessionError",
    "ChatSessionService",
    "ChatState",
    "ConcurrentPromptError",
]
