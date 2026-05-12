"""Control handle for one live backing chat execution."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meridian.lib.core.types import SpawnId

if TYPE_CHECKING:
    from meridian.lib.harness.connections.base import HarnessConnection
    from meridian.lib.streaming.spawn_manager import SpawnManager


class BackendHandle:
    """Live control handle to one backing execution."""

    def __init__(
        self,
        spawn_id: SpawnId,
        spawn_manager: SpawnManager,
        connection: HarnessConnection[Any],
        execution_generation: int,
    ) -> None:
        self.spawn_id = spawn_id
        self._manager = spawn_manager
        self._connection = connection
        self.generation = execution_generation

    @property
    def connection(self) -> HarnessConnection[Any]:
        return self._connection

    async def send_message(self, text: str) -> None:
        await self._manager.inject(self.spawn_id, text)

    async def send_cancel(self) -> None:
        await self._manager.interrupt(self.spawn_id, source="chat.backend_handle.cancel")

    async def respond_request(
        self,
        request_id: str,
        decision: str,
        payload: dict[str, object] | None = None,
    ) -> None:
        await self._manager.respond_request(
            self.spawn_id,
            request_id=request_id,
            decision=decision,
            payload=payload,
            source="chat.backend_handle.approval",
        )

    async def respond_user_input(
        self,
        request_id: str,
        answers: dict[str, object],
    ) -> None:
        await self._manager.respond_user_input(
            self.spawn_id,
            request_id=request_id,
            answers=answers,
            source="chat.backend_handle.user_input",
        )

    def health(self) -> bool:
        return self._connection.health()

    async def stop(self) -> None:
        await self._manager.stop_spawn(self.spawn_id)


__all__ = ["BackendHandle"]
