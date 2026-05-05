import asyncio

import pytest

from meridian.lib.chat.event_log import ChatEventLog
from meridian.lib.chat.event_pipeline import ChatEventPipeline
from meridian.lib.chat.protocol import RUNTIME_WARNING, ChatEvent, utc_now_iso


class Session:
    def __init__(self):
        self.completed = []
        self.died = []

    def on_turn_completed(self, generation=None):
        self.completed.append(generation)

    def on_execution_died(self, generation=None):
        self.died.append(generation)


class Fanout:
    def __init__(self):
        self.events = []

    async def broadcast(self, event):
        self.events.append(event)


class BlockingFanout:
    def __init__(self):
        self.events = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def broadcast(self, event):
        self.events.append(event)
        self.started.set()
        await self.release.wait()


class Index:
    def __init__(self):
        self.events = []

    def upsert(self, event):
        self.events.append(event)


def event(kind="turn.started", *, chat_id="c1", execution_id="e1", gen=None):
    payload = {} if gen is None else {"execution_generation": gen}
    return ChatEvent(kind, 0, chat_id, execution_id, utc_now_iso(), payload=payload)


@pytest.mark.asyncio
async def test_pipeline_persists_before_broadcast_and_callback(tmp_path):
    log_path = tmp_path / "events.jsonl"
    session = Session()
    fanout = Fanout()
    index = Index()
    pipeline = ChatEventPipeline(
        "c1",
        ChatEventLog(log_path),
        session,
        fanout=fanout,
        event_index=index,
    )
    pipeline.start()

    await pipeline.ingest(event("turn.completed", gen=2))
    await asyncio.sleep(0.05)
    await pipeline.stop()

    assert fanout.events[0].seq == 0
    assert index.events[0].seq == 0
    assert session.completed == [2]
    assert [stored.type for stored in ChatEventLog(log_path).read_all()] == [
        "turn.completed"
    ]


@pytest.mark.asyncio
async def test_post_persist_hook_appends_synthetic_event_inline(tmp_path):
    log_path = tmp_path / "events.jsonl"
    session = Session()
    fanout = Fanout()
    index = Index()
    pipeline = ChatEventPipeline(
        "c1",
        ChatEventLog(log_path),
        session,
        fanout=fanout,
        event_index=index,
    )

    def synthesize(persisted):
        assert persisted.seq == 0
        return [
            ChatEvent(
                "extension.synthetic",
                999,
                persisted.chat_id,
                persisted.execution_id,
                utc_now_iso(),
                payload={"cause_seq": persisted.seq},
            )
        ]

    pipeline.add_post_persist_hook(synthesize)
    pipeline.start()

    await pipeline.ingest(event("content.delta"))
    await pipeline.drain()
    await pipeline.stop()

    stored_events = list(ChatEventLog(log_path).read_all())
    assert [stored.type for stored in stored_events] == [
        "content.delta",
        "extension.synthetic",
    ]
    assert [stored.seq for stored in stored_events] == [0, 1]
    assert [event.type for event in index.events] == [
        "content.delta",
        "extension.synthetic",
    ]
    assert [event.type for event in fanout.events] == [
        "content.delta",
        "extension.synthetic",
    ]


@pytest.mark.asyncio
async def test_post_persist_hooks_isolate_failures_and_reject_unsafe_events(tmp_path):
    log_path = tmp_path / "events.jsonl"
    session = Session()
    fanout = Fanout()
    pipeline = ChatEventPipeline(
        "c1",
        ChatEventLog(log_path),
        session,
        fanout=fanout,
    )

    def fail(_persisted):
        raise RuntimeError("boom")

    def synthesize_unsafe_and_valid(persisted):
        return [
            ChatEvent(
                "extension.wrong-chat",
                0,
                "other-chat",
                persisted.execution_id,
                utc_now_iso(),
                payload={},
            ),
            ChatEvent(
                "turn.completed",
                0,
                persisted.chat_id,
                persisted.execution_id,
                utc_now_iso(),
                payload={"execution_generation": 99},
            ),
            ChatEvent(
                "extension.valid",
                0,
                persisted.chat_id,
                persisted.execution_id,
                utc_now_iso(),
                payload={"cause_seq": persisted.seq},
            ),
        ]

    pipeline.add_post_persist_hook(fail)
    pipeline.add_post_persist_hook(synthesize_unsafe_and_valid)
    pipeline.start()

    await pipeline.ingest(event("content.delta"))
    await pipeline.drain()
    await pipeline.stop()

    stored_events = list(ChatEventLog(log_path).read_all())
    assert [stored.type for stored in stored_events] == [
        "content.delta",
        "extension.valid",
    ]
    assert [event.type for event in fanout.events] == [
        "content.delta",
        "extension.valid",
    ]
    assert session.completed == []


@pytest.mark.asyncio
async def test_wrong_chat_event_is_dropped(tmp_path):
    log_path = tmp_path / "events.jsonl"
    session = Session()
    fanout = Fanout()
    pipeline = ChatEventPipeline("c1", ChatEventLog(log_path), session, fanout=fanout)
    pipeline.start()

    await pipeline.ingest(event("content.delta", chat_id="other-chat"))
    await asyncio.sleep(0.05)
    await pipeline.stop()

    assert fanout.events == []
    assert list(ChatEventLog(log_path).read_all()) == []
    assert session.completed == []


@pytest.mark.asyncio
@pytest.mark.xfail(
    reason=(
        "queue-full warning contract not yet met: warning cannot be "
        "enqueued once the queue is already full"
    ),
    strict=True,
)
async def test_queue_full_drops_event_and_emits_runtime_warning(tmp_path):
    log_path = tmp_path / "events.jsonl"
    session = Session()
    pipeline = ChatEventPipeline(
        "c1",
        ChatEventLog(log_path),
        session,
        max_queue=1,
    )

    await pipeline.ingest(event("content.delta"))
    await pipeline.ingest(event("item.started"))
    pipeline.start()
    await asyncio.sleep(0.05)
    await pipeline.stop()

    stored_events = list(ChatEventLog(log_path).read_all())
    assert [stored.type for stored in stored_events] == ["content.delta", RUNTIME_WARNING]
    warning = stored_events[-1]
    assert warning.payload["reason"] == "pipeline_queue_full"
    assert warning.payload["dropped_type"] == "item.started"


@pytest.mark.asyncio
async def test_turn_completed_notifies_session_even_when_queue_full(tmp_path):
    session = Session()
    fanout = BlockingFanout()
    pipeline = ChatEventPipeline(
        "c1",
        ChatEventLog(tmp_path / "events.jsonl"),
        session,
        fanout=fanout,
        max_queue=1,
    )
    pipeline.start()

    await pipeline.ingest(event("content.delta"))
    await fanout.started.wait()
    await pipeline.ingest(event("turn.completed", gen=7))
    fanout.release.set()
    await asyncio.sleep(0.05)
    await pipeline.stop()

    assert session.completed == [7]


@pytest.mark.asyncio
async def test_on_execution_complete_notifies_session(tmp_path):
    session = Session()
    pipeline = ChatEventPipeline(
        "c1",
        ChatEventLog(tmp_path / "events.jsonl"),
        session,
    )

    await pipeline.on_execution_complete(3)

    assert session.died == [3]
