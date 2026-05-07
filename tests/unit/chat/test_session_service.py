import pytest

from meridian.lib.chat.protocol import CHAT_EXITED
from meridian.lib.chat.session_service import (
    ChatClosedError,
    ChatSessionService,
    ConcurrentPromptError,
)
from meridian.lib.core.types import SpawnId


class FakeHandle:
    def __init__(self, spawn_id: str = "s1", healthy: bool = True):
        self.spawn_id = SpawnId(spawn_id)
        self._healthy = healthy
        self.messages: list[str] = []
        self.cancelled = False
        self.stop_calls = 0

    def health(self):
        return self._healthy

    async def send_message(self, text):
        self.messages.append(text)

    async def send_cancel(self):
        self.cancelled = True

    async def stop(self):
        self.stop_calls += 1


class FakePipeline:
    def __init__(self):
        self.events = []

    async def ingest(self, event):
        self.events.append(event)


class FakeAcquisition:
    def __init__(self, *, fail_attempts: int = 0):
        self.fail_attempts = fail_attempts
        self.handles: list[FakeHandle] = []
        self.prompts = []
        self._counter = 0

    async def acquire(
        self,
        chat_id,
        initial_prompt,
        *,
        execution_generation=0,
        chat_state="active",
    ):
        self.prompts.append((chat_id, initial_prompt, execution_generation, chat_state))
        if self.fail_attempts > 0:
            self.fail_attempts -= 1
            raise RuntimeError("acquire failed")
        self._counter += 1
        handle = FakeHandle(spawn_id=f"s{self._counter}")
        self.handles.append(handle)
        return handle


def test_transition_to_returns_none_for_same_state_and_tuple_for_change():
    session = ChatSessionService("c1", FakeAcquisition())

    assert session._transition_to("idle") is None
    assert session.state == "idle"

    assert session._transition_to("active") == ("idle", "active")
    assert session.state == "active"


@pytest.mark.asyncio
async def test_first_prompt_acquires_backend_and_sets_active():
    acquisition = FakeAcquisition()
    session = ChatSessionService("c1", acquisition)

    await session.prompt("hello")

    assert session.state == "active"
    assert acquisition.prompts == [("c1", "hello", 1, "active")]
    assert session.execution_generation == 1
    assert session.current_execution is acquisition.handles[0]


@pytest.mark.asyncio
async def test_failed_acquisition_rolls_back_to_idle_and_allows_retry():
    acquisition = FakeAcquisition(fail_attempts=1)
    session = ChatSessionService("c1", acquisition)

    with pytest.raises(RuntimeError, match="acquire failed"):
        await session.prompt("first")

    assert session.state == "idle"
    assert session.current_execution is None
    assert session.execution_generation == 0

    await session.prompt("second")

    assert session.state == "active"
    assert session.execution_generation == 1
    assert acquisition.prompts == [
        ("c1", "first", 1, "active"),
        ("c1", "second", 1, "active"),
    ]


@pytest.mark.asyncio
async def test_active_prompt_rejected_until_turn_completed():
    session = ChatSessionService("c1", FakeAcquisition())
    await session.prompt("hello")

    with pytest.raises(ConcurrentPromptError):
        await session.prompt("again")

    session.on_turn_completed(session.execution_generation)
    await session.prompt("again")
    assert session.state == "active"


@pytest.mark.asyncio
async def test_subsequent_prompts_reuse_healthy_backend_without_new_acquisition():
    acquisition = FakeAcquisition()
    session = ChatSessionService("c1", acquisition)

    await session.prompt("hello")
    handle = session.current_execution
    session.on_turn_completed(session.execution_generation)
    await session.prompt("again")

    assert len(acquisition.handles) == 1
    assert session.current_execution is handle
    assert handle.messages == ["again"]
    assert acquisition.prompts == [("c1", "hello", 1, "active")]


@pytest.mark.asyncio
async def test_stale_generation_callbacks_are_ignored():
    session = ChatSessionService("c1", FakeAcquisition())
    await session.prompt("hello")
    handle = session.current_execution

    session.on_turn_completed(session.execution_generation + 1)
    session.on_execution_died(session.execution_generation + 1)

    assert session.state == "active"
    assert session.current_execution is handle


def test_stale_turn_completed_does_not_transition_from_draining():
    session = ChatSessionService("c1", FakeAcquisition())
    session._transition_to("draining")

    session.on_turn_completed(1)

    assert session.state == "draining"


def test_stale_execution_died_does_not_clear_handle_or_transition():
    session = ChatSessionService("c1", FakeAcquisition())
    handle = FakeHandle()
    session._current_execution = handle
    session._transition_to("active")

    session.on_execution_died(1)

    assert session.state == "active"
    assert session.current_execution is handle


@pytest.mark.asyncio
async def test_cancel_drains_and_completion_returns_idle():
    session = ChatSessionService("c1", FakeAcquisition())
    await session.prompt("hello")
    handle = session.current_execution

    await session.cancel()

    assert session.state == "draining"
    assert handle.cancelled
    session.on_turn_completed(session.execution_generation)
    assert session.state == "idle"


@pytest.mark.asyncio
async def test_idle_cancel_is_success_without_side_effect():
    session = ChatSessionService("c1", FakeAcquisition())

    await session.cancel()

    assert session.state == "idle"


@pytest.mark.asyncio
async def test_closed_cancel_is_no_op():
    session = ChatSessionService("c1", FakeAcquisition())
    await session.close()

    await session.cancel()

    assert session.state == "closed"


@pytest.mark.asyncio
async def test_execution_death_clears_handle_and_next_prompt_reacquires_backend():
    acquisition = FakeAcquisition()
    session = ChatSessionService("c1", acquisition)
    await session.prompt("one")
    first = session.current_execution

    session.on_execution_died(session.execution_generation)

    assert session.state == "idle"
    assert session.current_execution is None

    await session.prompt("two")

    assert session.current_execution is not first
    assert len(acquisition.handles) == 2
    assert session.execution_generation == 2


@pytest.mark.asyncio
async def test_dead_backend_reacquired_on_prompt_when_health_check_fails():
    acquisition = FakeAcquisition()
    session = ChatSessionService("c1", acquisition)
    await session.prompt("one")
    first = session.current_execution
    first._healthy = False
    session.on_turn_completed(session.execution_generation)

    await session.prompt("two")

    assert len(acquisition.handles) == 2
    assert session.current_execution is not first
    assert session.execution_generation == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("starting_state", ["active", "draining"])
async def test_close_from_active_or_draining_stops_backend_and_is_idempotent(starting_state):
    session = ChatSessionService("c1", FakeAcquisition())
    pipeline = FakePipeline()
    await session.prompt("hello")
    handle = session.current_execution

    if starting_state == "draining":
        await session.cancel()
        assert session.state == "draining"

    await session.close(pipeline)
    await session.close(pipeline)

    assert session.state == "closed"
    assert session.current_execution is None
    assert handle.stop_calls == 1
    assert [event.type for event in pipeline.events] == [CHAT_EXITED]


@pytest.mark.asyncio
async def test_close_blocks_prompts():
    session = ChatSessionService("c1", FakeAcquisition())
    await session.close()

    with pytest.raises(ChatClosedError):
        await session.prompt("no")


@pytest.mark.asyncio
async def test_close_on_closed_session_is_no_op_without_pipeline_event():
    session = ChatSessionService("c1", FakeAcquisition())
    pipeline = FakePipeline()

    await session.close(pipeline)
    await session.close(pipeline)

    assert session.state == "closed"
    assert len(pipeline.events) == 1


@pytest.mark.asyncio
async def test_acquisition_failure_rolls_back_state_and_generation():
    session = ChatSessionService("c1", FakeAcquisition(fail_attempts=1))

    with pytest.raises(RuntimeError, match="acquire failed"):
        await session.prompt("hello")

    assert session.state == "idle"
    assert session.execution_generation == 0
    assert session.current_execution is None


@pytest.mark.asyncio
async def test_state_paths_flow_through_expected_lifecycle_values():
    acquisition = FakeAcquisition()
    session = ChatSessionService("c1", acquisition)

    await session.prompt("hello")
    assert session.state == "active"

    await session.cancel()
    assert session.state == "draining"

    session.on_turn_completed(session.execution_generation)
    assert session.state == "idle"

    await session.prompt("again")
    assert session.state == "active"

    session.on_execution_died(session.execution_generation)
    assert session.state == "idle"

    await session.close()
    assert session.state == "closed"
