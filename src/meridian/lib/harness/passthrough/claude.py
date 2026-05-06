"""Claude managed primary attach stub.

Claude primary launches use subprocess passthrough rather than managed attach.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.connections.base import ConnectionConfig, HarnessConnection
from meridian.lib.launch.launch_types import ResolvedLaunchSpec

from .base import PassthroughError, TuiCommandBuilder


class ClaudePassthrough:
    """Reject managed primary attach for Claude."""

    def build_config(
        self,
        *,
        spawn_id: SpawnId,
        spec: ResolvedLaunchSpec,
        execution_cwd: Path,
        env: dict[str, str],
    ) -> ConnectionConfig:
        _ = spawn_id, spec, execution_cwd, env
        raise PassthroughError("Managed primary attach is not supported for claude")

    def build_tui_command(
        self,
        connection: HarnessConnection[Any],
        spec: ResolvedLaunchSpec,
    ) -> TuiCommandBuilder:
        _ = connection, spec
        raise PassthroughError("Managed primary attach is not supported for claude")


__all__ = ["ClaudePassthrough"]
