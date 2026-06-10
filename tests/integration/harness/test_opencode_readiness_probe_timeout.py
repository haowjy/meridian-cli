"""Regression: OpenCode readiness probes must be bounded per-attempt.

`opencode serve` accepts the TCP connection before its HTTP handler is ready, so
the first GET after the socket starts accepting can hang. The readiness loop must
bound each probe (not the whole remaining budget) so a hung probe is abandoned and
retried — otherwise one stuck request consumes the entire startup budget and the
managed backend falls back to the black-box TUI / fails the spawn.

These tests stand up a real local TCP server that mimics that startup behavior and
drive the real aiohttp request path through ``_wait_for_ready``.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from meridian.lib.harness.connections.opencode_http import OpenCodeConnection


class _FakeProcess:
    def __init__(self) -> None:
        self.pid = 4242
        self.returncode: int | None = None

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class _ProbeServer:
    """Accepts connections; the first ``hang_first`` of them accept then hang
    (never respond), later ones return ``200`` health JSON."""

    def __init__(self, hang_first: int) -> None:
        self._hang_first = hang_first
        self.connections = 0
        self._release = asyncio.Event()
        self._server: asyncio.AbstractServer | None = None
        self.host = "127.0.0.1"
        self.port = 0

    async def __aenter__(self) -> _ProbeServer:
        self._server = await asyncio.start_server(self._handle, self.host, 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self._release.set()  # let any hung handlers fall through
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connections += 1
        index = self.connections
        with contextlib.suppress(Exception):
            await asyncio.wait_for(reader.read(4096), timeout=0.5)
        if index <= self._hang_first:
            # Accept the request but never respond until teardown — the client's
            # per-probe timeout must give up on us.
            with contextlib.suppress(Exception):
                await self._release.wait()
            with contextlib.suppress(Exception):
                writer.close()
            return
        body = b'{"healthy":true,"version":"test"}'
        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n\r\n" + body
        )
        writer.write(response)
        with contextlib.suppress(Exception):
            await writer.drain()
        with contextlib.suppress(Exception):
            writer.close()


def _connection_for(base_url: str) -> OpenCodeConnection:
    connection = OpenCodeConnection()
    connection._base_url = base_url
    connection._process = _FakeProcess()
    return connection


@pytest.mark.asyncio
async def test_wait_for_ready_recovers_when_first_probe_hangs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(OpenCodeConnection, "_PROBE_TIMEOUT_SECONDS", 0.3)

    async with _ProbeServer(hang_first=1) as server:
        connection = _connection_for(server.base_url)
        try:
            await asyncio.wait_for(
                connection._wait_for_ready(timeout_seconds=5.0), timeout=10.0
            )
            became_ready = connection._last_health_ok
        finally:
            await connection._cleanup_runtime()

        assert became_ready is True
        # Proves the hung first probe was abandoned and retried rather than
        # consuming the whole budget.
        assert server.connections >= 2


@pytest.mark.asyncio
async def test_wait_for_ready_times_out_within_budget_when_every_probe_hangs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(OpenCodeConnection, "_PROBE_TIMEOUT_SECONDS", 0.3)

    async with _ProbeServer(hang_first=10_000) as server:
        connection = _connection_for(server.base_url)
        try:
            with pytest.raises(
                TimeoutError, match="readiness endpoint did not become ready"
            ):
                await asyncio.wait_for(
                    connection._wait_for_ready(timeout_seconds=1.0), timeout=8.0
                )
        finally:
            await connection._cleanup_runtime()

        # Bounded per-attempt: several probes fit inside the 1s budget instead of
        # one probe blocking the entire time.
        assert server.connections >= 2
