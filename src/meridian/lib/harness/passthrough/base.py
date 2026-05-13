"""TuiPassthrough protocol — builds harness-specific attach configuration."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.connections.base import ConnectionConfig, HarnessConnection
from meridian.lib.launch.launch_types import ResolvedLaunchSpec

TuiCommandBuilder = Callable[[str], tuple[str, ...]]


class PassthroughError(Exception):
    """Passthrough configuration failed before primary attach launch."""


class TuiPassthrough(Protocol):
    """Build attach configuration for one harness's TUI passthrough."""

    def build_config(
        self,
        *,
        spawn_id: SpawnId,
        spec: ResolvedLaunchSpec,
        control_root: Path,
        task_cwd: Path | None,
        env: dict[str, str],
    ) -> ConnectionConfig:
        """Build the ConnectionConfig for observer mode."""
        ...

    def build_tui_command(
        self,
        connection: HarnessConnection[Any],
        spec: ResolvedLaunchSpec,
    ) -> TuiCommandBuilder:
        """Return a TUI command builder for the given connection."""
        ...
