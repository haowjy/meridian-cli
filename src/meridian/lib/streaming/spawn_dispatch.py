"""Harness connection dispatch for streaming spawns."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from meridian.lib.core.types import HarnessId, TransportId
from meridian.lib.harness.bundle import get_harness_bundle
from meridian.lib.harness.connections.base import (
    RawHarnessEvent,
    reap_on_ownership_transfer_failure,
)
from meridian.lib.harness.errors import HarnessBinaryNotFound
from meridian.lib.harness.permission_broker import PermissionBroker
from meridian.lib.harness.pi_native_source import reject_pi_native_source_options
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.ops.reference import (
    AuthorizedSourceUse,
    SourceUseRefused,
    resolve_source_use,
)
from meridian.lib.state.paths import (
    resolve_project_runtime_root_for_write,
    resolve_spawn_log_dir,
)

if TYPE_CHECKING:
    from meridian.lib.harness.connections.base import ConnectionConfig, HarnessConnection


def _ensure_harness_bootstrap() -> None:
    from meridian.lib.harness import ensure_bootstrap

    ensure_bootstrap()


async def dispatch_start(
    config: ConnectionConfig,
    spec: ResolvedLaunchSpec,
) -> HarnessConnection[Any]:
    """Dispatch one untracked start after refusing unsupported native claims."""

    _refuse_unowned_tracked_selection(config, spec)

    from meridian.lib.harness.connections import get_connection_class

    _ensure_harness_bootstrap()
    bundle = get_harness_bundle(config.harness_id)
    if not isinstance(spec, bundle.spec_cls):
        raise TypeError(
            f"HarnessBundle invariant violated: adapter for "
            f"{bundle.harness_id} returned {type(spec).__name__}, "
            f"expected {bundle.spec_cls.__name__}"
        )

    declared_transports = bundle.adapter.contract.transport.transport_ids
    transport_id = select_dispatch_transport(declared_transports)
    connection_class = get_connection_class(config.harness_id, transport_id)
    request_handler: PermissionBroker | None = None
    connection_ref: dict[str, HarnessConnection[Any]] = {}

    async def _runtime_event_sink(event: RawHarnessEvent) -> None:
        await connection_ref["connection"].inject_runtime_event(event)

    if config.harness_id in (HarnessId.CODEX, HarnessId.OPENCODE):
        runtime_root = config.runtime_root or resolve_project_runtime_root_for_write(
            config.control_root
        )
        request_handler = PermissionBroker(
            spawn_dir=resolve_spawn_log_dir(
                config.control_root, config.spawn_id, runtime_root=runtime_root
            ),
            event_sink=_runtime_event_sink,
            auto_reject_runtime_requests=True,
            harness_id=config.harness_id.value,
        )

    connection: HarnessConnection[Any]
    if request_handler is not None:
        connection_factory = cast(
            "Callable[..., HarnessConnection[Any]]",
            connection_class,
        )
        try:
            connection = connection_factory(request_handler=request_handler)
        except TypeError:
            connection = cast("Callable[[], HarnessConnection[Any]]", connection_class)()
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


def _refuse_unowned_tracked_selection(
    config: ConnectionConfig,
    spec: ResolvedLaunchSpec,
) -> None:
    """Close direct-spec bypasses before adapter lookup or connection creation."""

    if spec.recorded_native_source is not None:
        raise ValueError(
            "Tracked native startup is blocked pending owned admission "
            "(owner_required); no connection was started."
        )

    selected_id = (spec.continue_session_id or "").strip()
    if selected_id:
        if config.runtime_root is None:
            raise ValueError(
                "Cannot classify native selection without runtime authority; "
                "no connection was started."
            )
        result = resolve_source_use(
            config.runtime_root,
            "fork" if spec.continue_fork else "resume",
            selected_id,
            explicit_harness=str(config.harness_id),
        )
        if isinstance(result, (AuthorizedSourceUse, SourceUseRefused)):
            raise ValueError(
                f"Native selection '{selected_id}' is not eligible for immediate start "
                f"({getattr(result, 'reason', 'owner_required')}); no connection was started."
            )

    if config.harness_id == HarnessId.PI:
        try:
            reject_pi_native_source_options(spec.extra_args)
        except ValueError as exc:
            raise ValueError(
                "Raw Pi native selection cannot be classified at dispatch; "
                "no connection was started."
            ) from exc


def select_dispatch_transport(
    declared_transports: tuple[TransportId, ...],
) -> TransportId:
    """Choose dispatch transport from adapter contract declarations."""

    if len(declared_transports) == 1:
        return declared_transports[0]
    if TransportId.STREAMING in declared_transports:
        return TransportId.STREAMING
    return declared_transports[0]
