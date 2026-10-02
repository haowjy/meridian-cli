"""Pure byte framing and reusable bounded spool invariants."""

from __future__ import annotations

import asyncio
import os

import pytest

from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.harness.connections.pi_rpc_stream import (
    PiRpcBufferError,
    PiRpcInbox,
    pi_rpc_frames,
)


@pytest.mark.asyncio
async def test_frame_boundary_preserves_split_utf8_and_final_row() -> None:
    stream = asyncio.StreamReader()
    frames = pi_rpc_frames(stream, max_bytes=12)
    stream.feed_data(b'{"x":"\xc3')
    pending = asyncio.create_task(anext(frames))
    await asyncio.sleep(0)
    assert not pending.done()
    stream.feed_data(b'\xa9"}\n{}')
    stream.feed_eof()
    assert await pending == '{"x":"é"}'.encode()
    assert await anext(frames) == b"{}"
    with pytest.raises(StopAsyncIteration):
        await anext(frames)


@pytest.mark.asyncio
async def test_frame_limit_refuses_before_unbounded_accumulation() -> None:
    stream = asyncio.StreamReader()
    stream.feed_data(b"x" * 13)
    with pytest.raises(PiRpcBufferError, match=r"frame_too_large.*limit_bytes=12"):
        await anext(pi_rpc_frames(stream, max_bytes=12))


@pytest.mark.asyncio
async def test_spool_reuses_disk_for_lifetime_bytes_and_preserves_fifo_at_wrap() -> None:
    inbox = PiRpcInbox(max_bytes=300)
    try:
        for wave in range(100):
            events = [
                RawHarnessEvent(
                    event_type="message_end", payload={"i": wave * 2 + i}, harness_id="pi"
                )
                for i in range(2)
            ]
            for event in events:
                inbox.put(event)
            for event in events:
                received = await inbox.get()
                assert received is not None and received.payload == event.payload
            assert os.fstat(inbox._file.fileno()).st_size <= 300
        inbox.finish()
        assert await inbox.get() is None
    finally:
        inbox.close()


@pytest.mark.asyncio
async def test_spool_overflow_preserves_backlog_then_terminal_failure() -> None:
    inbox = PiRpcInbox(max_bytes=150)
    event = RawHarnessEvent(event_type="response", payload={"content": "x" * 50}, harness_id="pi")
    terminal = RawHarnessEvent(
        event_type="meridian/error/connectionClosed",
        payload={"message": "overflow"},
        harness_id="pi",
    )
    try:
        inbox.put(event)
        with pytest.raises(PiRpcBufferError, match=r"event_backlog_full.*limit_bytes=150"):
            inbox.put(event)
        inbox.finish(terminal)
        received = await inbox.get()
        assert received is not None and received.payload == event.payload
        assert await inbox.get() == terminal
        assert await inbox.get() is None
    finally:
        inbox.close()


@pytest.mark.asyncio
async def test_spool_close_wakes_pending_consumer_without_reading_closed_file() -> None:
    inbox = PiRpcInbox(max_bytes=150)
    pending = asyncio.create_task(inbox.get())
    await asyncio.sleep(0)
    inbox.close()
    assert await pending is None
    assert await inbox.get() is None
