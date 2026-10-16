"""Structured harness launch errors."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from meridian.lib.core.types import HarnessId


@dataclass
class HarnessLaunchFailure(RuntimeError):
    """Base class for harness launch failures with structured metadata."""

    harness_id: str


@dataclass
class HarnessBinaryNotFound(HarnessLaunchFailure):
    """Raised when a harness binary cannot be resolved on PATH."""

    binary_name: str
    searched_path: str

    def __post_init__(self) -> None:
        RuntimeError.__init__(
            self,
            (
                f"Harness binary not found: harness_id={self.harness_id} "
                f"binary_name={self.binary_name!r} searched_path={self.searched_path!r}"
            ),
        )

    @classmethod
    def from_os_error(
        cls,
        *,
        harness_id: HarnessId | str,
        error: FileNotFoundError | NotADirectoryError,
        binary_name: str | None = None,
        searched_path: str | None = None,
    ) -> HarnessBinaryNotFound:
        resolved_binary = binary_name or _binary_name_from_error(error) or "<unknown>"
        return cls(
            harness_id=str(harness_id),
            binary_name=resolved_binary,
            searched_path=searched_path
            if searched_path is not None
            else os.environ.get("PATH", ""),
        )


def _binary_name_from_error(error: FileNotFoundError | NotADirectoryError) -> str | None:
    filename = error.filename
    if not filename:
        return None
    return Path(str(filename)).name or str(filename)


__all__ = ["HarnessBinaryNotFound", "HarnessLaunchFailure"]
