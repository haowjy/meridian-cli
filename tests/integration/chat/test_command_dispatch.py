import pytest

from meridian.lib.chat.command_handler import ChatCommandHandler
from meridian.lib.chat.commands import ChatCommand
from meridian.lib.chat.session_service import ChatSessionService
from meridian.lib.core.types import SpawnId


class Handle:
    spawn_id = SpawnId("s1")
    def health(self): return True
    async def send_message(self, text): pass
    async def send_cancel(self): pass
    async def stop(self): pass
    async def respond_request(self, request_id, decision, payload=None): pass
    async def respond_user_input(self, request_id, answers): pass

class Acquisition:
    async def acquire(
        self,
        chat_id,
        initial_prompt,
        *,
        execution_generation=0,
        chat_state="active",
    ):
        _ = (chat_id, initial_prompt, execution_generation, chat_state)
        return Handle()

@pytest.mark.asyncio
async def test_transport_producers_reuse_same_handler_semantics():
    session = ChatSessionService("c1", Acquisition())
    handler = ChatCommandHandler({"c1": session})
    rest_command = ChatCommand("prompt", "rest", "c1", "now", {"text": "hi"})
    ws_command = ChatCommand("prompt", "ws", "c1", "now", {"text": "again"})

    assert (await handler.dispatch(rest_command)).status == "accepted"
    result = await handler.dispatch(ws_command)

    assert result.status == "rejected"
    assert result.error == "concurrent_prompt"
