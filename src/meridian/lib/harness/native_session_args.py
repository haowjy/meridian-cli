"""Small adapter-local boundary for native session selectors in raw args.

Each harness owns its grammar. This module only defines the normalized value
and refuses raw input until a harness supplies a normalizer.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

NativeSessionSurface = Literal["subprocess", "managed"]


@dataclass(frozen=True)
class NativeSessionSelector:
    operation: Literal["resume", "fork"]
    native_id: str

    def __post_init__(self) -> None:
        if not self.native_id.strip():
            raise ValueError("native session selector requires a non-empty ID")


@dataclass(frozen=True)
class NormalizedNativeSessionArgs:
    selector: NativeSessionSelector | None
    remaining_args: tuple[str, ...]


class NativeSessionArgsNormalizer(Protocol):
    """Normalize one raw argument vector for a harness-specific launch surface."""

    def __call__(
        self, args: tuple[str, ...], surface: NativeSessionSurface, /
    ) -> NormalizedNativeSessionArgs: ...


def normalize_native_session_args(
    args: tuple[str, ...],
    surface: NativeSessionSurface,
    normalizer: NativeSessionArgsNormalizer | Callable[
        [tuple[str, ...], NativeSessionSurface], NormalizedNativeSessionArgs
    ] | None,
) -> NormalizedNativeSessionArgs:
    """Normalize once, refusing nonempty raw input for an unimplemented adapter."""
    if normalizer is None:
        if args:
            raise ValueError("raw native session arguments are unsupported for this adapter")
        return NormalizedNativeSessionArgs(None, args)
    return normalizer(args, surface)
