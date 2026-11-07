from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncGenerator, Callable, Generator
from contextlib import asynccontextmanager, contextmanager

from meridian.lib.core.delivery import MessageRoute
from meridian.lib.harness.bundle import PrimaryMessageRoute
from meridian.lib.harness.control_action import ControlActionCoordinator
from meridian.lib.state.paths import LaunchPaths
from meridian.lib.streaming.control_socket import ControlSocketServer, control_socket_path

logger = logging.getLogger(__name__)

_ROUTE_POLL_SECONDS = 0.25
_JOIN_SECONDS = 10.0


@asynccontextmanager
async def serving_route(launch: LaunchPaths, route: MessageRoute) -> AsyncGenerator[None]:
    """Bind the spawn's control socket to *route*, journaled, for the block's duration."""

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
        yield
    finally:
        await server.stop()


def _serve_when_routable(
    launch: LaunchPaths, factory: PrimaryMessageRoute, pid: int, stop: threading.Event
) -> None:
    """Thread body: wait for a route, then serve it until *stop*. Logs, never raises."""

    async def serve_until_stopped(route: MessageRoute) -> None:
        async with serving_route(launch, route):
            await asyncio.to_thread(stop.wait)

    try:
        route: MessageRoute | None = None
        while not stop.is_set():
            route = factory(launch, pid)
            if route is not None:
                break
            stop.wait(_ROUTE_POLL_SECONDS)
        if route is not None:
            asyncio.run(serve_until_stopped(route))
    except Exception:
        logger.warning("Primary control socket for %s failed", launch.spawn_id, exc_info=True)


@contextmanager
def primary_control_socket(
    launch: LaunchPaths, factory: PrimaryMessageRoute | None
) -> Generator[Callable[[int], None], None, None]:

    stop = threading.Event()
    thread: threading.Thread | None = None

    def serve(pid: int) -> None:
        nonlocal thread
        if factory is None:
            return
        # A launch callback failure would kill the child.
        try:
            thread = threading.Thread(
                target=_serve_when_routable,
                args=(launch, factory, pid, stop),
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
