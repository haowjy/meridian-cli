"""Core meridian library exports."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from meridian.lib.core.domain import Spawn
    from meridian.lib.core.types import HarnessId, ModelId, SpawnId

__all__ = ["HarnessId", "ModelId", "Spawn", "SpawnId"]


def __getattr__(name: str):
    if name == "Spawn":
        from meridian.lib.core.domain import Spawn

        return Spawn
    if name in ("HarnessId", "ModelId", "SpawnId"):
        from meridian.lib.core.types import HarnessId, ModelId, SpawnId

        return {"HarnessId": HarnessId, "ModelId": ModelId, "SpawnId": SpawnId}[name]
    raise AttributeError(f"module 'meridian.lib' has no attribute {name!r}")
