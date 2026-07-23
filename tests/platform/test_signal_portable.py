"""Tests for portable signal handler installation in streaming_runner.py.

Verifies that _install_signal_handlers uses signal.signal() (stdlib, portable)
rather than loop.add_signal_handler() (asyncio, Windows-broken), and that the
cleanup callable pattern correctly restores previous handlers.
"""

from __future__ import annotations

import asyncio
import os
import signal
import threading

import pytest

from meridian.lib.launch.signals import SignalForwarder
from meridian.lib.launch.streaming_runner import (
    _install_signal_handlers,  # pyright: ignore[reportPrivateUsage]
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _invoke_handler(signum: signal.Signals) -> None:
    """Invoke the currently installed signal handler directly, without OS delivery.

    Calling os.kill(os.getpid(), signum) terminates the process on Windows instead
    of firing the Python-level handler.  Capturing and calling the handler directly
    exercises exactly the same code path on every platform.
    """
    handler = signal.getsignal(signum)
    if callable(handler):
        handler(signum, None)


# ---------------------------------------------------------------------------
# Basic installation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_returns_cleanup_callable() -> None:
    """_install_signal_handlers returns a callable from the main thread."""
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()
    received_signal: list[signal.Signals | None] = [None]

    cleanup = _install_signal_handlers(loop, shutdown_event, received_signal)
    assert cleanup is not None
    assert callable(cleanup)
    cleanup()  # restore


@pytest.mark.asyncio
async def test_sigint_sets_shutdown_event() -> None:
    """SIGINT causes the shutdown_event to be set."""
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()
    received_signal: list[signal.Signals | None] = [None]

    cleanup = _install_signal_handlers(loop, shutdown_event, received_signal)
    assert cleanup is not None
    try:
        _invoke_handler(signal.SIGINT)
        # Allow the event loop to process the call_soon_threadsafe callback
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert shutdown_event.is_set()
        assert received_signal[0] == signal.SIGINT
    finally:
        cleanup()


@pytest.mark.asyncio
async def test_sigterm_sets_shutdown_event() -> None:
    """SIGTERM causes the shutdown_event to be set."""
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()
    received_signal: list[signal.Signals | None] = [None]

    cleanup = _install_signal_handlers(loop, shutdown_event, received_signal)
    assert cleanup is not None
    try:
        _invoke_handler(signal.SIGTERM)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert shutdown_event.is_set()
        assert received_signal[0] == signal.SIGTERM
    finally:
        cleanup()


@pytest.mark.asyncio
async def test_cleanup_restores_previous_handlers() -> None:
    """Cleanup callable restores the handlers that were in place before installation."""
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()
    received_signal: list[signal.Signals | None] = [None]

    sentinel_called: list[int] = []

    def _sentinel(signum: int, frame: object) -> None:
        sentinel_called.append(signum)

    # Install sentinel as the current handler, then install our handlers on top
    prev_sigint = signal.signal(signal.SIGINT, _sentinel)
    prev_sigterm = signal.signal(signal.SIGTERM, _sentinel)
    try:
        cleanup = _install_signal_handlers(loop, shutdown_event, received_signal)
        assert cleanup is not None
        # Our handler should be active now, not sentinel
        assert signal.getsignal(signal.SIGINT) is not _sentinel
        assert signal.getsignal(signal.SIGTERM) is not _sentinel

        cleanup()

        # Sentinel should be restored
        assert signal.getsignal(signal.SIGINT) is _sentinel
        assert signal.getsignal(signal.SIGTERM) is _sentinel
    finally:
        # Restore originals regardless
        signal.signal(signal.SIGINT, prev_sigint)
        signal.signal(signal.SIGTERM, prev_sigterm)


def test_returns_none_from_non_main_thread() -> None:
    """_install_signal_handlers returns None when called from a non-main thread."""
    result: list[object] = []

    async def _inner() -> None:
        loop = asyncio.get_running_loop()
        shutdown_event = asyncio.Event()
        received_signal: list[signal.Signals | None] = [None]
        cleanup = _install_signal_handlers(loop, shutdown_event, received_signal)
        result.append(cleanup)

    def _thread_target() -> None:
        asyncio.run(_inner())

    t = threading.Thread(target=_thread_target)
    t.start()
    t.join()

    assert result == [None]


# ---------------------------------------------------------------------------
# Signal layering: streaming handlers + SignalCoordinator/SignalForwarder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_layering_streaming_handlers_restored_after_forwarder_unregisters() -> None:
    """SignalCoordinator overrides handlers while a forwarder is registered;
    when the forwarder unregisters, the streaming handlers come back.
    """
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()
    received_signal: list[signal.Signals | None] = [None]

    # 1. Install streaming handlers first
    cleanup = _install_signal_handlers(loop, shutdown_event, received_signal)
    assert cleanup is not None

    streaming_handler_sigint = signal.getsignal(signal.SIGINT)
    streaming_handler_sigterm = signal.getsignal(signal.SIGTERM)

    # Create a dummy process mock that does nothing
    class _FakeProcess:
        returncode: int | None = None
        pid: int = os.getpid()

        def send_signal(self, sig: signal.Signals) -> None:
            pass

    fake_proc = _FakeProcess()
    # Satisfy the subprocess.Process type with duck-typing
    forwarder = SignalForwarder(fake_proc)  # type: ignore[arg-type]

    try:
        with forwarder:
            # While forwarder is registered, SignalCoordinator owns the handlers
            active_sigint = signal.getsignal(signal.SIGINT)
            active_sigterm = signal.getsignal(signal.SIGTERM)
            # Coordinator installs its own handler (not the streaming one)
            assert active_sigint is not streaming_handler_sigint
            assert active_sigterm is not streaming_handler_sigterm

        # After forwarder unregisters, coordinator restores previous (streaming) handlers
        restored_sigint = signal.getsignal(signal.SIGINT)
        restored_sigterm = signal.getsignal(signal.SIGTERM)
        assert restored_sigint is streaming_handler_sigint
        assert restored_sigterm is streaming_handler_sigterm

        # Now invoke SIGINT — the streaming handler should fire
        _invoke_handler(signal.SIGINT)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert shutdown_event.is_set()
        assert received_signal[0] == signal.SIGINT
    finally:
        cleanup()
