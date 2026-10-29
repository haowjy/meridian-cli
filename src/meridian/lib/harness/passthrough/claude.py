"""Claude managed primary attach stub.

Claude primary launches use subprocess passthrough rather than managed attach.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from meridian.lib.harness.connections.base import ConnectionConfig, HarnessConnection
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.state.paths import LaunchPaths

from .base import PassthroughError, TuiCommandBuilder


class ClaudePassthrough:
    """Reject managed primary attach for Claude."""

    def build_config(
        self,
        *,
        launch: LaunchPaths,
        spec: ResolvedLaunchSpec,
        control_root: Path,
        task_cwd: Path | None,
        env: dict[str, str],
    ) -> ConnectionConfig:
        _ = launch, spec, control_root, task_cwd, env
        raise PassthroughError("Managed primary attach is not supported for claude")

    def build_tui_command(
        self,
        connection: HarnessConnection[Any],
        spec: ResolvedLaunchSpec,
    ) -> TuiCommandBuilder:
        _ = connection, spec
        raise PassthroughError("Managed primary attach is not supported for claude")


__all__ = ["ClaudePassthrough"]
