"""Serve a primary's control socket for the life of its harness process."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager

from meridian.lib.core.delivery import MessageRoute
from meridian.lib.harness.bundle import PrimaryMessageRoute
from meridian.lib.harness.control_action import ControlActionCoordinator
from meridian.lib.state.paths import LaunchPaths
from meridian.lib.streaming.control_socket import ControlSocketServer, control_socket_path

logger = logging.getLogger(__name__)

_ROUTE_POLL_SECONDS = 0.25
_JOIN_SECONDS = 10.0


def _serve_when_routable(
    launch: LaunchPaths, factory: PrimaryMessageRoute, pid: int, stop: threading.Event
) -> None:
    route: MessageRoute | None = None
    while not stop.is_set():
        route = factory(launch, pid)
        if route is not None:
            break
        stop.wait(_ROUTE_POLL_SECONDS)
    if route is not None:
        asyncio.run(_serve(launch, route, stop))


async def _serve(launch: LaunchPaths, route: MessageRoute, stop: threading.Event) -> None:
    coordinator = ControlActionCoordinator(launch=launch)
    server = ControlSocketServer(
        control_socket_path(launch.runtime_root, launch.spawn_id),
        MessageRoute(
            lambda text: coordinator.deliver(route.deliver, text, source="control_socket"),
            route.note,
        ),
    )
    await server.start()
    try:
        await asyncio.to_thread(stop.wait)
    finally:
        await server.stop()


@contextmanager
def primary_control_socket(
    launch: LaunchPaths, factory: PrimaryMessageRoute | None
) -> Generator[Callable[[int], None], None, None]:
    """Own the primary's control socket lexically; yield ``serve(pid)`` for on-running.

    The socket is bound only once *factory* returns a route, so a socket file always
    means a message can be delivered. Leaving the block unbinds it and joins the thread.
    ``serve`` never raises: a failure in on-running would kill the child.
    """

    stop = threading.Event()
    thread: threading.Thread | None = None

    def run(pid: int) -> None:
        assert factory is not None
        try:
            _serve_when_routable(launch, factory, pid, stop)
        except Exception:
            logger.warning("Primary control socket for %s failed", launch.spawn_id, exc_info=True)

    def serve(pid: int) -> None:
        nonlocal thread
        if factory is None or thread is not None:
            return
        try:
            thread = threading.Thread(
                target=run,
                args=(pid,),
                name=f"meridian-primary-control-{launch.spawn_id}",
                daemon=True,
            )
            thread.start()
        except Exception:
            logger.warning(
                "Primary control socket for %s not started", launch.spawn_id, exc_info=True
            )

    try:
        yield serve
    finally:
        stop.set()
        if thread is not None:
            thread.join(_JOIN_SECONDS)
