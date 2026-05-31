"""Codex TUI passthrough configuration for managed primary sessions."""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections.base import ConnectionConfig, HarnessConnection
from meridian.lib.launch.launch_types import ResolvedLaunchSpec

from .base import PassthroughError, TuiCommandBuilder


def _reserve_local_port(host: str = "127.0.0.1") -> int:
    """Reserve one ephemeral TCP port and return it."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


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


def _build_codex_attach_command(
    session_id: str,
    ws_url: str,
    prompt: str | None,
    projected_roots: tuple[Path, ...],
) -> tuple[str, ...]:
    """Build `codex resume {session_id} --remote {ws_url} [prompt]`."""

    command = ["codex", "resume", session_id, "--remote", ws_url]
    for root in projected_roots:
        command.extend(("--add-dir", root.as_posix()))
    if prompt and prompt.strip():
        command.append(prompt)
    return tuple(command)


class CodexPassthrough:
    """Build Codex managed primary TUI passthrough inputs."""

    def build_config(
        self,
        *,
        spawn_id: SpawnId,
        spec: ResolvedLaunchSpec,
        control_root: Path,
        task_cwd: Path | None,
        env: dict[str, str],
    ) -> ConnectionConfig:
        ws_bind_host = "127.0.0.1"
        return ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.CODEX,
            prompt=spec.prompt,
            control_root=control_root,
            env_overrides=dict(env),
            task_cwd=task_cwd,
            ws_bind_host=ws_bind_host,
            ws_port=_reserve_local_port(ws_bind_host),
        )

    def build_tui_command(
        self,
        connection: HarnessConnection[Any],
        spec: ResolvedLaunchSpec,
    ) -> TuiCommandBuilder:

        return lambda session_id: _build_codex_attach_command(
            session_id=session_id,
            ws_url=_require_observer_endpoint_url(connection, transport="ws"),
            prompt=spec.user_turn_content,
            projected_roots=spec.projected_roots,
        )


__all__ = ["CodexPassthrough"]
