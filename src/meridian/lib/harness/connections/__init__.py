"""Connection lookup helpers backed by the typed harness bundle registry."""

from __future__ import annotations

from typing import Any

from meridian.lib.core.types import HarnessId, TransportId
from meridian.lib.harness.connections.base import HarnessConnection


def get_connection_class(
    harness_id: HarnessId,
    transport_id: TransportId = TransportId.STREAMING,
) -> type[HarnessConnection[Any]]:
    """Return one connection class from the typed bundle registry."""
    # Harness bundles must register transport mappings before this lookup.

    from meridian.lib.harness import ensure_bootstrap
    from meridian.lib.harness.bundle import get_connection_cls

    ensure_bootstrap()
    return get_connection_cls(harness_id, transport_id)


__all__ = ["get_connection_class"]
