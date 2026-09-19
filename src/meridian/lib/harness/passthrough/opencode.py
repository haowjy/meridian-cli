"""OpenCode TUI passthrough configuration for managed primary sessions.

V1 and V2 attach differently:

- V1 (frozen): ``opencode attach <http_url> --session <id>``.
- V2 (2.0.6): no ``attach`` subcommand exists; the bare TUI connects to an
  explicit server with ``opencode --server <http_url> --session <id>`` and
  authenticates via ``OPENCODE_PASSWORD`` in its environment. The password is
  carried on :class:`ObserverEndpoint.client_env` and merged into the TUI child
  env by the attach launcher.

The connection selects the dialect via :attr:`ObserverEndpoint.attach_style`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections.base import (
    ConnectionConfig,
    HarnessConnection,
    ObserverEndpoint,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec

from .base import PassthroughError, TuiCommandBuilder


def _require_observer_endpoint(
    connection: HarnessConnection[Any],
    *,
    transport: str,
) -> ObserverEndpoint:
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
    return endpoint


def build_opencode_attach_command(
    session_id: str,
    http_url: str,
) -> tuple[str, ...]:
    """Build the V1 `opencode attach {http_url} --session {session_id}` command."""

    return ("opencode", "attach", http_url, "--session", session_id)


def build_opencode_server_attach_command(
    session_id: str,
    http_url: str,
) -> tuple[str, ...]:
    """Build the V2 `opencode --server {http_url} --session {session_id}` command.

    V2 dropped the ``attach`` subcommand; the bare TUI is the attach surface.
    Authentication is supplied separately via ``OPENCODE_PASSWORD``.
    """

    return ("opencode", "--server", http_url, "--session", session_id)


class OpenCodePassthrough:
    """Build OpenCode managed primary TUI passthrough inputs."""

    def build_config(
        self,
        *,
        spawn_id: SpawnId,
        spec: ResolvedLaunchSpec,
        control_root: Path,
        task_cwd: Path | None,
        env: dict[str, str],
    ) -> ConnectionConfig:
        return ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.OPENCODE,
            prompt=spec.prompt,
            control_root=control_root,
            child_env=dict(env),
            task_cwd=task_cwd,
            system=spec.appended_system_prompt or None,
        )

    def build_tui_command(
        self,
        connection: HarnessConnection[Any],
        spec: ResolvedLaunchSpec,
    ) -> TuiCommandBuilder:
        _ = spec

        def _build(session_id: str) -> tuple[str, ...]:
            # Resolve at invocation time: the observer endpoint only exists once
            # the managed backend has started.
            endpoint = _require_observer_endpoint(connection, transport="http")
            if endpoint.attach_style == "server":
                return build_opencode_server_attach_command(
                    session_id=session_id,
                    http_url=endpoint.url,
                )
            return build_opencode_attach_command(
                session_id=session_id,
                http_url=endpoint.url,
            )

        return _build


__all__ = [
    "OpenCodePassthrough",
    "build_opencode_attach_command",
    "build_opencode_server_attach_command",
]
