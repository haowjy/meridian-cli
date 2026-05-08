"""Typed harness bundle registry for dispatch and extraction."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Generic, cast

from meridian.lib.harness.adapter import HarnessAdapter
from meridian.lib.harness.connections.base import HarnessConnection
from meridian.lib.harness.extractors.base import HarnessExtractor
from meridian.lib.harness.ids import HarnessId, TransportId
from meridian.lib.launch.launch_types import SpecT


@dataclass(frozen=True)
class HarnessBundle(Generic[SpecT]):
    """Bundle binding one harness adapter to spec, extractor, and transports."""

    harness_id: HarnessId
    adapter: HarnessAdapter[SpecT]
    spec_cls: type[SpecT]
    extractor: HarnessExtractor[SpecT]
    connections: Mapping[TransportId, type[HarnessConnection[SpecT]]]


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


__all__ = [
    "HarnessBundle",
    "get_bundle_registry",
    "get_connection_cls",
    "get_harness_bundle",
    "register_harness_bundle",
]
