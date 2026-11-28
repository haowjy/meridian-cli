from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ExtensionCapability(StrEnum):
    """Capabilities that can be granted to extension command execution."""

    SUBPROCESS = "subprocess"
    KERNEL = "kernel"
    HITL = "hitl"


@dataclass(frozen=True)
class ExtensionCapabilities:
    """Granted capabilities for a command invocation."""

    subprocess: bool = False
    kernel: bool = False
    hitl: bool = False

    def has(self, capability: str) -> bool:

        return bool(getattr(self, capability, False))


__all__ = [
    "ExtensionCapabilities",
    "ExtensionCapability",
]
