"""Typed harness bundle registry for dispatch and extraction."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Generic, Protocol, TypeVar, cast

from meridian.lib.core.types import HarnessId, TransportId
from meridian.lib.harness.adapter import BootstrapMode, HarnessAdapter, HarnessContract
from meridian.lib.harness.connections.base import HarnessConnection
from meridian.lib.harness.extractors.base import HarnessExtractor
from meridian.lib.harness.semantics import HarnessSemantics
from meridian.lib.launch.launch_types import ResolvedLaunchSpec, SpecT

ProjectorSpecT = TypeVar("ProjectorSpecT", bound=ResolvedLaunchSpec, contravariant=True)
BootstrapPayloadT = TypeVar("BootstrapPayloadT", covariant=True)


class SubprocessProjector(Protocol[ProjectorSpecT]):
    """Project a resolved launch spec onto one subprocess argv."""

    def __call__(self, spec: ProjectorSpecT, *, base_command: tuple[str, ...]) -> list[str]: ...


class ManagedPrimaryBackendProjector(Protocol[ProjectorSpecT]):
    """Project one managed-primary observer backend command."""

    def __call__(self, spec: ProjectorSpecT, *, host: str, port: int) -> list[str]: ...


class ManagedPrimaryBootstrapProjector(Protocol[ProjectorSpecT, BootstrapPayloadT]):
    """Project one managed-primary bootstrap payload."""

    def __call__(self, spec: ProjectorSpecT, *, project_root: Path) -> BootstrapPayloadT: ...


@dataclass(frozen=True)
class ManagedPrimaryProjectionPorts(Generic[SpecT, BootstrapPayloadT]):
    """Projection helpers for observer/controller-backed primary launches."""

    backend_command: ManagedPrimaryBackendProjector[SpecT]
    bootstrap_payload: ManagedPrimaryBootstrapProjector[SpecT, BootstrapPayloadT]


@dataclass(frozen=True)
class HarnessProjectionPorts(Generic[SpecT]):
    """Projection helpers registered for one harness bundle."""

    subprocess_cli_args: SubprocessProjector[SpecT]
    managed_primary: ManagedPrimaryProjectionPorts[SpecT, object] | None = None


@dataclass(frozen=True)
class HarnessBundle(Generic[SpecT]):
    """Bundle binding one harness adapter to spec, extractor, transports, and projections."""

    harness_id: HarnessId
    adapter: HarnessAdapter[SpecT]
    spec_cls: type[SpecT]
    extractor: HarnessExtractor[SpecT]
    connections: Mapping[TransportId, type[HarnessConnection[SpecT]]]
    projections: HarnessProjectionPorts[SpecT]
    semantics: HarnessSemantics


_REGISTRY: dict[HarnessId, HarnessBundle[Any]] = {}


def register_harness_bundle(bundle: HarnessBundle[Any]) -> None:
    """Register one harness bundle and enforce bootstrap invariants."""
    # Adapter modules call this at import time. Keep this explicit side effect
    # listed in HARNESS_EXTENSION_TOUCHPOINTS for new harness onboarding.

    raw_extractor: object = bundle.extractor
    if raw_extractor is None:  # pyright: ignore[reportUnnecessaryComparison]
        raise TypeError(
            f"HarnessBundle for {bundle.harness_id} is missing extractor "
            "(every harness must declare a HarnessExtractor)"
        )

    if not isinstance(raw_extractor, HarnessExtractor):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise TypeError(
            f"HarnessBundle for {bundle.harness_id} has invalid extractor "
            f"{type(raw_extractor).__name__}; expected HarnessExtractor"
        )

    raw_semantics: object = bundle.semantics
    if not isinstance(raw_semantics, HarnessSemantics):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise TypeError(
            f"HarnessBundle for {bundle.harness_id} is missing a valid HarnessSemantics port"
        )

    if not bundle.connections:
        raise ValueError(
            f"HarnessBundle for {bundle.harness_id} has no connections; "
            "must declare at least one transport connection"
        )

    validated_connections: dict[TransportId, type[HarnessConnection[Any]]] = {}
    raw_connections = dict(cast("Mapping[object, object]", bundle.connections))
    for raw_transport_id, raw_connection_cls in raw_connections.items():
        if not isinstance(raw_transport_id, TransportId):
            raise ValueError(
                f"HarnessBundle for {bundle.harness_id} declares unsupported "
                f"transport key {raw_transport_id!r}"
            )
        if not isinstance(raw_connection_cls, type) or not issubclass(
            raw_connection_cls,
            HarnessConnection,
        ):
            raise TypeError(
                f"HarnessBundle for {bundle.harness_id} transport {raw_transport_id} "
                f"uses invalid connection class {raw_connection_cls!r}"
            )
        validated_connections[raw_transport_id] = cast(
            "type[HarnessConnection[Any]]",
            raw_connection_cls,
        )

    if bundle.harness_id in _REGISTRY:
        existing = type(_REGISTRY[bundle.harness_id].adapter).__name__
        incoming = type(bundle.adapter).__name__
        raise ValueError(
            f"duplicate harness bundle for {bundle.harness_id}: "
            f"existing adapter={existing}, incoming adapter={incoming}"
        )

    frozen_connections: Mapping[TransportId, type[HarnessConnection[Any]]] = MappingProxyType(
        dict(validated_connections)
    )
    _validate_contract_alignment(bundle)
    declared_transport_ids = frozenset(bundle.adapter.contract.transport.transport_ids)
    actual_transport_ids = frozenset(frozen_connections)
    if declared_transport_ids != actual_transport_ids:
        raise ValueError(
            f"HarnessBundle for {bundle.harness_id} declares transport contract "
            f"{sorted(t.value for t in declared_transport_ids)} but registers "
            f"{sorted(t.value for t in actual_transport_ids)}"
        )
    _REGISTRY[bundle.harness_id] = HarnessBundle(
        harness_id=bundle.harness_id,
        adapter=bundle.adapter,
        spec_cls=bundle.spec_cls,
        extractor=bundle.extractor,
        connections=frozen_connections,
        projections=bundle.projections,
        semantics=bundle.semantics,
    )


def _validate_contract_alignment(bundle: HarnessBundle[Any]) -> None:
    contract: HarnessContract = bundle.adapter.contract

    if contract.projection.launch_spec_cls != bundle.spec_cls.__name__:
        raise ValueError(
            f"HarnessBundle for {bundle.harness_id} declares projection launch_spec_cls "
            f"{contract.projection.launch_spec_cls!r} but registers spec_cls "
            f"{bundle.spec_cls.__name__!r}"
        )

    has_managed_primary_ports = bundle.projections.managed_primary is not None
    if contract.bootstrap.mode is BootstrapMode.MANAGED_PRIMARY_ATTACH:
        if not has_managed_primary_ports:
            raise ValueError(
                f"HarnessBundle for {bundle.harness_id} requires managed-primary "
                "projection ports but none were registered"
            )
        return

    if has_managed_primary_ports:
        raise ValueError(
            f"HarnessBundle for {bundle.harness_id} registered managed-primary "
            "projection ports but bootstrap mode is not managed_primary_attach"
        )


def get_harness_bundle(harness_id: HarnessId) -> HarnessBundle[Any]:
    """Look up one registered harness bundle."""

    try:
        return _REGISTRY[harness_id]
    except KeyError as exc:
        raise KeyError(f"unknown harness: {harness_id}") from exc


def get_bundle_registry() -> Mapping[HarnessId, HarnessBundle[Any]]:
    """Return the active harness bundle registry mapping."""

    return MappingProxyType(_REGISTRY)


def get_connection_cls(
    harness_id: HarnessId,
    transport_id: TransportId,
) -> type[HarnessConnection[Any]]:
    """Return the connection class for one harness+transport pair."""

    bundle = get_harness_bundle(harness_id)
    try:
        return bundle.connections[transport_id]
    except KeyError as exc:
        raise KeyError(
            f"harness {harness_id} has no connection for transport {transport_id}"
        ) from exc


def _require_bundle_spec(
    harness_id: HarnessId,
    spec: object,
) -> HarnessBundle[Any]:
    bundle = get_harness_bundle(harness_id)
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


def project_subprocess_spec(
    harness_id: HarnessId,
    spec: object,
    *,
    base_command: tuple[str, ...],
) -> list[str]:
    """Project one resolved spec to subprocess argv through the bundle registry."""

    bundle = _require_bundle_spec(harness_id, spec)
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
    """Project one managed-primary backend command through the bundle registry."""

    bundle = _require_bundle_spec(harness_id, spec)
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
    """Project one managed-primary bootstrap payload through the bundle registry."""

    bundle = _require_bundle_spec(harness_id, spec)
    managed_primary = bundle.projections.managed_primary
    if managed_primary is None:
        raise ValueError(f"harness {harness_id} does not declare managed-primary ports")
    return managed_primary.bootstrap_payload(
        cast("Any", spec),
        project_root=project_root,
    )


__all__ = [
    "HarnessBundle",
    "HarnessProjectionPorts",
    "ManagedPrimaryProjectionPorts",
    "get_bundle_registry",
    "get_connection_cls",
    "get_harness_bundle",
    "project_managed_primary_backend_command",
    "project_managed_primary_bootstrap",
    "project_subprocess_spec",
    "register_harness_bundle",
]
