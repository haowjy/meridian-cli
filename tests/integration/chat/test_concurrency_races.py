from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import pytest

from meridian.lib.chat.commands import ChatCommand
from meridian.lib.chat.protocol import TURN_COMPLETED, ChatEvent, utc_now_iso
from meridian.lib.chat.runtime import ChatRuntime
from meridian.lib.core.types import SpawnId

WAIT_TIMEOUT_SECONDS = 2


class Handle:
    def __init__(self, spawn_id: str, *, healthy: bool = True) -> None:
        self.spawn_id = SpawnId(spawn_id)
        self._healthy = healthy
        self.messages: list[str] = []
        self.cancel_calls = 0
        self.stop_calls = 0

    def health(self) -> bool:
        return self._healthy

    async def send_message(self, text: str) -> None:
        self.messages.append(text)

    async def send_cancel(self) -> None:
        self.cancel_calls += 1

    async def stop(self) -> None:
        self.stop_calls += 1


@dataclass
class AcquireControl:
    handle: Handle
    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)


class ScriptedAcquisition:
    def __init__(self, controls: list[AcquireControl]) -> None:
        self._controls = controls
        self._index = 0
        self.calls: list[tuple[str, str, int]] = []

    async def acquire(
        self,
        chat_id: str,
        initial_prompt: str,
        *,
        execution_generation: int = 0,
        chat_state: str = "active",
    ) -> Handle:
        if self._index >= len(self._controls):
            raise AssertionError("unexpected acquire")
        control = self._controls[self._index]
        self._index += 1
        _ = chat_state
        self.calls.append((chat_id, initial_prompt, execution_generation))
        control.started.set()
        await control.release.wait()
        return control.handle


def command(chat_id: str, kind: str, payload: dict[str, object] | None = None) -> ChatCommand:
    return ChatCommand(
        type=kind,
        command_id=str(uuid4()),
        chat_id=chat_id,
        timestamp=utc_now_iso(),
        payload=payload or {},
    )


async def start_runtime(tmp_path: Path, acquisition: ScriptedAcquisition) -> ChatRuntime:
    runtime = ChatRuntime(
        runtime_root=tmp_path,
        project_root=tmp_path,
        backend_acquisition=acquisition,
    )
    await runtime.start()
    return runtime


@pytest.mark.asyncio
async def test_parallel_chat_creation_returns_unique_chat_ids(tmp_path: Path) -> None:
    runtime = await start_runtime(tmp_path, ScriptedAcquisition([]))
    try:
        views = await asyncio.gather(*(runtime.create_chat() for _ in range(16)))
    finally:
        await runtime.stop()

    chat_ids = [view.chat_id for view in views]

    assert len(chat_ids) == 16
    assert len(set(chat_ids)) == 16
    assert all(view.state == "idle" for view in views)


@pytest.mark.asyncio
async def test_concurrent_prompt_dispatch_rejects_overlap_and_ignores_stale_generation(
    tmp_path: Path,
) -> None:
    first = AcquireControl(Handle("s1"))
    second = AcquireControl(Handle("s2"))
    first.release.set()
    acquisition = ScriptedAcquisition([first, second])
    runtime = await start_runtime(tmp_path, acquisition)
    try:
        chat_id = (await runtime.create_chat()).chat_id
        entry = runtime.live_entries[chat_id]

        first_prompt = await runtime.dispatch(command(chat_id, "prompt", {"text": "first"}))
        assert first_prompt.status == "accepted"
        assert entry.session.state == "active"
        overlap = await runtime.dispatch(command(chat_id, "prompt", {"text": "overlap"}))
        assert overlap.status == "rejected"
        assert overlap.error == "concurrent_prompt"

        await entry.pipeline.on_execution_complete(entry.session.execution_generation)
        assert entry.session.state == "idle"

        second_prompt = asyncio.create_task(
            runtime.dispatch(command(chat_id, "prompt", {"text": "second"}))
        )
        await asyncio.wait_for(second.started.wait(), timeout=WAIT_TIMEOUT_SECONDS)
        assert entry.session.execution_generation == 2
        assert entry.session.state == "active"

        await entry.pipeline.ingest(
            ChatEvent(
                type=TURN_COMPLETED,
                seq=0,
                chat_id=chat_id,
                execution_id="s1",
                timestamp=utc_now_iso(),
                payload={"execution_generation": 1},
            )
        )
        await entry.pipeline.drain()

        assert entry.session.state == "active"

        second.release.set()
        assert (await asyncio.wait_for(second_prompt, timeout=WAIT_TIMEOUT_SECONDS)).status == (
            "accepted"
        )
    finally:
        await runtime.stop()

    assert acquisition.calls == [
        (chat_id, "first", 1),
        (chat_id, "second", 2),
    ]


@pytest.mark.asyncio
async def test_close_during_inflight_prompt_leaves_chat_closed_and_unwedged(tmp_path: Path) -> None:
    control = AcquireControl(Handle("s1"))
    runtime = await start_runtime(tmp_path, ScriptedAcquisition([control]))
    try:
        chat_id = (await runtime.create_chat()).chat_id

        prompt_task = asyncio.create_task(
            runtime.dispatch(command(chat_id, "prompt", {"text": "hi"}))
        )
        await asyncio.wait_for(control.started.wait(), timeout=WAIT_TIMEOUT_SECONDS)

        close_task = asyncio.create_task(runtime.dispatch(command(chat_id, "close")))
        control.release.set()

        prompt_result, close_result = await asyncio.wait_for(
            asyncio.gather(prompt_task, close_task),
            timeout=WAIT_TIMEOUT_SECONDS,
        )

        assert prompt_result.status == "accepted"
        assert close_result.status == "accepted"
        assert runtime.get_state(chat_id) == "closed"
        assert runtime.live_entries[chat_id].session.state == "closed"
        assert control.handle.stop_calls == 1

        after_close = await runtime.dispatch(command(chat_id, "prompt", {"text": "after"}))
        assert after_close.status == "rejected"
        assert after_close.error == "chat_closed"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_second_close_is_rejected_as_chat_closed(tmp_path: Path) -> None:
    runtime = await start_runtime(tmp_path, ScriptedAcquisition([]))
    try:
        chat_id = (await runtime.create_chat()).chat_id

        first_close = await runtime.dispatch(command(chat_id, "close"))
        second_close = await runtime.dispatch(command(chat_id, "close"))
    finally:
        await runtime.stop()

    assert first_close.status == "accepted"
    assert second_close.status == "rejected"
    assert second_close.error == "chat_closed"


@pytest.mark.asyncio
async def test_dispatch_after_stop_raises_runtime_stopped_error(tmp_path: Path) -> None:
    runtime = await start_runtime(tmp_path, ScriptedAcquisition([]))
    chat_id = (await runtime.create_chat()).chat_id

    await runtime.stop()

    with pytest.raises(RuntimeError, match="chat runtime is stopped"):
        await runtime.dispatch(command(chat_id, "close"))


@pytest.mark.asyncio
async def test_parallel_prompts_across_different_chats_do_not_block_each_other(
    tmp_path: Path,
) -> None:
    first = AcquireControl(Handle("s1"))
    second = AcquireControl(Handle("s2"))
    acquisition = ScriptedAcquisition([first, second])
    runtime = await start_runtime(tmp_path, acquisition)
    try:
        first_chat = (await runtime.create_chat()).chat_id
        second_chat = (await runtime.create_chat()).chat_id

        first_prompt = asyncio.create_task(
            runtime.dispatch(command(first_chat, "prompt", {"text": "first"}))
        )
        await asyncio.wait_for(first.started.wait(), timeout=WAIT_TIMEOUT_SECONDS)

        second_prompt = asyncio.create_task(
            runtime.dispatch(command(second_chat, "prompt", {"text": "second"}))
        )
        await asyncio.wait_for(second.started.wait(), timeout=WAIT_TIMEOUT_SECONDS)

        first.release.set()
        second.release.set()

        first_result, second_result = await asyncio.wait_for(
            asyncio.gather(first_prompt, second_prompt),
            timeout=WAIT_TIMEOUT_SECONDS,
        )
    finally:
        await runtime.stop()

    assert first_result.status == "accepted"
    assert second_result.status == "accepted"
    assert acquisition.calls == [
        (first_chat, "first", 1),
        (second_chat, "second", 1),
    ]
