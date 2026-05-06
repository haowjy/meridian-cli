"""OpenCode TUI passthrough configuration for managed primary sessions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.connections.base import ConnectionConfig, HarnessConnection
from meridian.lib.harness.ids import HarnessId
from meridian.lib.harness.launch_spec import OpenCodeLaunchSpec
from meridian.lib.launch.launch_types import ResolvedLaunchSpec

from .base import PassthroughError, TuiCommandBuilder


def _require_observer_endpoint_url(
    connection: HarnessConnection[Any],
    *,
    transport: str,
) -> str:
    endpoint = connection.observer_endpoint
    if endpoint is None:
        raise PassthroughError(
            f"Managed backend did not expose an observer endpoint for {connection.harness_id.value}"
        )
    if endpoint.transport != transport:
        raise PassthroughError(
            "Managed backend exposed unexpected observer transport "
            f"'{endpoint.transport}' (expected '{transport}')"
        )
    return endpoint.url


def _build_opencode_attach_command(
    session_id: str,
    http_url: str,
) -> tuple[str, ...]:
    """Build `opencode attach {http_url} --session {session_id}`."""

    return ("opencode", "attach", http_url, "--session", session_id)


class OpenCodePassthrough:
    """Build OpenCode managed primary TUI passthrough inputs."""

    def build_config(
        self,
        *,
        spawn_id: SpawnId,
        spec: ResolvedLaunchSpec,
        execution_cwd: Path,
        env: dict[str, str],
    ) -> ConnectionConfig:
        if not isinstance(spec, OpenCodeLaunchSpec):
            raise PassthroughError(f"Expected OpenCodeLaunchSpec, got {type(spec).__name__}")
        return ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.OPENCODE,
            prompt=spec.prompt,
            project_root=execution_cwd,
            env_overrides=dict(env),
            system=spec.appended_system_prompt or None,
        )

    def build_tui_command(
        self,
        connection: HarnessConnection[Any],
        spec: ResolvedLaunchSpec,
    ) -> TuiCommandBuilder:
        _ = spec
        return lambda session_id: _build_opencode_attach_command(
            session_id=session_id,
            http_url=_require_observer_endpoint_url(connection, transport="http"),
        )


__all__ = ["OpenCodePassthrough"]
