from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from meridian.lib.core.types import TransportId
from meridian.lib.harness.bundle import get_launch_ports
from meridian.lib.harness.connections.base import (
    RawHarnessEvent,
    reap_on_ownership_transfer_failure,
)
from meridian.lib.harness.errors import HarnessBinaryNotFound
from meridian.lib.harness.permission_broker import PermissionBroker
from meridian.lib.launch.launch_types import ResolvedLaunchSpec

if TYPE_CHECKING:
    from meridian.lib.harness.connections.base import ConnectionConfig, HarnessConnection


async def dispatch_start(
    config: ConnectionConfig,
    spec: ResolvedLaunchSpec,
) -> HarnessConnection[Any]:

    from meridian.lib.harness.connections import get_connection_class

    launch_ports = get_launch_ports(config.harness_id)
    if not isinstance(spec, launch_ports.spec_cls):
        raise TypeError(
            f"LaunchPorts invariant violated: adapter for "
            f"{launch_ports.harness_id} returned {type(spec).__name__}, "
            f"expected {launch_ports.spec_cls.__name__}"
        )

    declared_transports = launch_ports.adapter.contract.transport_ids
    transport_id = select_dispatch_transport(declared_transports)
    connection_class = get_connection_class(config.harness_id, transport_id)
    request_handler: PermissionBroker | None = None
    connection_ref: dict[str, HarnessConnection[Any]] = {}

    async def _runtime_event_sink(event: RawHarnessEvent) -> None:
        await connection_ref["connection"].inject_runtime_event(event)

    if launch_ports.adapter.contract.sends_server_requests:
        request_handler = PermissionBroker(
            launch=config.launch,
            event_sink=_runtime_event_sink,
            auto_reject_runtime_requests=True,
            harness_id=config.harness_id.value,
        )

    connection: HarnessConnection[Any]
    if request_handler is not None:
        connection_factory = cast("Callable[..., HarnessConnection[Any]]", connection_class)
        connection = connection_factory(request_handler=request_handler)
    else:
        connection = cast("Callable[[], HarnessConnection[Any]]", connection_class)()

    connection_ref["connection"] = connection

    try:
        await connection.start(config, spec)
    except (FileNotFoundError, NotADirectoryError) as exc:
        await reap_on_ownership_transfer_failure(connection.stop)
        raise HarnessBinaryNotFound.from_os_error(
            harness_id=config.harness_id,
            error=exc,
        ) from exc
    except BaseException:
        await reap_on_ownership_transfer_failure(connection.stop)
        raise
    return connection


def select_dispatch_transport(
    declared_transports: tuple[TransportId, ...],
) -> TransportId:

    if len(declared_transports) == 1:
        return declared_transports[0]
    if TransportId.STREAMING in declared_transports:
        return TransportId.STREAMING
    return declared_transports[0]
