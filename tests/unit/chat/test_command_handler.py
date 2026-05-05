import pytest

from meridian.lib.chat.command_handler import ChatCommandHandler
from meridian.lib.chat.commands import ChatCommand
from meridian.lib.chat.protocol import CHAT_EXITED, USER_PROMPT
from meridian.lib.chat.session_service import ChatSessionService
from meridian.lib.core.types import SpawnId


class FakeHandle:
    spawn_id = SpawnId("s1")

    def __init__(self):
        self.requests = []
        self.inputs = []
        self.cancel_calls = 0
        self.stop_calls = 0

    def health(self):
        return True

    async def send_message(self, text):
        _ = text

    async def send_cancel(self):
        self.cancel_calls += 1

    async def stop(self):
        self.stop_calls += 1

    async def respond_request(self, request_id, decision, payload=None):
        self.requests.append((request_id, decision, payload))

    async def respond_user_input(self, request_id, answers):
        self.inputs.append((request_id, answers))


class FakeAcquisition:
    def __init__(self):
        self.handle = FakeHandle()

    async def acquire(
        self,
        chat_id,
        initial_prompt,
        *,
        execution_generation=0,
        chat_state="active",
    ):
        _ = (chat_id, initial_prompt, execution_generation, chat_state)
        return self.handle


class FakePipeline:
    def __init__(self):
        self.events = []

    async def ingest(self, event):
        self.events.append(event)


class FakeCheckpoint:
    def __init__(self):
        self.reverted = []

    async def revert_to_checkpoint(self, commit_sha):
        self.reverted.append(commit_sha)


def command(kind, payload=None):
    return ChatCommand(kind, "cmd1", "c1", "now", payload or {})


@pytest.mark.asyncio
async def test_unknown_chat_rejected():
    result = await ChatCommandHandler({}).dispatch(command("prompt", {"text": "hi"}))
    assert result.status == "rejected"
    assert result.error == "chat_not_found"


@pytest.mark.asyncio
async def test_prompt_and_concurrent_prompt_dispatch():
    session = ChatSessionService("c1", FakeAcquisition())
    handler = ChatCommandHandler({"c1": session})

    assert (await handler.dispatch(command("prompt", {"text": "hi"}))).status == "accepted"
    result = await handler.dispatch(command("prompt", {"text": "again"}))

    assert result.status == "rejected"
    assert result.error == "concurrent_prompt"


@pytest.mark.asyncio
async def test_accepted_prompt_emits_user_prompt_event_once():
    acquisition = FakeAcquisition()
    session = ChatSessionService("c1", acquisition)
    pipeline = FakePipeline()
    handler = ChatCommandHandler({"c1": session}, pipelines={"c1": pipeline})

    result = await handler.dispatch(command("prompt", {"text": "hi"}))

    assert result.status == "accepted"
    assert [event.type for event in pipeline.events] == [USER_PROMPT]
    assert pipeline.events[0].payload == {"text": "hi"}
    assert pipeline.events[0].execution_id == "s1"


@pytest.mark.asyncio
async def test_cancel_dispatches_to_session_and_backend():
    acquisition = FakeAcquisition()
    session = ChatSessionService("c1", acquisition)
    await session.prompt("hi")
    handler = ChatCommandHandler({"c1": session})

    result = await handler.dispatch(command("cancel"))

    assert result.status == "accepted"
    assert session.state == "draining"
    assert acquisition.handle.cancel_calls == 1


@pytest.mark.asyncio
async def test_close_dispatch_stops_backend_and_emits_chat_exited_when_pipeline_present():
    acquisition = FakeAcquisition()
    session = ChatSessionService("c1", acquisition)
    await session.prompt("hi")
    pipeline = FakePipeline()
    handler = ChatCommandHandler({"c1": session}, pipelines={"c1": pipeline})

    result = await handler.dispatch(command("close"))

    assert result.status == "accepted"
    assert session.state == "closed"
    assert acquisition.handle.stop_calls == 1
    assert [event.type for event in pipeline.events] == [CHAT_EXITED]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("swap_model", {"model": "x"}),
        ("swap_effort", {"effort": "high"}),
    ],
)
async def test_unsupported_deferred_commands_are_intentionally_always_rejected(kind, payload):
    session = ChatSessionService("c1", FakeAcquisition())
    handler = ChatCommandHandler({"c1": session})

    result = await handler.dispatch(command(kind, payload))

    assert result.status == "rejected"
    assert result.error == "not_supported_by_current_harness"


@pytest.mark.asyncio
async def test_approve_requires_active_execution():
    session = ChatSessionService("c1", FakeAcquisition())
    handler = ChatCommandHandler({"c1": session})

    result = await handler.dispatch(command("approve", {"request_id": "r1", "decision": "accept"}))

    assert result.status == "rejected"
    assert result.error == "no_active_execution"


@pytest.mark.asyncio
async def test_approve_routes_to_backend_handle_and_emits_resolution_event():
    acquisition = FakeAcquisition()
    session = ChatSessionService("c1", acquisition)
    await session.prompt("hi")
    pipeline = FakePipeline()
    handler = ChatCommandHandler({"c1": session}, pipelines={"c1": pipeline})

    result = await handler.dispatch(
        command(
            "approve",
            {"request_id": "r1", "decision": "accept", "payload": {"x": 1}},
        )
    )

    assert result.status == "accepted"
    assert acquisition.handle.requests == [("r1", "accept", {"x": 1})]
    assert [event.type for event in pipeline.events] == ["request.resolved"]
    assert pipeline.events[0].payload["command_id"] == "cmd1"
    assert pipeline.events[0].payload["decision"] == "accept"


@pytest.mark.asyncio
async def test_answer_input_routes_to_backend_handle_and_emits_resolution_event():
    acquisition = FakeAcquisition()
    session = ChatSessionService("c1", acquisition)
    await session.prompt("hi")
    pipeline = FakePipeline()
    handler = ChatCommandHandler({"c1": session}, pipelines={"c1": pipeline})

    result = await handler.dispatch(
        command(
            "answer_input",
            {"request_id": "r1", "answers": {"q1": "yes", "count": 2}},
        )
    )

    assert result.status == "accepted"
    assert acquisition.handle.inputs == [("r1", {"q1": "yes", "count": 2})]
    assert [event.type for event in pipeline.events] == ["user_input.resolved"]
    assert pipeline.events[0].payload["command_id"] == "cmd1"
    assert pipeline.events[0].payload["answers"] == {"q1": "yes", "count": 2}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "payload", "expected_error"),
    [
        ("prompt", {}, "invalid_command:missing_text"),
        (
            "approve",
            {"request_id": "r1", "decision": "accept", "payload": []},
            "invalid_command:payload_not_object",
        ),
        ("answer_input", {"request_id": "r1"}, "invalid_command:missing_answers"),
        (
            "answer_input",
            {"request_id": "r1", "answers": []},
            "invalid_command:missing_answers",
        ),
        ("revert", {}, "invalid_command:missing_commit_sha"),
    ],
)
async def test_invalid_command_payloads_are_rejected(kind, payload, expected_error):
    acquisition = FakeAcquisition()
    session = ChatSessionService("c1", acquisition)
    await session.prompt("hi")
    checkpoints = {"c1": FakeCheckpoint()} if kind == "revert" else None
    handler = ChatCommandHandler({"c1": session}, checkpoints=checkpoints)

    result = await handler.dispatch(command(kind, payload))

    assert result.status == "rejected"
    assert result.error == expected_error


@pytest.mark.asyncio
async def test_revert_dispatches_to_checkpoint_service():
    session = ChatSessionService("c1", FakeAcquisition())
    checkpoint = FakeCheckpoint()
    handler = ChatCommandHandler({"c1": session}, checkpoints={"c1": checkpoint})

    result = await handler.dispatch(command("revert", {"commit_sha": "abc123"}))

    assert result.status == "accepted"
    assert checkpoint.reverted == ["abc123"]


@pytest.mark.asyncio
async def test_revert_rejected_when_checkpoint_service_not_configured():
    session = ChatSessionService("c1", FakeAcquisition())
    handler = ChatCommandHandler({"c1": session})

    result = await handler.dispatch(command("revert", {"commit_sha": "abc123"}))

    assert result.status == "rejected"
    assert result.error == "checkpoint_not_configured"
