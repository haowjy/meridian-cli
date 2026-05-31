"""Streaming message and control types."""

from typing import TYPE_CHECKING

from meridian.lib.streaming.types import ControlMessage, InjectResult

if TYPE_CHECKING:
    from meridian.lib.streaming.control_socket import ControlSocketServer
    from meridian.lib.streaming.spawn_manager import SpawnManager
    from meridian.lib.streaming.spawn_session import SpawnSession

__all__ = [
    "ControlMessage",
    "ControlSocketServer",
    "InjectResult",
    "SpawnManager",
    "SpawnSession",
]


def __getattr__(name: str) -> object:
    if name == "ControlSocketServer":
        from meridian.lib.streaming.control_socket import ControlSocketServer

        return ControlSocketServer
    if name in {"SpawnManager", "SpawnSession"}:
        from meridian.lib.streaming.spawn_manager import SpawnManager
        from meridian.lib.streaming.spawn_session import SpawnSession

        return {"SpawnManager": SpawnManager, "SpawnSession": SpawnSession}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
