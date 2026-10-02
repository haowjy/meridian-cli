"""Pi NDJSON framing and a lossless, byte-bounded temporary event inbox.

The inbox is a circular file, not authoritative history. Consumed bytes are
reused, so its disk bound applies to a long session as well as a stalled drain.
Reception must never wait for the event consumer to make room: on overflow it
fails explicitly, allowing pending RPC requests to close rather than deadlock.
"""

from __future__ import annotations

import asyncio
import json
import struct
import tempfile
from collections.abc import AsyncGenerator
from typing import Final, cast

from meridian.lib.harness.connections.base import RawHarnessEvent

PI_RPC_READ_CHUNK_BYTES: Final = 64 * 1024
PI_RPC_MAX_FRAME_BYTES: Final = 64 * 1024 * 1024
PI_RPC_MAX_BACKLOG_BYTES: Final = 128 * 1024 * 1024
_LENGTH = struct.Struct("!Q")


class PiRpcBufferError(RuntimeError):
    """A deliberate frame/backlog refusal with the observed byte counts."""


async def pi_rpc_frames(
    stream: asyncio.StreamReader,
    *,
    max_bytes: int = PI_RPC_MAX_FRAME_BYTES,
) -> AsyncGenerator[bytes, None]:
    """Frame by LF across bounded reads, accepting a final complete JSON row."""
    frame = bytearray()
    while chunk := await stream.read(PI_RPC_READ_CHUNK_BYTES):
        start = 0
        while start < len(chunk):
            newline = chunk.find(b"\n", start)
            end = len(chunk) if newline < 0 else newline
            observed = len(frame) + end - start
            if observed > max_bytes:
                raise PiRpcBufferError(
                    f"pi_rpc_frame_too_large: observed_bytes>={observed}, limit_bytes={max_bytes}"
                )
            frame.extend(chunk[start:end])
            if newline < 0:
                break
            yield bytes(frame)
            frame.clear()
            start = newline + 1
    if frame:
        yield bytes(frame)


class PiRpcInbox:
    """One producer/consumer inbox; file operations contain no await boundary."""

    def __init__(self, *, max_bytes: int = PI_RPC_MAX_BACKLOG_BYTES) -> None:
        if max_bytes <= _LENGTH.size:
            raise ValueError("Pi RPC inbox byte limit must fit a length prefix")
        self._capacity = max_bytes
        # The connection closes the file after reception and event consumption.
        self._file = tempfile.TemporaryFile(mode="w+b")  # noqa: SIM115
        self._write_offset = 0
        self._read_offset = 0
        self._pending_bytes = 0
        self._ready = asyncio.Event()
        self._finished = False
        self._terminal: RawHarnessEvent | None = None

    def put(self, event: RawHarnessEvent) -> None:
        if self._finished:
            raise RuntimeError("Pi RPC inbox is closed for writes")
        body = json.dumps(
            {"event_type": event.event_type, "payload": event.payload},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        size = _LENGTH.size + len(body)
        if self._pending_bytes + size > self._capacity:
            raise PiRpcBufferError(
                f"pi_rpc_event_backlog_full: pending_bytes={self._pending_bytes}, "
                f"incoming_bytes={size}, limit_bytes={self._capacity}"
            )
        self._write(_LENGTH.pack(len(body)))
        self._write(body)
        self._pending_bytes += size
        self._ready.set()

    async def get(self) -> RawHarnessEvent | None:
        while not self._pending_bytes and not self._finished:
            self._ready.clear()
            await self._ready.wait()
        if self._file.closed:
            return None
        if self._pending_bytes:
            size = _LENGTH.unpack(self._read(_LENGTH.size))[0]
            payload = cast("dict[str, object]", json.loads(self._read(size)))
            self._pending_bytes -= _LENGTH.size + size
            return RawHarnessEvent(
                event_type=str(payload["event_type"]),
                payload=cast("dict[str, object]", payload["payload"]),
                harness_id="pi",
            )
        terminal, self._terminal = self._terminal, None
        return terminal

    def finish(self, terminal: RawHarnessEvent | None = None) -> None:
        if not self._finished:
            self._finished = True
            self._terminal = terminal
        self._ready.set()

    def close(self) -> None:
        self.finish()
        self._file.close()

    def _write(self, data: bytes) -> None:
        split = min(len(data), self._capacity - self._write_offset)
        self._file.seek(self._write_offset)
        self._file.write(data[:split])
        if split < len(data):
            self._file.seek(0)
            self._file.write(data[split:])
        self._write_offset = (self._write_offset + len(data)) % self._capacity

    def _read(self, size: int) -> bytes:
        split = min(size, self._capacity - self._read_offset)
        self._file.seek(self._read_offset)
        data = self._file.read(split)
        if split < size:
            self._file.seek(0)
            data += self._file.read(size - split)
        self._read_offset = (self._read_offset + size) % self._capacity
        return data
