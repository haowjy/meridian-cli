from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Generic, Protocol, TypeVar, cast

from meridian.lib.core.delivery import MessageRoute
from meridian.lib.core.types import HarnessId, TransportId
from meridian.lib.harness.adapter import BootstrapMode, HarnessAdapter, HarnessContract
from meridian.lib.harness.connections.base import HarnessConnection, RawHarnessEvent
from meridian.lib.harness.extractors.base import HarnessExtractor
from meridian.lib.harness.idle_types import (
    DetectIdleTtl,
    IdleSensor,
    IdleSensorContext,
    NativeIdleHook,
)
from meridian.lib.harness.launch_types import ManagedPrimaryPreview
from meridian.lib.harness.plugin_hosted import is_plugin_hosted
from meridian.lib.harness.semantics import HarnessSemantics
from meridian.lib.launch.launch_types import ResolvedLaunchSpec, SpecT
from meridian.lib.safety.policy import (
    UNENFORCED_POLICY,
    EffectivePolicy,
    EnforcementReport,
    PermissionPolicy,
)
from meridian.lib.state.paths import LaunchPaths

ProjectorSpecT = TypeVar("ProjectorSpecT", bound=ResolvedLaunchSpec, contravariant=True)
BootstrapPayloadT = TypeVar("BootstrapPayloadT", covariant=True)


class SubprocessProjector(Protocol[ProjectorSpecT]):
    """Project a resolved launch spec onto one subprocess argv."""

    def __call__(self, spec: ProjectorSpecT, *, base_command: tuple[str, ...]) -> list[str]: ...


class EffectivePolicyProjector(Protocol):
    """Project Meridian-owned policy inputs onto what one harness actually enforces."""

    def __call__(
        self,
        policy: PermissionPolicy,
        *,
        native_config: Mapping[str, object],
    ) -> EffectivePolicy: ...


class ManagedPrimaryBackendProjector(Protocol[ProjectorSpecT]):
    """Project one managed-primary observer backend command."""

    def __call__(self, spec: ProjectorSpecT, *, host: str, port: int) -> list[str]: ...


class ManagedPrimaryBootstrapProjector(Protocol[ProjectorSpecT, BootstrapPayloadT]):
    """Project one managed-primary bootstrap payload."""

    def __call__(self, spec: ProjectorSpecT, *, project_root: Path) -> BootstrapPayloadT: ...


class ManagedPrimaryPreviewProjector(Protocol[ProjectorSpecT]):
    def __call__(
        self,
        spec: ProjectorSpecT,
        *,
        project_root: Path,
    ) -> ManagedPrimaryPreview: ...


@dataclass(frozen=True)
class ManagedPrimaryProjectionPorts(Generic[SpecT, BootstrapPayloadT]):
    """Projection helpers for observer/controller-backed primary launches."""

    backend_command: ManagedPrimaryBackendProjector[SpecT]
    bootstrap_payload: ManagedPrimaryBootstrapProjector[SpecT, BootstrapPayloadT]
    preview: ManagedPrimaryPreviewProjector[SpecT] | None = None


@dataclass(frozen=True)
class HarnessProjectionPorts(Generic[SpecT]):
    """Projection helpers registered for one harness bundle."""

    subprocess_cli_args: SubprocessProjector[SpecT]
    # What the harness enforces from the spec's permission policy: its compile's report.
    # ``--dry-run`` and the projection manifest read it; the live projectors call the typed
    # compile (``permissions_<harness>.py``) directly for its native projection.
    permission_report: Callable[[SpecT], EnforcementReport]
    # What the harness actually enforces, shared by network reports and the child-spawn
    # guardrail. None means it confines neither shell nor network: ``UNENFORCED_POLICY``.
    effective_policy: EffectivePolicyProjector | None = None
    # Resolves the spec's facts that depend on the launch environment or child cwd once at
    # bind, so every projection and the connection read one value.
    bind_spec: Callable[[SpecT, Mapping[str, str], Path], SpecT] | None = None
    # Performs adapter-owned writes only on a real launch, after pure bind.
    materialize_spec: Callable[[SpecT, Mapping[str, str], Path], SpecT] | None = None
    managed_primary: ManagedPrimaryProjectionPorts[SpecT, object] | None = None


EventSinks = Callable[[LaunchPaths], tuple[Callable[[RawHarnessEvent], None], ...]]
PrimaryIdleSensor = Callable[[IdleSensorContext], IdleSensor | None]


@dataclass(frozen=True)
class PrimaryRouteContext:
    """What the launching process knows when it asks a bundle for a primary's route."""

    launch: LaunchPaths
    pid: int
    # The adapter's ``HarnessPrelaunchState.route_payload``: in-memory, this process only.
    payload: object | None = field(default=None, repr=False)


PrimaryMessageRoute = Callable[[PrimaryRouteContext], MessageRoute | None]
"""Route to the primary harness process, or ``None`` while it has none to offer."""


def _no_event_sinks(launch: LaunchPaths) -> tuple[Callable[[RawHarnessEvent], None], ...]:
    return ()


@dataclass(frozen=True)
class LaunchPorts(Generic[SpecT]):
    """What building one harness's launch needs: adapter, spec, projections, semantics.

    Every harness has these, whether or not Meridian runs it.
    """

    harness_id: HarnessId
    adapter: HarnessAdapter[SpecT]
    spec_cls: type[SpecT]
    projections: HarnessProjectionPorts[SpecT]
    semantics: HarnessSemantics


@dataclass(frozen=True)
class ProcessPorts(Generic[SpecT]):
    """What running one harness's process needs: extractor, connections, idle sensors,
    primary message route and event sinks.

    A harness whose plugin hosts its sessions has none: no runner of Meridian's runs it.
    """

    extractor: HarnessExtractor[SpecT]
    connections: Mapping[TransportId, type[HarnessConnection[SpecT]]]
    event_sinks: EventSinks = _no_event_sinks
    primary_message_route: PrimaryMessageRoute | None = None
    primary_idle_sensor: PrimaryIdleSensor | None = None
    native_idle_hook: NativeIdleHook | None = None
    detect_ttl: DetectIdleTtl | None = None


_LAUNCH: dict[HarnessId, LaunchPorts[Any]] = {}
_PROCESS: dict[HarnessId, ProcessPorts[Any]] = {}


def register_harness(launch: LaunchPorts[Any], process: ProcessPorts[Any] | None = None) -> None:
    """Register one harness; ``process`` is given exactly when its plugin does not host it."""

    # Adapter modules call this at import time. Keep this explicit side effect
    # listed in HARNESS_EXTENSION_TOUCHPOINTS for new harness onboarding.
    harness_id = launch.harness_id
    if harness_id in _LAUNCH:
        existing = type(_LAUNCH[harness_id].adapter).__name__
        incoming = type(launch.adapter).__name__
        raise ValueError(
            f"duplicate harness registration for {harness_id}: "
            f"existing adapter={existing}, incoming adapter={incoming}"
        )
    hosted = is_plugin_hosted(harness_id.value)
    if hosted == (process is not None):
        raise ValueError(
            f"harness {harness_id} "
            + (
                "is hosted by its plugin and must register no process ports"
                if hosted
                else "must register process ports: Meridian runs it"
            )
        )

    raw_semantics: object = launch.semantics
    if not isinstance(raw_semantics, HarnessSemantics):  # pyright: ignore[reportUnnecessaryIsInstance]  # Runtime registration validates dynamically constructed ports.
        raise TypeError(f"harness {harness_id} is missing a valid HarnessSemantics port")

    _validate_contract_alignment(launch)
    if process is None:
        if launch.adapter.contract.transport_ids:
            raise ValueError(f"harness {harness_id} has no process but declares transports")
        _LAUNCH[harness_id] = launch
        return

    raw_extractor: object = process.extractor
    if not isinstance(raw_extractor, HarnessExtractor):  # pyright: ignore[reportUnnecessaryIsInstance]  # Runtime registration validates dynamically constructed ports.
        raise TypeError(
            f"harness {harness_id} has invalid extractor "
            f"{type(raw_extractor).__name__}; expected HarnessExtractor"
        )
    if not process.connections:
        raise ValueError(
            f"harness {harness_id} has no connections; "
            "must declare at least one transport connection"
        )
    validated_connections: dict[TransportId, type[HarnessConnection[Any]]] = {}
    raw_connections = dict(cast("Mapping[object, object]", process.connections))
    for raw_transport_id, raw_connection_cls in raw_connections.items():
        if not isinstance(raw_transport_id, TransportId):
            raise ValueError(
                f"harness {harness_id} declares unsupported transport key {raw_transport_id!r}"
            )
        if not isinstance(raw_connection_cls, type) or not issubclass(
            raw_connection_cls,
            HarnessConnection,
        ):
            raise TypeError(
                f"harness {harness_id} transport {raw_transport_id} "
                f"uses invalid connection class {raw_connection_cls!r}"
            )
        validated_connections[raw_transport_id] = cast(
            "type[HarnessConnection[Any]]",
            raw_connection_cls,
        )
    declared_transport_ids = frozenset(launch.adapter.contract.transport_ids)
    actual_transport_ids = frozenset(validated_connections)
    if declared_transport_ids != actual_transport_ids:
        raise ValueError(
            f"harness {harness_id} declares transport contract "
            f"{sorted(t.value for t in declared_transport_ids)} but registers "
            f"{sorted(t.value for t in actual_transport_ids)}"
        )
    _LAUNCH[harness_id] = launch
    _PROCESS[harness_id] = replace(
        process, connections=MappingProxyType(dict(validated_connections))
    )


def _validate_contract_alignment(bundle: LaunchPorts[Any]) -> None:
    contract: HarnessContract = bundle.adapter.contract

    if contract.launch_spec_cls != bundle.spec_cls.__name__:
        raise ValueError(
            f"harness {bundle.harness_id} declares launch_spec_cls "
            f"{contract.launch_spec_cls!r} but registers spec_cls "
            f"{bundle.spec_cls.__name__!r}"
        )

    has_managed_primary_ports = bundle.projections.managed_primary is not None
    if contract.bootstrap_mode is BootstrapMode.MANAGED_PRIMARY_ATTACH:
        if not has_managed_primary_ports:
            raise ValueError(
                f"harness {bundle.harness_id} requires managed-primary "
                "projection ports but none were registered"
            )
        return

    if has_managed_primary_ports:
        raise ValueError(
            f"harness {bundle.harness_id} registered managed-primary "
            "projection ports but bootstrap mode is not managed_primary_attach"
        )


def get_launch_ports(harness_id: HarnessId) -> LaunchPorts[Any]:

    from meridian.lib.harness import ensure_bootstrap

    ensure_bootstrap()
    try:
        return _LAUNCH[harness_id]
    except KeyError as exc:
        raise KeyError(f"unknown harness: {harness_id}") from exc


def get_process_ports(harness_id: HarnessId) -> ProcessPorts[Any]:
    """The process ports of a harness Meridian runs; ``KeyError`` for any other."""

    from meridian.lib.harness import ensure_bootstrap

    ensure_bootstrap()
    try:
        return _PROCESS[harness_id]
    except KeyError as exc:
        raise KeyError(f"harness {harness_id} has no process of Meridian's") from exc


def registered_launch_ports() -> Mapping[HarnessId, LaunchPorts[Any]]:
    """Return what is registered so far, without triggering the bootstrap."""

    return MappingProxyType(_LAUNCH)


def get_launch_registry() -> Mapping[HarnessId, LaunchPorts[Any]]:

    from meridian.lib.harness import ensure_bootstrap

    ensure_bootstrap()
    return registered_launch_ports()


def get_connection_cls(
    harness_id: HarnessId,
    transport_id: TransportId,
) -> type[HarnessConnection[Any]]:

    ports = get_process_ports(harness_id)
    try:
        return ports.connections[transport_id]
    except KeyError as exc:
        raise KeyError(
            f"harness {harness_id} has no connection for transport {transport_id}"
        ) from exc


def _require_launch_spec(
    harness_id: HarnessId,
    spec: object,
) -> LaunchPorts[Any]:
    bundle = get_launch_ports(harness_id)
    if not isinstance(spec, bundle.spec_cls):
        raise TypeError(
            f"harness {harness_id} expected {bundle.spec_cls.__name__}, got {type(spec).__name__}"
        )
    # Validate harness discriminator: catch specs built for another harness.
    # Use getattr to avoid accessing .harness on `object` (pyright can't narrow via hasattr).
    spec_harness = getattr(spec, "harness", None)
    if spec_harness is not None and spec_harness != harness_id:
        raise ValueError(
            f"spec has harness={spec_harness!r} but was passed to harness {harness_id!r} bundle"
        )
    return bundle


def bind_launch_spec(
    harness_id: HarnessId,
    spec: SpecT,
    *,
    env: Mapping[str, str],
    child_cwd: Path,
) -> SpecT:

    bundle = _require_launch_spec(harness_id, spec)
    if bundle.projections.bind_spec is None:
        return spec
    return cast("SpecT", bundle.projections.bind_spec(spec, env, child_cwd))


def project_effective_policy(
    harness_id: HarnessId,
    policy: PermissionPolicy,
    *,
    native_config: Mapping[str, object],
) -> EffectivePolicy:
    """Project the sandbox and network a launch really gets, from inputs Meridian owns."""

    projector = get_launch_ports(harness_id).projections.effective_policy
    if projector is None:
        return UNENFORCED_POLICY
    return projector(policy, native_config=native_config)


def materialize_launch_spec(
    harness_id: HarnessId,
    spec: SpecT,
    *,
    env: Mapping[str, str],
    child_cwd: Path,
) -> SpecT:

    bundle = _require_launch_spec(harness_id, spec)
    if bundle.projections.materialize_spec is None:
        return spec
    return cast("SpecT", bundle.projections.materialize_spec(spec, env, child_cwd))


def project_subprocess_spec(
    harness_id: HarnessId,
    spec: object,
    *,
    base_command: tuple[str, ...],
) -> list[str]:

    bundle = _require_launch_spec(harness_id, spec)
    return bundle.projections.subprocess_cli_args(
        cast("Any", spec),
        base_command=base_command,
    )


def project_managed_primary_backend_command(
    harness_id: HarnessId,
    spec: object,
    *,
    host: str,
    port: int,
) -> list[str]:

    bundle = _require_launch_spec(harness_id, spec)
    managed_primary = bundle.projections.managed_primary
    if managed_primary is None:
        raise ValueError(f"harness {harness_id} does not declare managed-primary ports")
    return managed_primary.backend_command(cast("Any", spec), host=host, port=port)


def project_managed_primary_bootstrap(
    harness_id: HarnessId,
    spec: object,
    *,
    project_root: Path,
) -> object:

    bundle = _require_launch_spec(harness_id, spec)
    managed_primary = bundle.projections.managed_primary
    if managed_primary is None:
        raise ValueError(f"harness {harness_id} does not declare managed-primary ports")
    return managed_primary.bootstrap_payload(
        cast("Any", spec),
        project_root=project_root,
    )


def project_managed_primary_preview(
    harness_id: HarnessId,
    spec: object,
    *,
    project_root: Path,
) -> ManagedPrimaryPreview | None:
    bundle = _require_launch_spec(harness_id, spec)
    managed = bundle.projections.managed_primary
    if managed is None or managed.preview is None:
        return None
    return managed.preview(cast("Any", spec), project_root=project_root)


__all__ = [
    "HarnessProjectionPorts",
    "LaunchPorts",
    "ManagedPrimaryProjectionPorts",
    "PrimaryMessageRoute",
    "ProcessPorts",
    "bind_launch_spec",
    "get_connection_cls",
    "get_launch_ports",
    "get_launch_registry",
    "get_process_ports",
    "materialize_launch_spec",
    "project_effective_policy",
    "project_managed_primary_backend_command",
    "project_managed_primary_bootstrap",
    "project_subprocess_spec",
    "register_harness",
    "registered_launch_ports",
]
